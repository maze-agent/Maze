"""Deterministic fake of the public benchmark Runtime Protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ascend_maze.benchmark.canonical import canonical_json_digest, stable_payload_id
from ascend_maze.benchmark.clock import BenchmarkClock
from ascend_maze.benchmark.contracts import CellSpec, ExperimentSpec
from ascend_maze.benchmark.runtime import (
    BenchmarkRuntimeClient,
    BenchmarkRuntimeFactory,
    ResourceRecoveryResult,
    ResourceSnapshot,
    RunFlushResult,
    SubmissionReceipt,
    TerminalRunResult,
)
from ascend_maze.contracts.data import SharedFileRef
from ascend_maze.core.errors import ExperimentValidationError


@dataclass(slots=True)
class _FakeRun:
    run_id: str
    submission_id: str
    payload_digest: str
    terminal_at_ms: int
    status: str
    cancelled: bool = False
    flushed: bool = False
    destroyed: bool = False


class FakeBenchmarkRuntime(BenchmarkRuntimeClient):
    def __init__(
        self,
        clock: BenchmarkClock,
        *,
        run_duration_ms: int = 1,
        terminal_status: str = "succeeded",
        lose_first_commit_response: bool = False,
        recording_complete: bool = True,
        leak_resources: bool = False,
        config_fingerprint: str = "f" * 64,
    ) -> None:
        if run_duration_ms < 0:
            raise ValueError("fake Run duration must be non-negative")
        self.clock = clock
        self.run_duration_ms = run_duration_ms
        self.terminal_status = terminal_status
        self.lose_first_commit_response = lose_first_commit_response
        self.recording_complete = recording_complete
        self.leak_resources = leak_resources
        self.config_fingerprint = config_fingerprint
        self.controller_generation = "controller_generation_fake"
        self.runs_by_submission: dict[str, _FakeRun] = {}
        self.runs_by_id: dict[str, _FakeRun] = {}
        self.submit_calls: list[str] = []
        self.flush_calls: list[tuple[str, str]] = []
        self.cancel_calls: list[tuple[str, str]] = []
        self.destroy_calls: list[tuple[str, str, bool]] = []
        self.shutdown_calls: list[str] = []
        self.max_active_runs = 0
        self._lost_responses: set[str] = set()

    async def prepare_trial(self) -> Mapping[str, object]:
        return {"model_ready": True, "standby_ready": True}

    async def resource_snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot.create(
            captured_at_wall_ms=self.clock.wall_ms(),
            controller_generation=self.controller_generation,
            config_fingerprint=self.config_fingerprint,
            payload={
                "active_run_ids": sorted(
                    item.run_id
                    for item in self.runs_by_id.values()
                    if not item.destroyed
                ),
                "run_count": len(self.runs_by_id),
            },
        )

    async def submit(
        self,
        workflow: object,
        *,
        inputs: dict[str, object],
        submission_id: str,
        run_deadline_ms: int | None,
    ) -> SubmissionReceipt:
        del workflow, run_deadline_ms
        self.submit_calls.append(submission_id)
        digest = canonical_json_digest(_fake_json_value(inputs))
        existing = self.runs_by_submission.get(submission_id)
        if existing is not None:
            if existing.payload_digest != digest:
                raise ExperimentValidationError(
                    "fake submission ID was reused with another payload"
                )
            return SubmissionReceipt(
                submission_id=submission_id,
                state="committed",
                run_id=existing.run_id,
                replayed=True,
            )
        run_id = stable_payload_id("run", {"submission_id": submission_id}, length=32)
        run = _FakeRun(
            run_id=run_id,
            submission_id=submission_id,
            payload_digest=digest,
            terminal_at_ms=self.clock.monotonic_ms() + self.run_duration_ms,
            status=self.terminal_status,
        )
        self.runs_by_submission[submission_id] = run
        self.runs_by_id[run_id] = run
        self.max_active_runs = max(
            self.max_active_runs,
            sum(
                item.terminal_at_ms > self.clock.monotonic_ms() and not item.cancelled
                for item in self.runs_by_id.values()
            ),
        )
        if (
            self.lose_first_commit_response
            and submission_id not in self._lost_responses
        ):
            self._lost_responses.add(submission_id)
            raise TimeoutError("injected response loss after commit")
        return SubmissionReceipt(
            submission_id=submission_id,
            state="committed",
            run_id=run_id,
        )

    async def wait_terminal(
        self, run_id: str, *, deadline_monotonic_ms: int
    ) -> TerminalRunResult:
        run = self._run(run_id)
        terminal_at = self.clock.monotonic_ms() if run.cancelled else run.terminal_at_ms
        if terminal_at > deadline_monotonic_ms:
            await self.clock.wait_until(deadline_monotonic_ms)
            raise TimeoutError("fake Run terminal deadline expired")
        await self.clock.wait_until(terminal_at)
        status = "cancelled" if run.cancelled else run.status
        return TerminalRunResult.create(
            run_id,
            status,
            {
                "run_id": run_id,
                "status": status,
                "terminal_at_ms": terminal_at,
            },
        )

    async def flush_run(self, run_id: str, *, request_id: str) -> RunFlushResult:
        run = self._run(run_id)
        self.flush_calls.append((run_id, request_id))
        run.flushed = True
        return RunFlushResult.create(
            run_id,
            self.recording_complete,
            (),
            {
                "run_id": run_id,
                "recording_complete": self.recording_complete,
                "committed_files": [],
            },
        )

    async def cancel_run(self, run_id: str, *, request_id: str) -> None:
        self.cancel_calls.append((run_id, request_id))
        self._run(run_id).cancelled = True

    async def destroy_run(
        self,
        run_id: str,
        *,
        request_id: str,
        force: bool = False,
    ) -> None:
        run = self._run(run_id)
        self.destroy_calls.append((run_id, request_id, force))
        if not run.flushed and not force:
            raise ExperimentValidationError("fake Run must be flushed before destroy")
        if not self.recording_complete and not force:
            raise ExperimentValidationError(
                "recording is incomplete; force is required"
            )
        run.destroyed = True

    async def wait_for_recovery(
        self,
        before: ResourceSnapshot,
        *,
        run_ids: tuple[str, ...],
        deadline_monotonic_ms: int,
    ) -> tuple[ResourceSnapshot, ResourceRecoveryResult]:
        del before, deadline_monotonic_ms
        unreleased = sorted(
            run_id for run_id in run_ids if not self._run(run_id).destroyed
        )
        recovered = not unreleased and not self.leak_resources
        after = await self.resource_snapshot()
        return after, ResourceRecoveryResult.create(
            recovered=recovered,
            checked_at_wall_ms=self.clock.wall_ms(),
            reason_code=None if recovered else "resource_recovery_failed",
            details={
                "unreleased_run_ids": unreleased,
                "injected_leak": self.leak_resources,
            },
        )

    async def shutdown(self, *, request_id: str) -> Mapping[str, object]:
        self.shutdown_calls.append(request_id)
        return {"cleanup_confirmed": True, "timed_out": False, "exit_code": 0}

    async def finalize_recovery(
        self,
        before: ResourceSnapshot,
        after: ResourceSnapshot,
        recovery: ResourceRecoveryResult,
        *,
        deadline_monotonic_ms: int,
    ) -> tuple[ResourceSnapshot, ResourceRecoveryResult]:
        del before, deadline_monotonic_ms
        return after, recovery

    def _run(self, run_id: str) -> _FakeRun:
        try:
            return self.runs_by_id[run_id]
        except KeyError as exc:
            raise ExperimentValidationError(f"unknown fake Run: {run_id}") from exc


class FakeBenchmarkRuntimeFactory(BenchmarkRuntimeFactory):
    analysis_after_each_trial = False

    def __init__(self, runtime: FakeBenchmarkRuntime) -> None:
        self.runtime = runtime
        self.open_calls: list[tuple[str, bool]] = []

    async def open(
        self,
        *,
        spec: ExperimentSpec,
        cell: CellSpec,
        trial_attempt_id: str,
        trial_directory: str,
        resume: bool,
    ) -> BenchmarkRuntimeClient:
        del spec, trial_directory
        self.open_calls.append((trial_attempt_id, resume))
        self.runtime.config_fingerprint = cell.config_snapshot.config_fingerprint
        return self.runtime


def _fake_json_value(value: object) -> object:
    if isinstance(value, SharedFileRef):
        return {
            "$shared_file": {
                "canonical_path": value.canonical_path,
                "content_sha256": value.content_sha256,
                "size_bytes": value.size_bytes,
            }
        }
    if isinstance(value, Mapping):
        return {
            str(key): _fake_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_fake_json_value(item) for item in value]
    return value
