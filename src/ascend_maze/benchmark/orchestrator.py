"""C14 Trial orchestration, durable resume, and arrival execution."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from ascend_maze.benchmark.canonical import (
    canonical_json_digest,
    stable_payload_id,
    thaw,
)
from ascend_maze.benchmark.clock import BenchmarkClock, SystemBenchmarkClock
from ascend_maze.benchmark.contracts import (
    CellSpec,
    ExperimentSpec,
    StudyPlan,
    TrialManifest,
    TrialSpec,
)
from ascend_maze.benchmark.loader import load_study_plan
from ascend_maze.benchmark.persistence import (
    AtomicWriteFailpoint,
    atomic_write_bytes,
    atomic_write_json,
    load_json_object,
)
from ascend_maze.benchmark.planning import file_sha256
from ascend_maze.benchmark.runtime import (
    BenchmarkRuntimeClient,
    BenchmarkRuntimeFactory,
    ResourceRecoveryResult,
    ResourceSnapshot,
    RunFlushResult,
)
from ascend_maze.benchmark.schedule import (
    ArrivalEntry,
    TrialSchedule,
    catch_up_spacing_ms,
    materialize_trial_schedule,
)
from ascend_maze.benchmark.schedule_parquet import (
    validate_schedule_parquet,
    write_schedule_parquet,
)
from ascend_maze.benchmark.state import (
    TERMINAL_TRIAL_STATES,
    TrialExecutionState,
    TrialRunRecord,
    parse_trial_execution_state,
)
from ascend_maze.benchmark.workload import (
    TraceSchedule,
    WorkloadRecord,
    load_trace_schedule,
    load_workflow,
    load_workload_dataset,
)
from ascend_maze.core.errors import ExperimentValidationError

STUDY_MANIFEST_SCHEMA = "ascend-maze.study-manifest.v1"


@dataclass(frozen=True, slots=True)
class TrialPaths:
    root: Path

    @property
    def state(self) -> Path:
        return self.root / "state.json"

    @property
    def trial_manifest(self) -> Path:
        return self.root / "trial_manifest.json"

    @property
    def run_manifest(self) -> Path:
        return self.root / "run_manifest.json"

    @property
    def schedule(self) -> Path:
        return self.root / "arrival_schedule.parquet"

    @property
    def flush_results(self) -> Path:
        return self.root / "flush_results.json"

    @property
    def resource_before(self) -> Path:
        return self.root / "resource_before.json"

    @property
    def resource_after(self) -> Path:
        return self.root / "resource_after.json"


class TrialJournal:
    """One authoritative state file plus repairable derived manifests."""

    def __init__(
        self,
        paths: TrialPaths,
        state: TrialExecutionState,
        *,
        failpoint: AtomicWriteFailpoint | None = None,
        loaded: bool = False,
    ) -> None:
        self.paths = paths
        self.state = state
        self.failpoint = failpoint
        self.loaded = loaded
        self._lock = asyncio.Lock()

    @classmethod
    def create_or_load(
        cls,
        paths: TrialPaths,
        trial: TrialSpec,
        manifest: TrialManifest,
        schedule: TrialSchedule,
        *,
        wall_ms: int,
        failpoint: AtomicWriteFailpoint | None = None,
    ) -> "TrialJournal":
        if paths.state.exists():
            state = parse_trial_execution_state(
                load_json_object(paths.state, description="Trial state")
            )
            if (
                state.trial_attempt_id != manifest.trial_attempt_id
                or state.trial_id != trial.trial_id
                or state.attempt_index != manifest.attempt_index
            ):
                raise ExperimentValidationError(
                    "existing Trial state identity does not match the Study plan"
                )
            journal = cls(paths, state, failpoint=failpoint, loaded=True)
            journal._validate_or_write_schedule(schedule)
            journal._persist_derived(state)
            return journal
        if paths.root.exists() and any(paths.root.iterdir()):
            raise ExperimentValidationError(
                "Trial directory exists without an authoritative state.json"
            )
        paths.root.mkdir(parents=True, exist_ok=True)
        runs = tuple(
            TrialRunRecord(
                phase=entry.phase,
                arrival_index=entry.arrival_index,
                record_id=entry.record_id,
                input_digest=entry.input_digest,
                submission_id=entry.submission_id,
                scheduled_offset_ms=entry.scheduled_offset_ms,
            )
            for entry in (*schedule.warmup, *schedule.measurement)
        )
        state = TrialExecutionState(
            schema_version=1,
            trial_attempt_id=manifest.trial_attempt_id,
            trial_id=trial.trial_id,
            attempt_index=manifest.attempt_index,
            state="planned",
            revision=0,
            created_at_wall_ms=wall_ms,
            updated_at_wall_ms=wall_ms,
            warmup_started_monotonic_ms=None,
            measurement_started_monotonic_ms=None,
            drain_deadline_monotonic_ms=None,
            runs=runs,
        )
        journal = cls(paths, state, failpoint=failpoint)
        atomic_write_json(paths.root / "trial_spec.json", trial.canonical_payload())
        journal._validate_or_write_schedule(schedule)
        journal._persist(state)
        return journal

    async def transition(self, target: str, *, wall_ms: int) -> TrialExecutionState:
        async with self._lock:
            return self._commit(self.state.transition(target, wall_ms=wall_ms))

    async def revise(self, *, wall_ms: int, **changes: object) -> TrialExecutionState:
        async with self._lock:
            return self._commit(self.state.revised(wall_ms=wall_ms, **changes))

    async def mutate_run(
        self,
        phase: str,
        arrival_index: int,
        update: Callable[[TrialRunRecord], TrialRunRecord],
        *,
        wall_ms: int,
    ) -> TrialRunRecord:
        async with self._lock:
            current = self.state.run(phase, arrival_index)
            changed = update(current)
            self._commit(self.state.replace_run(changed, wall_ms=wall_ms))
            return changed

    async def add_invalid_reason(self, reason: str, *, wall_ms: int) -> None:
        if not reason:
            raise ExperimentValidationError("invalid reason cannot be empty")
        async with self._lock:
            if reason in self.state.invalid_reasons:
                return
            self._commit(
                self.state.revised(
                    wall_ms=wall_ms,
                    invalid_reasons=(*self.state.invalid_reasons, reason),
                )
            )

    def _commit(self, state: TrialExecutionState) -> TrialExecutionState:
        self._persist(state)
        self.state = state
        return state

    def _persist(self, state: TrialExecutionState) -> None:
        atomic_write_json(
            self.paths.state,
            state.canonical_payload(),
            failpoint=self.failpoint,
        )
        self._persist_derived(state)

    def _persist_derived(self, state: TrialExecutionState) -> None:
        run_ids = tuple(item.run_id for item in state.runs if item.run_id is not None)
        committed_files = tuple(
            dict.fromkeys(
                path
                for result in state.flush_results
                for path in result.committed_files
            )
        )
        manifest = TrialManifest(
            schema_version=1,
            trial_attempt_id=state.trial_attempt_id,
            trial_id=state.trial_id,
            attempt_index=state.attempt_index,
            state=state.state,
            run_ids=run_ids,
            experiment_ids=run_ids,
            committed_files=committed_files,
        )
        atomic_write_json(
            self.paths.trial_manifest,
            manifest.canonical_payload(),
            failpoint=self.failpoint,
        )
        atomic_write_json(
            self.paths.run_manifest,
            state.run_manifest_payload(),
            failpoint=self.failpoint,
        )
        atomic_write_json(
            self.paths.flush_results,
            {
                "schema_version": 1,
                "trial_attempt_id": state.trial_attempt_id,
                "results": [item.canonical_payload() for item in state.flush_results],
            },
            failpoint=self.failpoint,
        )
        if state.resource_before is not None:
            atomic_write_json(
                self.paths.resource_before,
                state.resource_before.canonical_payload(),
                failpoint=self.failpoint,
            )
        if state.resource_after is not None:
            atomic_write_json(
                self.paths.resource_after,
                {
                    **state.resource_after.canonical_payload(),
                    "recovery": (
                        None
                        if state.recovery is None
                        else state.recovery.canonical_payload()
                    ),
                },
                failpoint=self.failpoint,
            )

    def _validate_or_write_schedule(self, schedule: TrialSchedule) -> None:
        if self.paths.schedule.exists():
            validate_schedule_parquet(self.paths.schedule, schedule)
        else:
            write_schedule_parquet(self.paths.schedule, schedule)


class TrialOrchestrator:
    def __init__(
        self,
        *,
        runtime_factory: BenchmarkRuntimeFactory,
        clock: BenchmarkClock,
        failpoint: AtomicWriteFailpoint | None = None,
    ) -> None:
        self.runtime_factory = runtime_factory
        self.clock = clock
        self.failpoint = failpoint

    async def execute(
        self,
        *,
        plan: StudyPlan,
        cell: CellSpec,
        trial: TrialSpec,
        paths: TrialPaths,
        attempt_index: int = 0,
    ) -> TrialExecutionState:
        dataset = load_workload_dataset(plan.spec.workload)
        trace = self._load_trace(plan.spec)
        manifest = TrialManifest.planned(trial, attempt_index=attempt_index)
        schedule = materialize_trial_schedule(
            plan.spec,
            trial,
            manifest.trial_attempt_id,
            dataset,
            trace=trace,
        )
        journal = TrialJournal.create_or_load(
            paths,
            trial,
            manifest,
            schedule,
            wall_ms=self.clock.wall_ms(),
            failpoint=self.failpoint,
        )
        if journal.state.terminal:
            return journal.state
        if journal.state.state == "planned":
            await journal.transition("preparing", wall_ms=self.clock.wall_ms())
        runtime: BenchmarkRuntimeClient | None = None
        try:
            workflow = load_workflow(
                plan.spec.workload,
                config=cast(Mapping[str, object], thaw(cell.config_snapshot.resolved)),
            )
            runtime = await self.runtime_factory.open(
                spec=plan.spec,
                cell=cell,
                trial_attempt_id=manifest.trial_attempt_id,
                trial_directory=str(paths.root),
                resume=journal.loaded,
            )
            records = {item.record_id: item for item in dataset.records}
            while journal.state.state not in TERMINAL_TRIAL_STATES:
                state = journal.state.state
                if state == "preparing":
                    await self._prepare(runtime, journal)
                elif state == "warming":
                    await self._warm(
                        runtime,
                        workflow,
                        plan.spec,
                        schedule,
                        records,
                        journal,
                    )
                elif state == "measuring":
                    await self._measure(
                        runtime,
                        workflow,
                        plan.spec,
                        schedule,
                        records,
                        journal,
                    )
                elif state == "draining":
                    await self._drain(runtime, plan.spec, journal)
                elif state == "flushing":
                    await self._flush(runtime, plan.spec, journal)
                else:
                    raise ExperimentValidationError(f"unhandled Trial state: {state}")
            return journal.state
        except asyncio.CancelledError as exc:
            await asyncio.shield(
                self._abort_after_exception(runtime, plan.spec, journal, exc)
            )
            raise
        except Exception as exc:
            # Failpoints model an abrupt process death, so they must keep the
            # durable resume behavior rather than becoming a handled failure.
            if self.failpoint is not None:
                raise
            return await self._abort_after_exception(runtime, plan.spec, journal, exc)

    async def _abort_after_exception(
        self,
        runtime: BenchmarkRuntimeClient | None,
        spec: ExperimentSpec,
        journal: TrialJournal,
        primary_error: BaseException,
    ) -> TrialExecutionState:
        error_text = _exception_text(primary_error)
        reasons = journal.state.invalid_reasons
        if "trial_execution_failed" not in reasons:
            reasons = (*reasons, "trial_execution_failed")
        cleanup_errors: list[tuple[str, str]] = []
        try:
            await journal.revise(
                wall_ms=self.clock.wall_ms(),
                invalid_reasons=reasons,
                last_error=error_text,
            )
        except Exception as exc:
            cleanup_errors.append(("persist_primary_error", _exception_text(exc)))

        if runtime is None:
            await journal.add_invalid_reason(
                "resource_recovery_unverified", wall_ms=self.clock.wall_ms()
            )
            await journal.transition("aborted", wall_ms=self.clock.wall_ms())
            return journal.state

        before = journal.state.resource_before
        if before is None:
            try:
                before = await runtime.resource_snapshot()
                await journal.revise(
                    wall_ms=self.clock.wall_ms(), resource_before=before
                )
            except Exception as exc:
                cleanup_errors.append(
                    ("resource_snapshot_before", _exception_text(exc))
                )

        cleanup_deadline = self.clock.monotonic_ms() + min(
            spec.windows.drain_deadline_ms, 10_000
        )
        for current in journal.state.runs:
            if current.run_id is None or current.terminal:
                continue
            try:
                await runtime.cancel_run(
                    current.run_id,
                    request_id=_request_id(
                        journal.state.trial_attempt_id, "cancel", current.run_id
                    ),
                )
            except Exception as exc:
                cleanup_errors.append(
                    (f"cancel:{current.run_id}", _exception_text(exc))
                )

        for current in journal.state.runs:
            if current.run_id is None or current.terminal:
                continue
            run_id = current.run_id
            try:
                terminal_result = await runtime.wait_terminal(
                    run_id,
                    deadline_monotonic_ms=cleanup_deadline,
                )
                if terminal_result.run_id != run_id:
                    raise ExperimentValidationError(
                        "terminal result Run ID mismatch during cleanup"
                    )
                terminal_at = self.clock.monotonic_ms()
                await journal.mutate_run(
                    current.phase,
                    current.arrival_index,
                    lambda item: replace(
                        item,
                        terminal_status=terminal_result.status,
                        terminal_at_monotonic_ms=terminal_at,
                    ),
                    wall_ms=self.clock.wall_ms(),
                )
            except Exception as exc:
                cleanup_errors.append((f"wait_terminal:{run_id}", _exception_text(exc)))

        for current in journal.state.runs:
            if current.run_id is None or current.terminal_status is None:
                continue
            run_id = current.run_id
            if not current.flushed:
                try:
                    flush_result = await runtime.flush_run(
                        run_id,
                        request_id=_request_id(
                            journal.state.trial_attempt_id, "flush", run_id
                        ),
                    )
                    await self._record_flush(journal, current, flush_result)
                    current = journal.state.run(current.phase, current.arrival_index)
                    if not flush_result.recording_complete:
                        await journal.add_invalid_reason(
                            "recording_incomplete", wall_ms=self.clock.wall_ms()
                        )
                except Exception as exc:
                    cleanup_errors.append((f"flush:{run_id}", _exception_text(exc)))
            if not current.destroyed:
                try:
                    await runtime.destroy_run(
                        run_id,
                        request_id=_request_id(
                            journal.state.trial_attempt_id, "destroy", run_id
                        ),
                        force=True,
                    )
                    await journal.mutate_run(
                        current.phase,
                        current.arrival_index,
                        lambda item: replace(item, destroyed=True),
                        wall_ms=self.clock.wall_ms(),
                    )
                except Exception as exc:
                    cleanup_errors.append((f"destroy:{run_id}", _exception_text(exc)))

        run_ids = tuple(
            item.run_id for item in journal.state.runs if item.run_id is not None
        )
        after: ResourceSnapshot | None = journal.state.resource_after
        recovery: ResourceRecoveryResult | None = journal.state.recovery
        if before is not None:
            try:
                after, recovery = await runtime.wait_for_recovery(
                    before,
                    run_ids=run_ids,
                    deadline_monotonic_ms=cleanup_deadline,
                )
            except Exception as exc:
                cleanup_errors.append(("wait_for_recovery", _exception_text(exc)))
                try:
                    after = await runtime.resource_snapshot()
                except Exception as snapshot_exc:
                    cleanup_errors.append(
                        ("resource_snapshot_after", _exception_text(snapshot_exc))
                    )
                if after is not None:
                    recovery = ResourceRecoveryResult.create(
                        recovered=False,
                        checked_at_wall_ms=self.clock.wall_ms(),
                        reason_code="resource_recovery_failed",
                        details={"wait_for_recovery_error": _exception_text(exc)},
                    )
            if after is not None and recovery is not None:
                try:
                    await journal.revise(
                        wall_ms=self.clock.wall_ms(),
                        resource_after=after,
                        recovery=recovery,
                    )
                except Exception as exc:
                    cleanup_errors.append(
                        ("persist_pre_shutdown_recovery", _exception_text(exc))
                    )

        try:
            shutdown = await runtime.shutdown(
                request_id=_request_id(
                    journal.state.trial_attempt_id,
                    "shutdown",
                    journal.state.trial_attempt_id,
                )
            )
            if (
                shutdown.get("timed_out") is True
                or shutdown.get("cleanup_confirmed") is False
            ):
                cleanup_errors.append(
                    ("shutdown", "Runtime shutdown did not confirm cleanup")
                )
        except Exception as exc:
            cleanup_errors.append(("shutdown", _exception_text(exc)))

        if before is not None and after is not None and recovery is not None:
            try:
                after, recovery = await runtime.finalize_recovery(
                    before,
                    after,
                    recovery,
                    deadline_monotonic_ms=(
                        self.clock.monotonic_ms() + spec.windows.drain_deadline_ms
                    ),
                )
            except Exception as exc:
                cleanup_errors.append(("finalize_recovery", _exception_text(exc)))

        if recovery is not None:
            critical_cleanup_failure = any(
                stage
                in {
                    "resource_snapshot_before",
                    "resource_snapshot_after",
                    "wait_for_recovery",
                    "shutdown",
                    "finalize_recovery",
                }
                for stage, _ in cleanup_errors
            )
            recovered = recovery.recovered and not critical_cleanup_failure
            recovery = ResourceRecoveryResult.create(
                recovered=recovered,
                checked_at_wall_ms=self.clock.wall_ms(),
                reason_code=(
                    None
                    if recovered
                    else recovery.reason_code or "trial_cleanup_failed"
                ),
                details={
                    "runtime_recovery": recovery.canonical_payload(),
                    "cleanup_errors": [
                        {"stage": stage, "error": error}
                        for stage, error in cleanup_errors
                    ],
                    "primary_error": error_text,
                },
            )
            assert after is not None
            final_reasons = journal.state.invalid_reasons
            if "trial_execution_failed" not in final_reasons:
                final_reasons = (*final_reasons, "trial_execution_failed")
            if not recovery.recovered:
                recovery_reason = recovery.reason_code or "trial_cleanup_failed"
                if recovery_reason not in final_reasons:
                    final_reasons = (*final_reasons, recovery_reason)
            await journal.revise(
                wall_ms=self.clock.wall_ms(),
                resource_after=after,
                recovery=recovery,
                invalid_reasons=final_reasons,
                last_error=error_text,
            )
        else:
            await journal.add_invalid_reason(
                "resource_recovery_unverified", wall_ms=self.clock.wall_ms()
            )

        await journal.transition("aborted", wall_ms=self.clock.wall_ms())
        return journal.state

    def _load_trace(self, spec: ExperimentSpec) -> TraceSchedule | None:
        if spec.arrival.trace_input is None:
            return None
        return load_trace_schedule(spec.workload, spec.arrival.trace_input)

    async def _prepare(
        self, runtime: BenchmarkRuntimeClient, journal: TrialJournal
    ) -> None:
        if journal.state.resource_before is None:
            await runtime.prepare_trial()
            before = await runtime.resource_snapshot()
            await journal.revise(
                wall_ms=self.clock.wall_ms(),
                resource_before=before,
            )
        await journal.transition("warming", wall_ms=self.clock.wall_ms())

    async def _warm(
        self,
        runtime: BenchmarkRuntimeClient,
        workflow: object,
        spec: ExperimentSpec,
        schedule: TrialSchedule,
        records: Mapping[str, WorkloadRecord],
        journal: TrialJournal,
    ) -> None:
        if journal.state.warmup_started_monotonic_ms is None:
            await journal.revise(
                wall_ms=self.clock.wall_ms(),
                warmup_started_monotonic_ms=self.clock.monotonic_ms(),
            )
        if spec.windows.warmup_runs:
            for entry in schedule.warmup:
                await self._run_to_terminal(
                    runtime, workflow, spec, entry, records, journal
                )
        elif schedule.warmup:
            assert journal.state.warmup_started_monotonic_ms is not None
            await self._issue_open(
                runtime,
                workflow,
                spec,
                schedule.warmup,
                records,
                journal,
                base_monotonic_ms=journal.state.warmup_started_monotonic_ms,
            )
            await self._wait_phase_terminal(
                runtime,
                spec,
                journal,
                phase="warmup",
                deadline_monotonic_ms=(
                    self.clock.monotonic_ms() + spec.windows.drain_deadline_ms
                ),
            )
        await journal.transition("measuring", wall_ms=self.clock.wall_ms())

    async def _measure(
        self,
        runtime: BenchmarkRuntimeClient,
        workflow: object,
        spec: ExperimentSpec,
        schedule: TrialSchedule,
        records: Mapping[str, WorkloadRecord],
        journal: TrialJournal,
    ) -> None:
        if journal.state.measurement_started_monotonic_ms is None:
            await journal.revise(
                wall_ms=self.clock.wall_ms(),
                measurement_started_monotonic_ms=self.clock.monotonic_ms(),
            )
        if spec.arrival.mode == "closed_loop":
            assert spec.arrival.concurrency is not None
            await self._execute_closed_loop(
                runtime,
                workflow,
                spec,
                schedule.measurement,
                records,
                journal,
                concurrency=spec.arrival.concurrency,
            )
        else:
            assert journal.state.measurement_started_monotonic_ms is not None
            await self._issue_open(
                runtime,
                workflow,
                spec,
                schedule.measurement,
                records,
                journal,
                base_monotonic_ms=journal.state.measurement_started_monotonic_ms,
            )
        deadline = self.clock.monotonic_ms() + spec.windows.drain_deadline_ms
        await journal.revise(
            wall_ms=self.clock.wall_ms(),
            drain_deadline_monotonic_ms=deadline,
        )
        await journal.transition("draining", wall_ms=self.clock.wall_ms())

    async def _drain(
        self,
        runtime: BenchmarkRuntimeClient,
        spec: ExperimentSpec,
        journal: TrialJournal,
    ) -> None:
        deadline = journal.state.drain_deadline_monotonic_ms
        if deadline is None:
            raise ExperimentValidationError("draining Trial has no absolute deadline")
        await self._wait_phase_terminal(
            runtime,
            spec,
            journal,
            phase="measurement",
            deadline_monotonic_ms=deadline,
        )
        await journal.transition("flushing", wall_ms=self.clock.wall_ms())

    async def _flush(
        self,
        runtime: BenchmarkRuntimeClient,
        spec: ExperimentSpec,
        journal: TrialJournal,
    ) -> None:
        for current in journal.state.runs:
            if current.run_id is None or current.terminal_status is None:
                continue
            if not current.flushed:
                result = await runtime.flush_run(
                    current.run_id,
                    request_id=_request_id(
                        journal.state.trial_attempt_id, "flush", current.run_id
                    ),
                )
                await self._record_flush(journal, current, result)
                current = journal.state.run(current.phase, current.arrival_index)
                if not result.recording_complete:
                    await journal.add_invalid_reason(
                        "recording_incomplete", wall_ms=self.clock.wall_ms()
                    )
            if not current.destroyed:
                assert current.run_id is not None
                await runtime.destroy_run(
                    current.run_id,
                    request_id=_request_id(
                        journal.state.trial_attempt_id, "destroy", current.run_id
                    ),
                    force=current.recording_complete is not True,
                )
                await journal.mutate_run(
                    current.phase,
                    current.arrival_index,
                    lambda item: replace(item, destroyed=True),
                    wall_ms=self.clock.wall_ms(),
                )
        before = journal.state.resource_before
        if before is None:
            raise ExperimentValidationError("flushing Trial has no resource baseline")
        run_ids = tuple(
            item.run_id for item in journal.state.runs if item.run_id is not None
        )
        if journal.state.resource_after is None or journal.state.recovery is None:
            after, recovery = await runtime.wait_for_recovery(
                before,
                run_ids=run_ids,
                deadline_monotonic_ms=(
                    self.clock.monotonic_ms() + spec.windows.drain_deadline_ms
                ),
            )
            await journal.revise(
                wall_ms=self.clock.wall_ms(),
                resource_after=after,
                recovery=recovery,
            )
        assert journal.state.resource_after is not None
        assert journal.state.recovery is not None
        shutdown = await runtime.shutdown(
            request_id=_request_id(
                journal.state.trial_attempt_id,
                "shutdown",
                journal.state.trial_attempt_id,
            )
        )
        if (
            shutdown.get("timed_out") is True
            or shutdown.get("cleanup_confirmed") is False
        ):
            await journal.add_invalid_reason(
                "safe_shutdown_failed", wall_ms=self.clock.wall_ms()
            )
        final_after, final_recovery = await runtime.finalize_recovery(
            before,
            journal.state.resource_after,
            journal.state.recovery,
            deadline_monotonic_ms=(
                self.clock.monotonic_ms() + spec.windows.drain_deadline_ms
            ),
        )
        await journal.revise(
            wall_ms=self.clock.wall_ms(),
            resource_after=final_after,
            recovery=final_recovery,
        )
        if not final_recovery.recovered:
            await journal.add_invalid_reason(
                final_recovery.reason_code or "resource_recovery_failed",
                wall_ms=self.clock.wall_ms(),
            )
        terminal = "invalid" if journal.state.invalid_reasons else "valid"
        await journal.transition(terminal, wall_ms=self.clock.wall_ms())

    async def _record_flush(
        self,
        journal: TrialJournal,
        current: TrialRunRecord,
        result: RunFlushResult,
    ) -> None:
        if result.run_id != current.run_id:
            raise ExperimentValidationError("flush result Run ID mismatch")
        results = tuple(
            item for item in journal.state.flush_results if item.run_id != result.run_id
        ) + (result,)
        await journal.revise(
            wall_ms=self.clock.wall_ms(),
            flush_results=results,
        )
        await journal.mutate_run(
            current.phase,
            current.arrival_index,
            lambda item: replace(
                item,
                flushed=True,
                recording_complete=result.recording_complete,
            ),
            wall_ms=self.clock.wall_ms(),
        )

    async def _execute_closed_loop(
        self,
        runtime: BenchmarkRuntimeClient,
        workflow: object,
        spec: ExperimentSpec,
        entries: tuple[ArrivalEntry, ...],
        records: Mapping[str, WorkloadRecord],
        journal: TrialJournal,
        *,
        concurrency: int,
    ) -> None:
        pending = [
            entry
            for entry in entries
            if not journal.state.run(entry.phase, entry.arrival_index).terminal
        ]
        active: set[asyncio.Task[None]] = set()
        cursor = 0
        try:
            while cursor < len(pending) or active:
                while cursor < len(pending) and len(active) < concurrency:
                    entry = pending[cursor]
                    current = journal.state.run(entry.phase, entry.arrival_index)
                    if current.issued_at_monotonic_ms is None:
                        issued = self.clock.monotonic_ms()
                        await journal.mutate_run(
                            entry.phase,
                            entry.arrival_index,
                            lambda item: replace(
                                item,
                                offered_at_monotonic_ms=issued,
                                issued_at_monotonic_ms=issued,
                            ),
                            wall_ms=self.clock.wall_ms(),
                        )
                    await self._submit_existing(
                        runtime,
                        workflow,
                        spec,
                        entry,
                        records,
                        journal,
                    )
                    current = journal.state.run(entry.phase, entry.arrival_index)
                    if current.run_id is not None and not current.terminal:
                        active.add(
                            asyncio.create_task(
                                self._wait_one_terminal(
                                    runtime,
                                    spec,
                                    journal,
                                    current,
                                    deadline_monotonic_ms=(
                                        self.clock.monotonic_ms()
                                        + spec.windows.drain_deadline_ms
                                    ),
                                )
                            )
                        )
                    cursor += 1
                if not active:
                    continue
                done, active = await asyncio.wait(
                    active, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    await task
        finally:
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)

    async def _issue_open(
        self,
        runtime: BenchmarkRuntimeClient,
        workflow: object,
        spec: ExperimentSpec,
        entries: tuple[ArrivalEntry, ...],
        records: Mapping[str, WorkloadRecord],
        journal: TrialJournal,
        *,
        base_monotonic_ms: int,
    ) -> None:
        tasks: list[asyncio.Task[None]] = []
        issued_times = (
            [
                item.issued_at_monotonic_ms
                for item in journal.state.runs
                if item.phase == entries[0].phase
                and item.issued_at_monotonic_ms is not None
            ]
            if entries
            else []
        )
        last_issued = max(issued_times, default=None)
        spacing = catch_up_spacing_ms(spec.arrival)
        for entry in entries:
            current = journal.state.run(entry.phase, entry.arrival_index)
            if current.run_id is not None or current.submission_error is not None:
                continue
            if current.issued_at_monotonic_ms is None:
                assert entry.scheduled_offset_ms is not None
                scheduled = base_monotonic_ms + entry.scheduled_offset_ms
                now = self.clock.monotonic_ms()
                effective_deadline = scheduled
                if now > scheduled and last_issued is not None and spacing:
                    effective_deadline = max(now, last_issued + spacing)
                await self.clock.wait_until(effective_deadline)
                issued = self.clock.monotonic_ms()
                await journal.mutate_run(
                    entry.phase,
                    entry.arrival_index,
                    lambda item: _mark_issued(item, scheduled, issued),
                    wall_ms=self.clock.wall_ms(),
                )
                last_issued = issued
            tasks.append(
                asyncio.create_task(
                    self._submit_existing(
                        runtime,
                        workflow,
                        spec,
                        entry,
                        records,
                        journal,
                    )
                )
            )
        if tasks:
            await _gather_all(tasks)

    async def _run_to_terminal(
        self,
        runtime: BenchmarkRuntimeClient,
        workflow: object,
        spec: ExperimentSpec,
        entry: ArrivalEntry,
        records: Mapping[str, WorkloadRecord],
        journal: TrialJournal,
    ) -> None:
        current = journal.state.run(entry.phase, entry.arrival_index)
        if current.terminal or current.submission_error is not None:
            return
        if current.issued_at_monotonic_ms is None:
            issued = self.clock.monotonic_ms()
            await journal.mutate_run(
                entry.phase,
                entry.arrival_index,
                lambda item: replace(
                    item,
                    offered_at_monotonic_ms=issued,
                    issued_at_monotonic_ms=issued,
                ),
                wall_ms=self.clock.wall_ms(),
            )
        await self._submit_existing(runtime, workflow, spec, entry, records, journal)
        current = journal.state.run(entry.phase, entry.arrival_index)
        if current.run_id is None or current.terminal:
            return
        await self._wait_one_terminal(
            runtime,
            spec,
            journal,
            current,
            deadline_monotonic_ms=(
                self.clock.monotonic_ms() + spec.windows.drain_deadline_ms
            ),
        )

    async def _submit_existing(
        self,
        runtime: BenchmarkRuntimeClient,
        workflow: object,
        spec: ExperimentSpec,
        entry: ArrivalEntry,
        records: Mapping[str, WorkloadRecord],
        journal: TrialJournal,
    ) -> None:
        current = journal.state.run(entry.phase, entry.arrival_index)
        if current.run_id is not None or current.submission_error is not None:
            return
        record = records.get(entry.record_id)
        if record is None or record.input_digest != entry.input_digest:
            raise ExperimentValidationError("arrival input identity changed")
        receipt = None
        for attempt in range(2):
            try:
                receipt = await runtime.submit(
                    workflow,
                    inputs=record.materialize_inputs(),
                    submission_id=entry.submission_id,
                    run_deadline_ms=spec.windows.drain_deadline_ms,
                )
                break
            except (TimeoutError, ConnectionError, OSError):
                if attempt == 1:
                    raise
                await asyncio.sleep(0)
        assert receipt is not None
        if receipt.submission_id != entry.submission_id:
            raise ExperimentValidationError("submission receipt ID mismatch")
        admitted = self.clock.monotonic_ms()
        if receipt.state == "aborted":
            await journal.mutate_run(
                entry.phase,
                entry.arrival_index,
                lambda item: replace(
                    item,
                    submission_replayed=receipt.replayed,
                    submission_error=receipt.error or "submission_aborted",
                ),
                wall_ms=self.clock.wall_ms(),
            )
            await journal.add_invalid_reason(
                "submission_aborted", wall_ms=self.clock.wall_ms()
            )
            return
        assert receipt.run_id is not None
        await journal.mutate_run(
            entry.phase,
            entry.arrival_index,
            lambda item: replace(
                item,
                admitted_at_monotonic_ms=admitted,
                run_id=receipt.run_id,
                submission_replayed=receipt.replayed,
            ),
            wall_ms=self.clock.wall_ms(),
        )

    async def _wait_phase_terminal(
        self,
        runtime: BenchmarkRuntimeClient,
        spec: ExperimentSpec,
        journal: TrialJournal,
        *,
        phase: str,
        deadline_monotonic_ms: int,
    ) -> None:
        tasks: list[asyncio.Task[None]] = []
        for current in journal.state.runs:
            if current.phase != phase or current.terminal:
                continue
            if current.run_id is None:
                if (
                    current.submission_error is None
                    and current.issued_at_monotonic_ms is not None
                ):
                    raise ExperimentValidationError(
                        "issued submission has no receipt; resume must replay it first"
                    )
                continue
            tasks.append(
                asyncio.create_task(
                    self._wait_one_terminal(
                        runtime,
                        spec,
                        journal,
                        current,
                        deadline_monotonic_ms=deadline_monotonic_ms,
                    )
                )
            )
        if tasks:
            await _gather_all(tasks)

    async def _wait_one_terminal(
        self,
        runtime: BenchmarkRuntimeClient,
        spec: ExperimentSpec,
        journal: TrialJournal,
        current: TrialRunRecord,
        *,
        deadline_monotonic_ms: int,
    ) -> None:
        assert current.run_id is not None
        try:
            result = await runtime.wait_terminal(
                current.run_id,
                deadline_monotonic_ms=deadline_monotonic_ms,
            )
        except TimeoutError:
            await journal.add_invalid_reason(
                "drain_deadline_exceeded", wall_ms=self.clock.wall_ms()
            )
            await runtime.cancel_run(
                current.run_id,
                request_id=_request_id(
                    journal.state.trial_attempt_id, "cancel", current.run_id
                ),
            )
            result = await runtime.wait_terminal(
                current.run_id,
                deadline_monotonic_ms=(
                    self.clock.monotonic_ms() + spec.windows.drain_deadline_ms
                ),
            )
        if result.run_id != current.run_id:
            raise ExperimentValidationError("terminal result Run ID mismatch")
        terminal_at = self.clock.monotonic_ms()
        await journal.mutate_run(
            current.phase,
            current.arrival_index,
            lambda item: replace(
                item,
                terminal_status=result.status,
                terminal_at_monotonic_ms=terminal_at,
            ),
            wall_ms=self.clock.wall_ms(),
        )


@dataclass(frozen=True, slots=True)
class StudyExecutionResult:
    study_id: str
    study_directory: str
    state: str
    completed_trials: int
    blocked_reason: str | None

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "study_id": self.study_id,
            "study_directory": self.study_directory,
            "state": self.state,
            "completed_trials": self.completed_trials,
            "blocked_reason": self.blocked_reason,
        }


async def run_study(
    spec_path: str | Path,
    *,
    runtime_factory: BenchmarkRuntimeFactory,
    output_root: str | Path = "experiment_output",
    clock_factory: Callable[[], BenchmarkClock] = SystemBenchmarkClock,
    failpoint: AtomicWriteFailpoint | None = None,
) -> StudyExecutionResult:
    source = Path(spec_path).expanduser().resolve(strict=True)
    plan = load_study_plan(source)
    study_directory = (
        Path(output_root).expanduser().resolve(strict=False) / plan.spec.study_id
    )
    manifest_path = study_directory / "study_manifest.json"
    if manifest_path.exists():
        raise ExperimentValidationError(
            f"Study already exists; use maze-bench resume: {study_directory}"
        )
    study_directory.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(
        study_directory / "study_spec.canonical.json",
        plan.spec.canonical_bytes + b"\n",
    )
    atomic_write_bytes(
        study_directory / "study_plan.json", plan.canonical_bytes + b"\n"
    )
    manifest = _new_study_manifest(plan, source)
    atomic_write_json(manifest_path, manifest, failpoint=failpoint)
    return await _continue_study(
        plan,
        study_directory,
        manifest,
        runtime_factory=runtime_factory,
        clock_factory=clock_factory,
        failpoint=failpoint,
    )


async def resume_study(
    study_directory: str | Path,
    *,
    runtime_factory: BenchmarkRuntimeFactory,
    clock_factory: Callable[[], BenchmarkClock] = SystemBenchmarkClock,
    failpoint: AtomicWriteFailpoint | None = None,
) -> StudyExecutionResult:
    root = Path(study_directory).expanduser().resolve(strict=True)
    manifest = load_json_object(
        root / "study_manifest.json", description="Study manifest"
    )
    _validate_study_manifest(manifest)
    source = Path(cast(str, manifest["source_spec_path"]))
    if file_sha256(source) != manifest["source_spec_sha256"]:
        raise ExperimentValidationError("ExperimentSpec source changed before resume")
    plan = load_study_plan(source)
    if (
        plan.spec.study_id != manifest["study_id"]
        or canonical_json_digest(plan.canonical_payload()) != manifest["plan_sha256"]
    ):
        raise ExperimentValidationError("Study plan identity changed before resume")
    return await _continue_study(
        plan,
        root,
        manifest,
        runtime_factory=runtime_factory,
        clock_factory=clock_factory,
        failpoint=failpoint,
    )


async def _continue_study(
    plan: StudyPlan,
    root: Path,
    manifest: Mapping[str, object],
    *,
    runtime_factory: BenchmarkRuntimeFactory,
    clock_factory: Callable[[], BenchmarkClock],
    failpoint: AtomicWriteFailpoint | None,
) -> StudyExecutionResult:
    completed = _audit_study_trials(plan, root, manifest)
    if runtime_factory.analysis_after_each_trial:
        _ensure_analysis_history(root, completed)
    if manifest.get("state") == "completed":
        if len(completed) != len(plan.trials):
            raise ExperimentValidationError(
                "completed Study does not contain every frozen Trial"
            )
        return _study_result(root, manifest)
    if manifest.get("state") == "blocked":
        raise ExperimentValidationError(
            "Study is blocked by failed resource recovery; controlled cleanup is required"
        )
    cell_by_id = {cell.cell_id: cell for cell in plan.cells}
    mutable = dict(manifest)
    mutable["state"] = "running"
    for trial in plan.trials:
        existing = completed.get(trial.trial_id)
        if existing is not None and existing.get("state") in TERMINAL_TRIAL_STATES:
            continue
        attempt = TrialManifest.planned(trial)
        trial_root = _trial_root(root, trial, attempt.trial_attempt_id)
        orchestrator = TrialOrchestrator(
            runtime_factory=runtime_factory,
            clock=clock_factory(),
            failpoint=failpoint,
        )
        result = await orchestrator.execute(
            plan=plan,
            cell=cell_by_id[trial.cell_id],
            trial=trial,
            paths=TrialPaths(trial_root),
        )
        completed[trial.trial_id] = {
            "trial_id": trial.trial_id,
            "trial_attempt_id": result.trial_attempt_id,
            "state": result.state,
            "relative_directory": str(trial_root.relative_to(root)),
            "invalid_reasons": list(result.invalid_reasons),
        }
        mutable["trials"] = [
            completed[item.trial_id]
            for item in plan.trials
            if item.trial_id in completed
        ]
        mutable["completed_trials"] = len(completed)
        recovery = result.recovery
        blocking_reason = None
        if recovery is not None and not recovery.recovered:
            blocking_reason = recovery.reason_code or "resource_recovery_failed"
        elif "safe_shutdown_failed" in result.invalid_reasons:
            blocking_reason = "safe_shutdown_failed"
        elif "resource_recovery_unverified" in result.invalid_reasons:
            blocking_reason = "resource_recovery_unverified"
        if blocking_reason is not None:
            mutable["state"] = "blocked"
            mutable["blocked_reason"] = blocking_reason
            atomic_write_json(
                root / "study_manifest.json", mutable, failpoint=failpoint
            )
            if runtime_factory.analysis_after_each_trial:
                _run_analysis_pipeline(root, result.trial_attempt_id)
            return _study_result(root, mutable)
        atomic_write_json(root / "study_manifest.json", mutable, failpoint=failpoint)
        if runtime_factory.analysis_after_each_trial:
            _run_analysis_pipeline(root, result.trial_attempt_id)
    mutable["state"] = "completed"
    mutable["blocked_reason"] = None
    atomic_write_json(root / "study_manifest.json", mutable, failpoint=failpoint)
    return _study_result(root, mutable)


def _run_analysis_pipeline(root: Path, trial_attempt_id: str) -> None:
    """Commit the only formal analysis path after every completed Trial."""

    from ascend_maze.benchmark.aggregation import aggregate_study
    from ascend_maze.benchmark.importer import validate_study
    from ascend_maze.benchmark.reporting import report_study

    validation = validate_study(root)
    aggregate_study(root)
    report = report_study(root)
    aggregate_manifest = load_json_object(
        root / "aggregates" / "manifest.json",
        description="aggregate manifest",
    )
    atomic_write_json(
        root / "analysis_history" / f"{trial_attempt_id}.json",
        {
            "schema_version": 1,
            "trial_attempt_id": trial_attempt_id,
            "pipeline": ("validate", "aggregate", "report"),
            "validation_digest": validation["validation_digest"],
            "aggregate_manifest_digest": canonical_json_digest(aggregate_manifest),
            "report_digest": report["content_digest"],
        },
    )


def _ensure_analysis_history(
    root: Path, completed: Mapping[str, Mapping[str, object]]
) -> None:
    for entry in completed.values():
        trial_attempt_id = cast(str, entry["trial_attempt_id"])
        path = root / "analysis_history" / f"{trial_attempt_id}.json"
        if not path.exists():
            _run_analysis_pipeline(root, trial_attempt_id)
            continue
        payload = load_json_object(path, description="Trial analysis history")
        if (
            payload.get("schema_version") != 1
            or payload.get("trial_attempt_id") != trial_attempt_id
            or payload.get("pipeline") != ["validate", "aggregate", "report"]
            or any(
                not isinstance(payload.get(name), str) or not payload.get(name)
                for name in (
                    "validation_digest",
                    "aggregate_manifest_digest",
                    "report_digest",
                )
            )
        ):
            raise ExperimentValidationError(
                f"Trial analysis history is invalid: {trial_attempt_id}"
            )


def _new_study_manifest(plan: StudyPlan, source: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "schema": STUDY_MANIFEST_SCHEMA,
        "study_id": plan.spec.study_id,
        "state": "planned",
        "source_spec_path": str(source),
        "source_spec_sha256": file_sha256(source),
        "plan_sha256": canonical_json_digest(plan.canonical_payload()),
        "trial_count": len(plan.trials),
        "completed_trials": 0,
        "trials": [],
        "blocked_reason": None,
    }


def _validate_study_manifest(manifest: Mapping[str, object]) -> None:
    required = {
        "schema_version",
        "schema",
        "study_id",
        "state",
        "source_spec_path",
        "source_spec_sha256",
        "plan_sha256",
        "trial_count",
        "completed_trials",
        "trials",
        "blocked_reason",
    }
    if set(manifest) != required or manifest.get("schema") != STUDY_MANIFEST_SCHEMA:
        raise ExperimentValidationError("Study manifest schema is invalid")
    if manifest.get("state") not in {"planned", "running", "completed", "blocked"}:
        raise ExperimentValidationError("Study manifest state is invalid")
    for name in ("study_id", "source_spec_path", "source_spec_sha256", "plan_sha256"):
        if not isinstance(manifest.get(name), str) or not manifest[name]:
            raise ExperimentValidationError(f"Study manifest {name} is invalid")
    for name in ("trial_count", "completed_trials"):
        value = manifest.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ExperimentValidationError(f"Study manifest {name} is invalid")
    blocked_reason = manifest.get("blocked_reason")
    if blocked_reason is not None and (
        not isinstance(blocked_reason, str) or not blocked_reason
    ):
        raise ExperimentValidationError("Study manifest blocked_reason is invalid")
    if (manifest.get("state") == "blocked") != (blocked_reason is not None):
        raise ExperimentValidationError(
            "Study blocked state and blocked_reason do not match"
        )


def _completed_trials(
    manifest: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    raw = manifest.get("trials")
    if not isinstance(raw, list):
        raise ExperimentValidationError("Study manifest trials must be an array")
    result: dict[str, Mapping[str, object]] = {}
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {
            "trial_id",
            "trial_attempt_id",
            "state",
            "relative_directory",
            "invalid_reasons",
        }:
            raise ExperimentValidationError("Study manifest Trial entry is invalid")
        if not isinstance(item.get("trial_id"), str):
            raise ExperimentValidationError("Study manifest Trial ID is invalid")
        trial_id = cast(str, item["trial_id"])
        if trial_id in result:
            raise ExperimentValidationError("Study manifest has duplicate Trials")
        result[trial_id] = cast(Mapping[str, object], item)
    return result


def _audit_study_trials(
    plan: StudyPlan,
    root: Path,
    manifest: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    if manifest.get("trial_count") != len(plan.trials):
        raise ExperimentValidationError("Study manifest trial_count changed")
    completed = _completed_trials(manifest)
    if manifest.get("completed_trials") != len(completed):
        raise ExperimentValidationError("Study manifest completed_trials changed")
    trial_by_id = {item.trial_id: item for item in plan.trials}
    unknown = sorted(set(completed) - set(trial_by_id))
    if unknown:
        raise ExperimentValidationError(
            f"Study manifest contains an unknown Trial: {unknown[0]}"
        )
    for trial_id, entry in completed.items():
        trial = trial_by_id[trial_id]
        expected_attempt = TrialManifest.planned(trial).trial_attempt_id
        if entry.get("trial_attempt_id") != expected_attempt:
            raise ExperimentValidationError("Study manifest Trial attempt changed")
        state_name = entry.get("state")
        if state_name not in TERMINAL_TRIAL_STATES:
            raise ExperimentValidationError(
                "Study manifest can only index terminal Trial attempts"
            )
        expected_root = _trial_root(root, trial, expected_attempt)
        relative = entry.get("relative_directory")
        if not isinstance(relative, str) or not relative:
            raise ExperimentValidationError("Study manifest Trial directory is invalid")
        recorded_root = (root / relative).resolve(strict=False)
        if recorded_root != expected_root.resolve(strict=False):
            raise ExperimentValidationError("Study manifest Trial path changed")
        state = parse_trial_execution_state(
            load_json_object(expected_root / "state.json", description="Trial state")
        )
        if (
            state.trial_id != trial_id
            or state.trial_attempt_id != expected_attempt
            or state.state != state_name
        ):
            raise ExperimentValidationError(
                "Study manifest and Trial state identities do not match"
            )
        reasons = entry.get("invalid_reasons")
        if not isinstance(reasons, list) or reasons != list(state.invalid_reasons):
            raise ExperimentValidationError(
                "Study manifest and Trial invalid reasons do not match"
            )
    return completed


def _trial_root(root: Path, trial: TrialSpec, trial_attempt_id: str) -> Path:
    return (
        root
        / "trials"
        / trial.cell_id
        / f"{trial.block_index:04d}-{trial.repetition_index:04d}"
        / trial_attempt_id
    )


def _study_result(root: Path, manifest: Mapping[str, object]) -> StudyExecutionResult:
    return StudyExecutionResult(
        study_id=cast(str, manifest["study_id"]),
        study_directory=str(root),
        state=cast(str, manifest["state"]),
        completed_trials=cast(int, manifest["completed_trials"]),
        blocked_reason=cast(str | None, manifest["blocked_reason"]),
    )


def _request_id(trial_attempt_id: str, action: str, resource_id: str) -> str:
    return stable_payload_id(
        "control_request",
        {
            "trial_attempt_id": trial_attempt_id,
            "action": action,
            "resource_id": resource_id,
        },
        length=32,
    )


async def _gather_all(tasks: list[asyncio.Task[None]]) -> None:
    """Settle every issued operation before exposing the first failure."""

    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    for result in results:
        if isinstance(result, BaseException):
            raise result


def _exception_text(exc: BaseException) -> str:
    name = f"{type(exc).__module__}.{type(exc).__qualname__}"
    message = str(exc).strip()
    return name if not message else f"{name}: {message}"


def _mark_issued(item: TrialRunRecord, scheduled: int, issued: int) -> TrialRunRecord:
    return replace(
        item,
        scheduled_at_monotonic_ms=scheduled,
        offered_at_monotonic_ms=issued,
        issued_at_monotonic_ms=issued,
        arrival_lateness_ms=max(0, issued - scheduled),
    )
