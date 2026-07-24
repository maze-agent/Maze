"""Local RuntimeClient preserving staged input ownership across disconnects."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import stat
from time import perf_counter
from typing import Callable

from ascend_maze.api.workflow import Workflow
from ascend_maze.compiler.ir import CompiledWorkflow
from ascend_maze.contracts.data import DataHandle, SharedFileRef
from ascend_maze.contracts.submission import (
    RunInputIdentity,
    SubmissionContract,
    SubmissionOptions,
    SubmissionState,
    hash_session_key,
)
from ascend_maze.core.canonical import FrozenMap, freeze_canonical
from ascend_maze.core.errors import (
    ContractValidationError,
    DataHandleInvalidError,
    SubmissionAbortedError,
    SubmissionConflictError,
)
from ascend_maze.core.identifiers import new_id
from ascend_maze.runtime.packaging import build_code_packages

from ascend_maze.control.controller import (
    InMemoryController,
    SubmissionOutcome,
    SubmitRequest,
)


@dataclass(frozen=True, slots=True)
class PreparedSubmission:
    request: SubmitRequest
    input_signature: tuple[tuple[str, tuple[str, ...]], ...]


class InMemoryRuntimeClient:
    def __init__(
        self,
        controller: InMemoryController,
        *,
        shared_filesystem_roots: tuple[str, ...] = (),
    ) -> None:
        self.controller = controller
        self.shared_filesystem_roots = normalize_shared_filesystem_roots(
            shared_filesystem_roots
        )
        self._prepared: dict[str, PreparedSubmission] = {}
        self.last_prepare_trace: dict[str, object] = {}

    def prepare_submission(
        self,
        workflow: Workflow | CompiledWorkflow,
        *,
        inputs: dict[str, object],
        submission_id: str | None = None,
        session_key: str | None = None,
        run_deadline_ms: int | None = None,
        execution_options: dict[str, object] | None = None,
    ) -> PreparedSubmission:
        trace_started = perf_counter()
        trace: dict[str, object] = {}
        stage_started = perf_counter()
        if isinstance(workflow, Workflow):
            compiled = workflow._compiled or workflow.compile()
            callables_by_definition: dict[str, Callable[..., object]] = {}
            for draft in workflow._draft_tasks:
                definition_id = compiled.tasks[draft.task_id].definition_id
                callables_by_definition.setdefault(definition_id, draft.template.func)
        else:
            compiled = workflow
            callables_by_definition = {}
        trace["compile_or_resolve_ms"] = _elapsed_ms(stage_started)

        stage_started = perf_counter()
        if set(inputs) != set(compiled.workflow_inputs):
            missing = sorted(set(compiled.workflow_inputs) - set(inputs))
            extra = sorted(set(inputs) - set(compiled.workflow_inputs))
            raise ValueError(f"workflow input mismatch; missing={missing}, extra={extra}")
        trace["input_validation_ms"] = _elapsed_ms(stage_started)

        resolved_submission_id = submission_id or new_id("submission")
        stage_started = perf_counter()
        frozen_execution_options = freeze_canonical(execution_options or {})
        if not isinstance(frozen_execution_options, FrozenMap):
            raise TypeError("execution_options must freeze to a mapping")
        options = SubmissionOptions(
            run_deadline_ms=run_deadline_ms,
            execution_options=frozen_execution_options,
        )
        trace["options_ms"] = _elapsed_ms(stage_started)
        trace["staged_input_count"] = len(inputs)

        existing = self._prepared.get(resolved_submission_id)
        if existing is not None:
            stage_started = perf_counter()
            signature = tuple(
                (name, self._source_identity(inputs[name]))
                for name in sorted(inputs)
            )
            trace["input_signature_ms"] = _elapsed_ms(stage_started)
            old = existing.request
            if (
                old.compiled.workflow_fingerprint != compiled.workflow_fingerprint
                or existing.input_signature != signature
                or old.contract.session_key_hash != hash_session_key(session_key)
                or old.contract.options != options
                or old.contract.config_fingerprint
                != self.controller.config_fingerprint
            ):
                raise SubmissionConflictError(
                    "local submission_id is already prepared with another payload"
                )
            trace["prepared_cache_hit"] = True
            trace["total_ms"] = _elapsed_ms(trace_started)
            self.last_prepare_trace = trace
            return existing

        handles: list[tuple[str, DataHandle]] = []
        try:
            stage_started = perf_counter()
            for name in sorted(inputs):
                value = inputs[name]
                if isinstance(value, SharedFileRef):
                    validate_shared_file_ref(
                        value, self.shared_filesystem_roots
                    )
                handles.append(
                    (
                        name,
                        self.controller.data_store.put_staged_for_submission_input(
                            value, self.controller.data_owner_generation
                        ),
                    )
                )
            trace["input_staging_ms"] = _elapsed_ms(stage_started)
        except Exception:
            for _, handle in handles:
                self.controller.data_store.release(handle)
            raise

        stage_started = perf_counter()
        signature = tuple(
            (name, self._source_identity(inputs[name]))
            for name in sorted(inputs)
        )
        trace["input_signature_ms"] = _elapsed_ms(stage_started)

        stage_started = perf_counter()
        identities = tuple(
            run_input_identity(name, inputs[name], handle)
            for name, handle in handles
        )
        trace["input_identity_ms"] = _elapsed_ms(stage_started)

        stage_started = perf_counter()
        contract = SubmissionContract.create(
            submission_id=resolved_submission_id,
            workflow_fingerprint=compiled.workflow_fingerprint,
            input_identities=identities,
            session_key_hash=hash_session_key(session_key),
            options=options,
            config_fingerprint=self.controller.config_fingerprint,
        )
        trace["contract_ms"] = _elapsed_ms(stage_started)

        stage_started = perf_counter()
        code_packages = build_code_packages(
            compiled,
            environment_fingerprint=self.controller.environment_fingerprint,
            callables_by_definition=callables_by_definition,
        )
        trace["code_package_ms"] = _elapsed_ms(stage_started)
        request = SubmitRequest(
            compiled=compiled,
            code_packages=code_packages,
            workflow_inputs=tuple(handles),
            contract=contract,
        )
        prepared = PreparedSubmission(request=request, input_signature=signature)
        self._prepared[resolved_submission_id] = prepared
        trace["prepared_cache_hit"] = False
        trace["total_ms"] = _elapsed_ms(trace_started)
        self.last_prepare_trace = trace
        return prepared

    async def submit_prepared(
        self,
        prepared: PreparedSubmission,
        *,
        lose_response_after_commit: bool = False,
    ) -> SubmissionOutcome:
        try:
            outcome = await self.controller.submit(
                prepared.request,
                lose_response_after_commit=lose_response_after_commit,
            )
        except SubmissionConflictError:
            self._release_staged_inputs(prepared)
            self._prepared.pop(prepared.request.contract.submission_id, None)
            raise
        if outcome.state is SubmissionState.ABORTED:
            self._release_staged_inputs(prepared)
        elif outcome.replayed:
            self._release_staged_inputs(prepared)
        self._prepared.pop(prepared.request.contract.submission_id, None)
        return outcome

    @property
    def prepared_submission_count(self) -> int:
        return len(self._prepared)

    async def submit(
        self,
        workflow: Workflow | CompiledWorkflow,
        *,
        inputs: dict[str, object],
        submission_id: str | None = None,
        session_key: str | None = None,
        run_deadline_ms: int | None = None,
        execution_options: dict[str, object] | None = None,
        lose_response_after_commit: bool = False,
    ) -> SubmissionOutcome:
        prepared = self.prepare_submission(
            workflow,
            inputs=inputs,
            submission_id=submission_id,
            session_key=session_key,
            run_deadline_ms=run_deadline_ms,
            execution_options=execution_options,
        )
        return await self.submit_prepared(
            prepared,
            lose_response_after_commit=lose_response_after_commit,
        )

    async def run(
        self,
        workflow: Workflow | CompiledWorkflow,
        *,
        inputs: dict[str, object],
        submission_id: str | None = None,
        session_key: str | None = None,
        run_deadline_ms: int | None = None,
        execution_options: dict[str, object] | None = None,
        lose_response_after_commit: bool = False,
    ) -> str:
        outcome = await self.submit(
            workflow,
            inputs=inputs,
            submission_id=submission_id,
            session_key=session_key,
            run_deadline_ms=run_deadline_ms,
            execution_options=execution_options,
            lose_response_after_commit=lose_response_after_commit,
        )
        if outcome.state is SubmissionState.ABORTED or outcome.run_id is None:
            raise SubmissionAbortedError(outcome.error or "submission aborted")
        return outcome.run_id

    def _release_staged_inputs(self, prepared: PreparedSubmission) -> None:
        for _, handle in prepared.request.workflow_inputs:
            try:
                if self.controller.data_store.state_of(handle) == "staged":
                    self.controller.data_store.release(handle)
            except DataHandleInvalidError:
                pass

    @staticmethod
    def _source_identity(value: object) -> tuple[str, ...]:
        if isinstance(value, SharedFileRef):
            return (
                "shared_file",
                value.canonical_path,
                value.content_sha256,
                str(value.size_bytes),
            )
        return (
            "object",
            type(value).__module__,
            type(value).__qualname__,
            str(id(value)),
        )

    def get_submission_status(self, submission_id: str) -> SubmissionOutcome | None:
        if not isinstance(submission_id, str) or not submission_id:
            raise ValueError("submission_id is required")
        try:
            return self.controller.submission_outcome(submission_id)
        except KeyError:
            return None


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1_000))


def validate_shared_file_ref(
    file_ref: SharedFileRef,
    shared_filesystem_roots: tuple[str, ...],
) -> None:
    path = Path(file_ref.canonical_path)
    if not shared_filesystem_roots:
        raise ContractValidationError(
            "SharedFileRef requires data.shared_filesystem_roots"
        )
    if not any(
        path == root or root in path.parents
        for root in (Path(value) for value in shared_filesystem_roots)
    ):
        raise ContractValidationError(
            "SharedFileRef path is outside data.shared_filesystem_roots"
        )
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            info_before = os.fstat(stream.fileno())
            if not stat.S_ISREG(info_before.st_mode):
                raise ContractValidationError("SharedFileRef path must be a regular file")
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
            info_after = os.fstat(stream.fileno())
    except OSError as exc:
        raise ContractValidationError(
            f"SharedFileRef is not readable on Head: {path}"
        ) from exc
    if (
        info_before.st_size != info_after.st_size
        or info_before.st_mtime_ns != info_after.st_mtime_ns
    ):
        raise ContractValidationError("SharedFileRef changed while it was validated")
    if size != file_ref.size_bytes:
        raise ContractValidationError("SharedFileRef size_bytes does not match file")
    if not hmac.compare_digest(digest.hexdigest(), file_ref.content_sha256):
        raise ContractValidationError("SharedFileRef content_sha256 does not match file")


def run_input_identity(
    name: str,
    value: object,
    handle: DataHandle,
) -> RunInputIdentity:
    if isinstance(value, SharedFileRef):
        return RunInputIdentity.from_shared_file(name, value)
    return RunInputIdentity.from_data_handle(name, handle)


def normalize_shared_filesystem_roots(values: tuple[str, ...]) -> tuple[str, ...]:
    roots: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise ContractValidationError("shared filesystem root must be a path string")
        roots.append(str(Path(value).expanduser().resolve(strict=False)))
    return tuple(sorted(set(roots)))
