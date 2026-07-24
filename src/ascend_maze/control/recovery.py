"""Durable Controller checkpoints and generation fencing for startup recovery."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import pickle
import sqlite3
from threading import RLock
from typing import Protocol, runtime_checkable

from ascend_maze.compiler.ir import CompiledWorkflow
from ascend_maze.contracts.data import DataHandle
from ascend_maze.contracts.recording import RunRecordingContext
from ascend_maze.contracts.runtime import CodePackage
from ascend_maze.contracts.submission import SubmissionContract, SubmissionState
from ascend_maze.core.errors import ContractValidationError, StateTransitionError
from ascend_maze.data.index import RunDataIndexCheckpoint
from ascend_maze.lifecycle.state import RunSnapshot
from ascend_maze.placement.manager import LeaseSnapshot


@dataclass(frozen=True, slots=True)
class RecoveryIdentity:
    cluster_id: str
    config_fingerprint: str
    environment_fingerprint: str
    build_revision: str

    def __post_init__(self) -> None:
        for name in (
            "cluster_id",
            "config_fingerprint",
            "environment_fingerprint",
            "build_revision",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ContractValidationError(f"recovery {name} is required")


@dataclass(frozen=True, slots=True)
class SubmissionRecoveryRecord:
    submission_id: str
    payload_hash: str
    state: SubmissionState
    run_id: str | None
    error: str | None
    compiled: CompiledWorkflow
    code_packages: tuple[CodePackage, ...]
    workflow_inputs: tuple[tuple[str, DataHandle], ...]
    contract: SubmissionContract

    def __post_init__(self) -> None:
        if not self.submission_id or not self.payload_hash:
            raise ContractValidationError("recovery submission identity is required")
        if self.contract.submission_id != self.submission_id:
            raise ContractValidationError("recovery submission contract mismatch")
        if self.contract.submission_payload_hash != self.payload_hash:
            raise ContractValidationError("recovery submission payload hash mismatch")
        if self.state is SubmissionState.COMMITTED and self.run_id is None:
            raise ContractValidationError("committed recovery submission requires run_id")


@dataclass(frozen=True, slots=True)
class RunRecoveryRecord:
    run_id: str
    submission_id: str
    snapshot: RunSnapshot
    index: RunDataIndexCheckpoint
    recording_context: RunRecordingContext
    session_key_hash: str | None
    destroy_result: object | None
    expected_producer_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.run_id or not self.submission_id:
            raise ContractValidationError("recovery run identity is required")
        if self.snapshot.run_id != self.run_id or self.index.reference.run_id != self.run_id:
            raise ContractValidationError("recovery run payload identity mismatch")
        if tuple(sorted(set(self.expected_producer_ids))) != self.expected_producer_ids:
            raise ContractValidationError(
                "recovery expected producer IDs must be sorted and unique"
            )


@dataclass(frozen=True, slots=True)
class ControllerCheckpoint:
    schema_version: int
    identity: RecoveryIdentity
    controller_generation: str
    checkpoint_sequence: int
    created_at_ms: int
    data_owner_generation: str
    data_store_descriptor: object | None
    submissions: tuple[SubmissionRecoveryRecord, ...]
    runs: tuple[RunRecoveryRecord, ...]
    leases: tuple[LeaseSnapshot, ...]
    model_instances: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractValidationError("unsupported Controller checkpoint schema")
        if not self.controller_generation or not self.data_owner_generation:
            raise ContractValidationError("checkpoint generations are required")
        if self.checkpoint_sequence < 1 or self.created_at_ms < 0:
            raise ContractValidationError("invalid Controller checkpoint sequence/time")
        submission_ids = [item.submission_id for item in self.submissions]
        run_ids = [item.run_id for item in self.runs]
        if len(submission_ids) != len(set(submission_ids)):
            raise ContractValidationError("duplicate checkpoint submission_id")
        if len(run_ids) != len(set(run_ids)):
            raise ContractValidationError("duplicate checkpoint run_id")


@dataclass(frozen=True, slots=True)
class RecoveryClaim:
    controller_generation: str
    epoch: int
    previous_generation: str | None
    checkpoint: ControllerCheckpoint | None


@runtime_checkable
class ControllerRecoveryStore(Protocol):
    def claim_generation(
        self,
        *,
        identity: RecoveryIdentity,
        controller_generation: str,
    ) -> RecoveryClaim: ...

    def save(
        self,
        checkpoint: ControllerCheckpoint,
        *,
        controller_generation: str,
        epoch: int,
    ) -> None: ...

    def load(self) -> ControllerCheckpoint | None: ...

    def assert_current(self, *, controller_generation: str, epoch: int) -> None: ...

    def close(self) -> None: ...


class InMemoryControllerRecoveryStore:
    """Thread-safe recovery authority used by local and deterministic tests."""

    def __init__(self) -> None:
        self._identity: RecoveryIdentity | None = None
        self._current_generation: str | None = None
        self._epoch = 0
        self._checkpoint: ControllerCheckpoint | None = None
        self._lock = RLock()

    def claim_generation(
        self,
        *,
        identity: RecoveryIdentity,
        controller_generation: str,
    ) -> RecoveryClaim:
        if not controller_generation:
            raise ContractValidationError("controller_generation is required")
        with self._lock:
            if self._identity is not None and self._identity != identity:
                raise StateTransitionError("recovery identity does not match existing state")
            previous = self._current_generation
            if previous == controller_generation:
                raise StateTransitionError("Controller generation is already claimed")
            self._identity = identity
            self._current_generation = controller_generation
            self._epoch += 1
            return RecoveryClaim(
                controller_generation=controller_generation,
                epoch=self._epoch,
                previous_generation=previous,
                checkpoint=self._checkpoint,
            )

    def save(
        self,
        checkpoint: ControllerCheckpoint,
        *,
        controller_generation: str,
        epoch: int,
    ) -> None:
        with self._lock:
            self._validate_writer(controller_generation, epoch)
            if checkpoint.identity != self._identity:
                raise StateTransitionError("checkpoint recovery identity changed")
            if checkpoint.controller_generation != controller_generation:
                raise StateTransitionError("checkpoint Controller generation mismatch")
            self._checkpoint = checkpoint

    def load(self) -> ControllerCheckpoint | None:
        with self._lock:
            return self._checkpoint

    def assert_current(self, *, controller_generation: str, epoch: int) -> None:
        with self._lock:
            self._validate_writer(controller_generation, epoch)

    def close(self) -> None:
        return None

    def _validate_writer(self, controller_generation: str, epoch: int) -> None:
        if (
            self._current_generation != controller_generation
            or self._epoch != epoch
        ):
            raise StateTransitionError("stale Controller generation is fenced")


class SqliteControllerRecoveryStore:
    """Single-row SQLite checkpoint store surviving Controller process loss."""

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS controller_recovery (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            cluster_id TEXT NOT NULL,
            config_fingerprint TEXT NOT NULL,
            environment_fingerprint TEXT NOT NULL,
            build_revision TEXT NOT NULL,
            current_generation TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            payload BLOB,
            payload_sha256 TEXT
        )
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(self._SCHEMA)
        self._lock = RLock()

    def claim_generation(
        self,
        *,
        identity: RecoveryIdentity,
        controller_generation: str,
    ) -> RecoveryClaim:
        if not controller_generation:
            raise ContractValidationError("controller_generation is required")
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                "SELECT cluster_id, config_fingerprint, environment_fingerprint, "
                "build_revision, current_generation, epoch, payload, payload_sha256 "
                "FROM controller_recovery WHERE singleton = 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO controller_recovery VALUES (1, ?, ?, ?, ?, ?, 1, NULL, NULL)",
                    (
                        identity.cluster_id,
                        identity.config_fingerprint,
                        identity.environment_fingerprint,
                        identity.build_revision,
                        controller_generation,
                    ),
                )
                return RecoveryClaim(controller_generation, 1, None, None)
            existing_identity = RecoveryIdentity(*row[:4])
            if existing_identity != identity:
                raise StateTransitionError("recovery identity does not match existing state")
            previous = str(row[4])
            if previous == controller_generation:
                raise StateTransitionError("Controller generation is already claimed")
            epoch = int(row[5]) + 1
            checkpoint = self._decode(row[6], row[7])
            connection.execute(
                "UPDATE controller_recovery SET current_generation = ?, epoch = ? "
                "WHERE singleton = 1",
                (controller_generation, epoch),
            )
            return RecoveryClaim(
                controller_generation,
                epoch,
                previous,
                checkpoint,
            )

    def save(
        self,
        checkpoint: ControllerCheckpoint,
        *,
        controller_generation: str,
        epoch: int,
    ) -> None:
        if checkpoint.controller_generation != controller_generation:
            raise StateTransitionError("checkpoint Controller generation mismatch")
        payload = pickle.dumps(checkpoint, protocol=pickle.HIGHEST_PROTOCOL)
        digest = hashlib.sha256(payload).hexdigest()
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                "SELECT cluster_id, config_fingerprint, environment_fingerprint, "
                "build_revision, current_generation, epoch "
                "FROM controller_recovery WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise StateTransitionError("Controller generation was not claimed")
            if RecoveryIdentity(*row[:4]) != checkpoint.identity:
                raise StateTransitionError("checkpoint recovery identity changed")
            if str(row[4]) != controller_generation or int(row[5]) != epoch:
                raise StateTransitionError("stale Controller generation is fenced")
            connection.execute(
                "UPDATE controller_recovery SET payload = ?, payload_sha256 = ? "
                "WHERE singleton = 1",
                (payload, digest),
            )

    def load(self) -> ControllerCheckpoint | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload, payload_sha256 FROM controller_recovery "
                "WHERE singleton = 1"
            ).fetchone()
            return None if row is None else self._decode(row[0], row[1])

    def assert_current(self, *, controller_generation: str, epoch: int) -> None:
        with self._lock:
            row = self._connection.execute(
                "SELECT current_generation, epoch FROM controller_recovery "
                "WHERE singleton = 1"
            ).fetchone()
            if (
                row is None
                or str(row[0]) != controller_generation
                or int(row[1]) != epoch
            ):
                raise StateTransitionError("stale Controller generation is fenced")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def rebind_checkpoint(
        checkpoint: ControllerCheckpoint,
        *,
        controller_generation: str,
        checkpoint_sequence: int,
        created_at_ms: int,
    ) -> ControllerCheckpoint:
        return replace(
            checkpoint,
            controller_generation=controller_generation,
            checkpoint_sequence=checkpoint_sequence,
            created_at_ms=created_at_ms,
        )

    class _Transaction:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __enter__(self) -> sqlite3.Connection:
            self.connection.execute("BEGIN IMMEDIATE")
            return self.connection

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            self.connection.execute("ROLLBACK" if exc_type is not None else "COMMIT")

    def _transaction(self) -> _Transaction:
        return self._Transaction(self._connection)

    @staticmethod
    def _decode(payload: object, digest: object) -> ControllerCheckpoint | None:
        if payload is None:
            return None
        if not isinstance(payload, bytes) or not isinstance(digest, str):
            raise StateTransitionError("Controller recovery payload is malformed")
        if hashlib.sha256(payload).hexdigest() != digest:
            raise StateTransitionError("Controller recovery payload checksum mismatch")
        value = pickle.loads(payload)
        if not isinstance(value, ControllerCheckpoint):
            raise StateTransitionError("Controller recovery payload type mismatch")
        return value
