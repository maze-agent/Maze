"""Measured C7/C8/C12/C13 microbenchmarks with standard C14 artifacts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from time import perf_counter_ns
from typing import Mapping, Sequence

from ascend_maze.benchmark.canonical import canonical_json_digest, stable_payload_id
from ascend_maze.benchmark.c13_runtime import measure_local_control_overhead
from ascend_maze.benchmark.contracts import CellSpec, ExperimentSpec
from ascend_maze.benchmark.orchestrator import StudyExecutionResult, run_study
from ascend_maze.benchmark.persistence import atomic_write_bytes, load_json_object
from ascend_maze.benchmark.planning import file_sha256
from ascend_maze.benchmark.runtime import (
    BenchmarkRuntimeClient,
    BenchmarkRuntimeFactory,
    ResourceRecoveryResult,
    ResourceSnapshot,
    RunFlushResult,
    SubmissionReceipt,
    TerminalRunResult,
)
from ascend_maze.benchmark.workloads.component import (
    build as build_microbenchmark_workflow,
)
from ascend_maze.contracts.recording import (
    ExecutionEvent,
    ParquetRecorderConfig,
    RunRecordingContext,
)
from ascend_maze.contracts.resources import ExecutionTarget, ResourceSpec
from ascend_maze.core.canonical import CanonicalValue, FrozenMap, freeze_canonical
from ascend_maze.core.clock import ManualClock
from ascend_maze.core.errors import ExperimentValidationError
from ascend_maze.lifecycle import DeadlineKind, DeadlineManager
from ascend_maze.placement import NodeCapacity, PlacementManager
from ascend_maze.recording import NoopRecorder, ParquetRecorder
from ascend_maze.resources import ResourceAnchor
from ascend_maze.scheduler import (
    HacsNoTpStaticPolicy,
    QueueToken,
    SchedulableTaskView,
    TaskKey,
)

MICROBENCHMARK_SUITES = ("c7", "c8", "c12", "c13")
_MICRO_ENVIRONMENT = canonical_json_digest(
    {"kind": "offline-component-microbenchmark", "version": 1}
)
_NO_MODEL_DIGEST = hashlib.sha256(b"no-model-artifact").hexdigest()


@dataclass(frozen=True, slots=True)
class MicrobenchmarkBundle:
    spec_paths: Mapping[str, Path]
    build_revision: str
    workflow_fingerprint: str


@dataclass(slots=True)
class _MeasuredRun:
    run_id: str
    index: int
    flushed: RunFlushResult | None = None
    destroyed: bool = False


def prepare_microbenchmark_specs(output_directory: str | Path) -> MicrobenchmarkBundle:
    root = Path(output_directory).expanduser().resolve(strict=False)
    source_root = _repository_root(Path(__file__).resolve())
    build_revision = _clean_build_revision(source_root)
    workflow_fingerprint = (
        build_microbenchmark_workflow().compile().workflow_fingerprint
    )
    spec_root = root / "microbenchmark_specs"
    config = spec_root / "performance.toml"
    dataset = spec_root / "dataset.json"
    atomic_write_bytes(config, _base_config(spec_root).encode("ascii"))
    records = [
        {"record_id": f"record-{index:03d}", "inputs": {"value": index}}
        for index in range(100)
    ]
    from ascend_maze.benchmark.persistence import atomic_write_json

    atomic_write_json(
        dataset,
        {
            "schema_version": 1,
            "workflow_fingerprint": workflow_fingerprint,
            "records": records,
        },
    )
    paths: dict[str, Path] = {}
    for suite in MICROBENCHMARK_SUITES:
        path = spec_root / f"{suite}.toml"
        atomic_write_bytes(
            path,
            _microbenchmark_spec(
                suite=suite,
                build_revision=build_revision,
                config=config,
                dataset=dataset,
                workflow_fingerprint=workflow_fingerprint,
            ).encode("ascii"),
        )
        paths[suite] = path
    return MicrobenchmarkBundle(paths, build_revision, workflow_fingerprint)


async def run_microbenchmark_suites(
    output_root: str | Path,
    *,
    suites: Sequence[str] = MICROBENCHMARK_SUITES,
) -> tuple[StudyExecutionResult, ...]:
    requested = tuple(suites)
    if not requested or any(item not in MICROBENCHMARK_SUITES for item in requested):
        raise ExperimentValidationError("unknown or empty microbenchmark suite set")
    if len(set(requested)) != len(requested):
        raise ExperimentValidationError("microbenchmark suites must be unique")
    root = Path(output_root).expanduser().resolve(strict=False)
    bundle = prepare_microbenchmark_specs(root)
    results: list[StudyExecutionResult] = []
    for suite in requested:
        result = await run_study(
            bundle.spec_paths[suite],
            runtime_factory=MicrobenchmarkRuntimeFactory(suite),
            output_root=root,
        )
        results.append(result)
    return tuple(results)


class MicrobenchmarkRuntimeFactory(BenchmarkRuntimeFactory):
    analysis_after_each_trial = True

    def __init__(self, suite: str) -> None:
        if suite not in MICROBENCHMARK_SUITES:
            raise ValueError("unsupported microbenchmark suite")
        self.suite = suite

    async def open(
        self,
        *,
        spec: ExperimentSpec,
        cell: CellSpec,
        trial_attempt_id: str,
        trial_directory: str,
        resume: bool,
    ) -> BenchmarkRuntimeClient:
        if resume:
            raise ExperimentValidationError(
                "component microbenchmark Trial resume requires a new Trial attempt"
            )
        return MeasuredMicrobenchmarkRuntime(
            suite=self.suite,
            spec=spec,
            cell=cell,
            trial_attempt_id=trial_attempt_id,
            trial_directory=Path(trial_directory),
        )


class MeasuredMicrobenchmarkRuntime(BenchmarkRuntimeClient):
    def __init__(
        self,
        *,
        suite: str,
        spec: ExperimentSpec,
        cell: CellSpec,
        trial_attempt_id: str,
        trial_directory: Path,
    ) -> None:
        self.suite = suite
        self.spec = spec
        self.cell = cell
        self.trial_attempt_id = trial_attempt_id
        self.trial_directory = trial_directory
        self.controller_generation = stable_payload_id(
            "controller_generation",
            {"trial_attempt_id": trial_attempt_id},
        )
        self._producer_id = stable_payload_id(
            "producer",
            {"trial_attempt_id": trial_attempt_id},
        )
        self.runs: dict[str, _MeasuredRun] = {}
        self.runs_by_submission: dict[str, str] = {}
        self.samples: dict[str, tuple[float, ...]] = {}
        self._producer_sequence = 0
        self._prepared = False
        self._recorder = ParquetRecorder(
            ParquetRecorderConfig(
                root_directory=str(trial_directory / "c8"),
                control_queue_capacity=32_768,
                telemetry_queue_capacity=1_024,
                batch_size=512,
                flush_interval_ms=1,
                compression="zstd",
                max_page_size=1_000,
            ),
            cursor_signing_key=b"c14e-microbenchmark-cursor-key-01",
        )

    async def prepare_trial(self) -> Mapping[str, object]:
        if self._prepared:
            return {"suite": self.suite, "cell": self.cell.name, "replayed": True}
        if self.suite == "c7":
            measured = await asyncio.to_thread(_measure_c7)
        elif self.suite == "c8":
            measured = await _measure_c8(self.cell.name)
        elif self.suite == "c12":
            measured = await asyncio.to_thread(_measure_c12, self.cell.name)
        else:
            measured = await measure_local_control_overhead(
                self.cell.name,
                self.trial_directory,
                build_microbenchmark_workflow(),
            )
        self.samples = {name: tuple(values) for name, values in measured.items()}
        if any(not values for values in self.samples.values()):
            raise ExperimentValidationError("microbenchmark produced an empty metric")
        self._prepared = True
        return {
            "suite": self.suite,
            "cell": self.cell.name,
            "sample_counts": {
                name: len(values) for name, values in sorted(self.samples.items())
            },
        }

    async def resource_snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot.create(
            captured_at_wall_ms=int(time.time() * 1_000),
            controller_generation=self.controller_generation,
            config_fingerprint=self.cell.config_snapshot.config_fingerprint,
            payload={
                "suite": self.suite,
                "cell": self.cell.name,
                "active_run_ids": sorted(
                    run_id for run_id, run in self.runs.items() if not run.destroyed
                ),
                "placement_lease_count": 0,
                "worker_lease_count": 0,
                "route_lease_count": 0,
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
        del workflow, inputs, run_deadline_ms
        existing_run_id = self.runs_by_submission.get(submission_id)
        if existing_run_id is not None:
            return SubmissionReceipt(submission_id, "committed", existing_run_id, True)
        run_id = stable_payload_id(
            "run",
            {
                "trial_attempt_id": self.trial_attempt_id,
                "submission_id": submission_id,
            },
        )
        self.runs[run_id] = _MeasuredRun(run_id, len(self.runs))
        self.runs_by_submission[submission_id] = run_id
        return SubmissionReceipt(submission_id, "committed", run_id)

    async def wait_terminal(
        self, run_id: str, *, deadline_monotonic_ms: int
    ) -> TerminalRunResult:
        del deadline_monotonic_ms
        self._run(run_id)
        return TerminalRunResult.create(
            run_id,
            "succeeded",
            {"run_id": run_id, "status": "succeeded"},
        )

    async def flush_run(self, run_id: str, *, request_id: str) -> RunFlushResult:
        del request_id
        run = self._run(run_id)
        if run.flushed is not None:
            return run.flushed
        context = RunRecordingContext(
            schema_version=1,
            experiment_id=run_id,
            run_id=run_id,
            workflow_fingerprint=self.spec.workload.workflow_fingerprint,
            config_fingerprint=self.cell.config_snapshot.config_fingerprint,
            environment_fingerprint=self.spec.workload.required_environment_fingerprint,
            build_revision=self.spec.build_revision,
            started_wall_time_ms=1_000 + run.index,
            initial_expected_producer_ids=(self._producer_id,),
        )
        self._recorder.open_run(context)
        for event in self._run_events(run):
            if not self._recorder.emit(event):
                raise ExperimentValidationError(
                    "formal microbenchmark artifact recorder dropped an event"
                )
        flushed = await self._recorder.flush_run(run_id, 60_000)
        result = RunFlushResult.create(
            run_id,
            flushed.recording_complete,
            flushed.committed_files,
            _flush_payload(flushed),
        )
        run.flushed = result
        return result

    async def cancel_run(self, run_id: str, *, request_id: str) -> None:
        del request_id
        self._run(run_id)

    async def destroy_run(
        self,
        run_id: str,
        *,
        request_id: str,
        force: bool = False,
    ) -> None:
        del request_id, force
        run = self._run(run_id)
        if run.flushed is None:
            raise ExperimentValidationError(
                "microbenchmark Run must flush before destroy"
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
        residual = tuple(
            run_id for run_id in run_ids if not self._run(run_id).destroyed
        )
        after = await self.resource_snapshot()
        return after, ResourceRecoveryResult.create(
            recovered=not residual,
            checked_at_wall_ms=int(time.time() * 1_000),
            reason_code=None if not residual else "resource_recovery_failed",
            details={"residual_run_ids": residual},
        )

    async def shutdown(self, *, request_id: str) -> Mapping[str, object]:
        del request_id
        await self._recorder.close(60_000)
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

    def _run(self, run_id: str) -> _MeasuredRun:
        try:
            return self.runs[run_id]
        except KeyError as exc:
            raise ExperimentValidationError(
                f"unknown microbenchmark Run: {run_id}"
            ) from exc

    def _run_events(self, run: _MeasuredRun) -> tuple[ExecutionEvent, ...]:
        task_id = "task_microbenchmark"
        dispatch_id = f"dispatch_{run.index}"
        placement_id = f"placement_{run.index}"
        worker_id = f"worker_{run.index}"
        route_id = f"route_{run.index}"
        model_instance_id = "model_instance_microbenchmark"
        base_time = 100_000 + run.index * 1_000
        templates: list[tuple[str, dict[str, object]]] = [
            ("run_submitted", {}),
            ("task_queued", {}),
            (
                "task_dispatched",
                {
                    "dispatch_id": dispatch_id,
                    "model_id": "microbenchmark-model",
                    "instance_generation": 1,
                },
            ),
            (
                "worker_acquired",
                {"dispatch_id": dispatch_id, "worker_lease_id": worker_id},
            ),
        ]
        metric_events: list[tuple[str, dict[str, object]]] = []
        for metric_name, values in sorted(self.samples.items()):
            for value in _sample_partition(values, run.index, 100):
                metric_events.append(
                    (
                        "microbenchmark_sample",
                        {"metric_name": metric_name, "value": value},
                    )
                )
        templates.extend(metric_events)
        templates.extend(
            (
                (
                    "model_route_released",
                    {"model_id": "microbenchmark-model", "instance_generation": 1},
                ),
                (
                    "worker_released",
                    {"dispatch_id": dispatch_id, "worker_lease_id": worker_id},
                ),
                ("task_succeeded", {"dispatch_id": dispatch_id}),
                (
                    "run_terminal",
                    {"status": "succeeded", "finished_at_ms": base_time + 100},
                ),
            )
        )
        events: list[ExecutionEvent] = []
        for index, (event_type, payload) in enumerate(templates):
            self._producer_sequence += 1
            task_scoped = event_type not in {"run_submitted", "run_terminal"}
            attempt_scoped = event_type not in {
                "run_submitted",
                "run_terminal",
                "task_queued",
            }
            route_scoped = event_type in {
                "task_dispatched",
                "model_route_released",
                "task_succeeded",
            }
            model_scoped = route_scoped
            events.append(
                ExecutionEvent(
                    schema_version=1,
                    event_id=stable_payload_id(
                        "event",
                        {
                            "trial_attempt_id": self.trial_attempt_id,
                            "run_id": run.run_id,
                            "sequence": self._producer_sequence,
                        },
                    ),
                    experiment_id=run.run_id,
                    run_id=run.run_id,
                    task_id=task_id if task_scoped else None,
                    attempt=1 if attempt_scoped else None,
                    lease_id=placement_id if attempt_scoped else None,
                    route_lease_id=route_id if route_scoped else None,
                    model_instance_id=model_instance_id if model_scoped else None,
                    event_type=event_type,
                    producer_id=self._producer_id,
                    producer_sequence=self._producer_sequence,
                    node_id=None,
                    device_id=None,
                    monotonic_time_ms=base_time + index,
                    wall_time_ms=1_000_000 + base_time + index,
                    duration_ms=None,
                    payload=_frozen_payload(payload),
                )
            )
        return tuple(events)


def _sample_partition(
    values: Sequence[float], index: int, partition_count: int
) -> tuple[float, ...]:
    if not values:
        raise ExperimentValidationError("cannot partition empty microbenchmark samples")
    start = len(values) * index // partition_count
    end = len(values) * (index + 1) // partition_count
    if start == end:
        return (values[index % len(values)],)
    return tuple(values[start:end])


def _measure_c7() -> dict[str, tuple[float, ...]]:
    clock = ManualClock(monotonic_ms=120_000)
    policy = HacsNoTpStaticPolicy(clock=clock, scheduler_epoch_ms=0)
    anchor = ResourceAnchor(
        definition_id="definition_microbenchmark",
        task_kind="cpu",
        execution_target=ExecutionTarget.LOCAL_WORKER,
        declared=ResourceSpec(cpu_num=1, mem_mb=64, npu_mem_mb=0, io_num=0),
        static_inferred=ResourceSpec(cpu_num=1, mem_mb=64, npu_mem_mb=0, io_num=0),
        learned=None,
        effective=ResourceSpec(cpu_num=1, mem_mb=64, npu_mem_mb=0, io_num=0),
        model_id=None,
        profile_key="c7-pressure",
        revision=1,
        strategy="static",
    )
    for index in range(10_000):
        run_id = f"run_{index:05d}"
        policy.register_run(run_id=run_id, submitted_at_ms=index, total_value_tasks=1)
        policy.enqueue(
            "cpu",
            SchedulableTaskView(
                queue_token=QueueToken(TaskKey(run_id, f"task_{index:05d}"), 1),
                task_kind="cpu",
                ready_at_ms=index,
                queued_at_ms=index,
                enqueue_sequence=index,
                depth_from_entry=0,
                depth_to_exit=index % 8,
                resource_anchor=anchor,
            ),
        )
    placement = PlacementManager()
    placement.register_node(
        NodeCapacity(
            node_id="node_microbenchmark",
            boot_id="boot_microbenchmark",
            node_ip="127.0.0.1",
            cpu_total=64,
            mem_total_mb=1_048_576,
            cpu_system_reserved=0,
            mem_system_reserved_mb=0,
            io_slots_total=64,
            observed_free_mem_mb=1_048_576,
        )
    )
    policy_samples: list[float] = []
    total_samples: list[float] = []
    for _ in range(10_000):
        started = perf_counter_ns()
        proposal = policy.propose("cpu", 1)[0]
        policy_elapsed_ms = (perf_counter_ns() - started) / 1_000_000
        policy_ms = max(0.0, policy_elapsed_ms - proposal.score_compute_ms)
        placement_started = perf_counter_ns()
        selected = placement.try_reserve(
            run_id=proposal.task_key.run_id,
            task_id=proposal.task_key.task_id,
            attempt=1,
            anchor=anchor,
            now_ms=clock.monotonic_ms(),
            dispatch_deadline_ms=clock.monotonic_ms() + 1_000,
        )
        placement_ms = (perf_counter_ns() - placement_started) / 1_000_000
        if selected.lease is None:
            raise ExperimentValidationError("C7 microbenchmark placement failed")
        placement.release_lease(
            selected.lease.lease_id,
            now_ms=clock.monotonic_ms(),
            run_id=proposal.task_key.run_id,
            task_id=proposal.task_key.task_id,
            attempt=1,
            reason="microbenchmark",
        )
        placement.destroy_run_context(proposal.task_key.run_id)
        policy_samples.append(policy_ms)
        total_samples.append(proposal.score_compute_ms + policy_ms + placement_ms)
    return {
        "scheduler_policy_select_ms": tuple(policy_samples),
        "scheduler_total_ms": tuple(total_samples),
    }


async def _measure_c8(cell_name: str) -> dict[str, tuple[float, ...]]:
    if cell_name not in {"noop", "parquet"}:
        raise ExperimentValidationError("C8 microbenchmark Cell is invalid")
    temporary = Path(tempfile.mkdtemp(prefix="ascend-maze-c8-"))
    recorder = (
        NoopRecorder()
        if cell_name == "noop"
        else ParquetRecorder(
            ParquetRecorderConfig(
                root_directory=str(temporary),
                control_queue_capacity=4_096,
                telemetry_queue_capacity=256,
                batch_size=128,
                flush_interval_ms=1_000,
                compression="zstd",
                max_page_size=1_000,
            ),
            cursor_signing_key=b"c14e-c8-measurement-cursor-key-01",
        )
    )
    dct: list[float] = []
    throughput: list[float] = []
    run_ids: list[str] = []
    sequence = 0
    try:
        for sample in range(100):
            run_id = f"c8_measurement_{sample:03d}"
            run_ids.append(run_id)
            context = RunRecordingContext(
                schema_version=1,
                experiment_id=run_id,
                run_id=run_id,
                workflow_fingerprint="w" * 64,
                config_fingerprint="c" * 64,
                environment_fingerprint="e" * 64,
                build_revision="microbenchmark",
                started_wall_time_ms=sample,
                initial_expected_producer_ids=("controller",),
            )
            started = perf_counter_ns()
            recorder.open_run(context)
            for event_index in range(16):
                time.sleep(0.001)
                sequence += 1
                accepted = recorder.emit(
                    ExecutionEvent(
                        schema_version=1,
                        event_id=f"c8_event_{sequence}",
                        experiment_id=run_id,
                        run_id=run_id,
                        task_id=None,
                        attempt=None,
                        lease_id=None,
                        route_lease_id=None,
                        model_instance_id=None,
                        event_type="microbenchmark_payload",
                        producer_id="controller",
                        producer_sequence=sequence,
                        node_id=None,
                        device_id=None,
                        monotonic_time_ms=sequence,
                        wall_time_ms=sequence,
                        duration_ms=None,
                        payload=_frozen_payload({"event_index": event_index}),
                    )
                )
                if not accepted:
                    raise ExperimentValidationError(
                        "C8 measured recorder dropped an event"
                    )
            elapsed_ms = (perf_counter_ns() - started) / 1_000_000
            dct.append(elapsed_ms)
            throughput.append(16_000.0 / elapsed_ms)
        for run_id in run_ids:
            flushed = await recorder.flush_run(run_id, 30_000)
            if not flushed.recording_complete:
                raise ExperimentValidationError(
                    "C8 measured recorder reported drop/gap/writer error"
                )
    finally:
        await recorder.close(30_000)
        shutil.rmtree(temporary, ignore_errors=True)
    return {
        "dct_ms": tuple(dct),
        "throughput_success_per_s": tuple(throughput),
    }


def _measure_c12(cell_name: str) -> dict[str, tuple[float, ...]]:
    if cell_name not in {"no_fault_reference", "fault_bookkeeping"}:
        raise ExperimentValidationError("C12 microbenchmark Cell is invalid")
    with_bookkeeping = cell_name == "fault_bookkeeping"
    dct: list[float] = []
    throughput: list[float] = []
    for sample in range(100):
        deadlines = DeadlineManager()
        states: dict[tuple[str, str, int], str] = {}
        started = perf_counter_ns()
        for index in range(2_000):
            run_id = f"run_{sample:03d}_{index:04d}"
            task_id = f"task_{index:04d}"
            key = (run_id, task_id, 1)
            states[key] = "running"
            if with_bookkeeping:
                deadlines.register(
                    kind=DeadlineKind.LEASE,
                    run_id=run_id,
                    task_id=task_id,
                    attempt=1,
                    due_at_ms=index + 1_000,
                )
                deadlines.register(
                    kind=DeadlineKind.TASK,
                    run_id=run_id,
                    task_id=task_id,
                    attempt=1,
                    due_at_ms=index + 2_000,
                )
            states[key] = "succeeded"
            if with_bookkeeping:
                deadlines.cancel(
                    kind=DeadlineKind.LEASE,
                    run_id=run_id,
                    task_id=task_id,
                    attempt=1,
                )
                deadlines.cancel(
                    kind=DeadlineKind.TASK,
                    run_id=run_id,
                    task_id=task_id,
                    attempt=1,
                )
        elapsed_ms = (perf_counter_ns() - started) / 1_000_000
        if len(states) != 2_000 or any(
            value != "succeeded" for value in states.values()
        ):
            raise ExperimentValidationError("C12 event replay state diverged")
        if with_bookkeeping and deadlines.active_count != 0:
            raise ExperimentValidationError("C12 no-fault timers were not cleared")
        dct.append(elapsed_ms / 2_000)
        throughput.append(2_000_000.0 / elapsed_ms)
    return {
        "dct_ms": tuple(dct),
        "throughput_success_per_s": tuple(throughput),
    }


def _frozen_payload(
    payload: Mapping[str, object],
) -> FrozenMap[CanonicalValue, CanonicalValue]:
    frozen = freeze_canonical(payload)
    if not isinstance(frozen, FrozenMap):
        raise ExperimentValidationError(
            "microbenchmark event payload must be a mapping"
        )
    return frozen


def _flush_payload(result: object) -> dict[str, object]:
    from ascend_maze.contracts.recording import FlushResult

    if not isinstance(result, FlushResult):
        raise TypeError("microbenchmark flush result has an invalid type")
    return {
        "run_id": result.run_id,
        "committed_files": result.committed_files,
        "dropped_control_event_count": result.dropped_control_event_count,
        "dropped_telemetry_count": result.dropped_telemetry_count,
        "sequence_gap_count": result.sequence_gap_count,
        "missing_producer_count": result.missing_producer_count,
        "writer_errors": result.writer_errors,
        "recording_complete": result.recording_complete,
        "flush_duration_ms": result.flush_duration_ms,
    }


def _base_config(root: Path) -> str:
    runtime = root / "runtime"
    return "\n".join(
        (
            "schema_version = 1",
            'profile = "performance"',
            "",
            "[control]",
            f'runtime_directory = "{runtime}"',
            "watch_retention_count = 20000",
            "",
            "[runtime.ray]",
            'namespace = "c14e-microbenchmark"',
            f'temp_directory = "{root / "ray"}"',
            "",
            "[cluster]",
            f'environment_fingerprint = "{_MICRO_ENVIRONMENT}"',
            "",
            "[scheduler]",
            'policy = "hacs_no_tp"',
            'partitioner = "heterogeneous"',
            "",
            "[placement]",
            'anchor_strategy = "static"',
            "task_slots_total = 2",
            "allow_colocation = true",
            "",
            "[worker]",
            "max_tasks_per_worker = 1",
            "standby_min_idle = 1",
            "standby_max_idle = 1",
            "",
            "[recording]",
            'backend = "noop"',
            f'root_directory = "{root / "records"}"',
            "",
            "[fault]",
            "retry_backoff_ms = 100",
            "max_retries_default = 0",
            "",
        )
    )


def _microbenchmark_spec(
    *,
    suite: str,
    build_revision: str,
    config: Path,
    dataset: Path,
    workflow_fingerprint: str,
) -> str:
    metric_sets = {
        "c7": ("scheduler_policy_select_ms", "scheduler_total_ms"),
        "c8": ("dct_ms", "throughput_success_per_s"),
        "c12": ("dct_ms", "throughput_success_per_s"),
        "c13": (
            "dct_ms",
            "scheduling_order_match",
            "throughput_success_per_s",
        ),
    }
    lines = [
        "schema_version = 1",
        f'study_name = "c14e-{suite}-microbenchmark"',
        'study_kind = "formal"',
        "base_seed = 1405002",
        "block_count = 10",
        "repetition_count = 1",
        f'build_revision = "{build_revision}"',
        f'base_config = "{config}"',
        f'base_config_sha256 = "{file_sha256(config)}"',
        "",
        "[workload]",
        f'name = "{suite}-microbenchmark"',
        'workflow_factory = "ascend_maze.benchmark.workloads.component:build"',
        f'workflow_fingerprint = "{workflow_fingerprint}"',
        'model_catalog_revision = "no-model-catalog"',
        f'model_artifact_digest = "{_NO_MODEL_DIGEST}"',
        f'required_environment_fingerprint = "{_MICRO_ENVIRONMENT}"',
        "",
        "[[workload.inputs]]",
        'logical_name = "dataset"',
        f'path = "{dataset}"',
        f'sha256 = "{file_sha256(dataset)}"',
        f"size_bytes = {dataset.stat().st_size}",
        "",
        "[arrival]",
        'mode = "closed_loop"',
        "concurrency = 1",
        "",
        "[windows]",
        "warmup_runs = 0",
        "warmup_duration_ms = 0",
        "measurement_run_count = 100",
        "measurement_duration_ms = 0",
        "drain_deadline_ms = 60000",
        "",
        "[analysis]",
        "metric_set = [" + ", ".join(f'"{name}"' for name in metric_sets[suite]) + "]",
        'validity_policy = "c14_v1"',
        'statistics_policy = "c14_v1"',
        'performance_budget_set = "c14_v1"',
        'quantile_method = "hyndman_fan_type_7"',
        "bootstrap_samples = 10000",
        "confidence_level = 0.95",
        "familywise_confidence_level = 0.9875",
        "automatic_outlier_removal = false",
        "",
        "[matrix]",
        'kind = "custom_v1"',
    ]
    lines.extend(_matrix_lines(suite))
    return "\n".join(lines) + "\n"


def _matrix_lines(suite: str) -> tuple[str, ...]:
    if suite == "c7":
        return (
            'baseline_cell = "scheduler_pressure"',
            "factors = []",
            "",
            "[[matrix.cells]]",
            'name = "scheduler_pressure"',
            "factors = []",
            "confirmatory = true",
        )
    if suite == "c8":
        return (
            'baseline_cell = "noop"',
            "",
            "[[matrix.factors]]",
            'name = "recording_backend"',
            'allowed_paths = ["recording.backend"]',
            "",
            "[[matrix.cells]]",
            'name = "noop"',
            "factors = []",
            "confirmatory = true",
            "",
            "[[matrix.cells]]",
            'name = "parquet"',
            'factors = ["recording_backend"]',
            "confirmatory = true",
            "",
            "[[matrix.cells.overrides]]",
            'path = "recording.backend"',
            'value = "parquet"',
        )
    if suite == "c12":
        return (
            'baseline_cell = "no_fault_reference"',
            "",
            "[[matrix.factors]]",
            'name = "c12_mode"',
            'allowed_paths = ["benchmark.c12_bookkeeping"]',
            "",
            "[[matrix.cells]]",
            'name = "no_fault_reference"',
            "factors = []",
            "confirmatory = true",
            "",
            "[[matrix.cells]]",
            'name = "fault_bookkeeping"',
            'factors = ["c12_mode"]',
            "confirmatory = true",
            "",
            "[[matrix.cells.overrides]]",
            'path = "benchmark.c12_bookkeeping"',
            "value = true",
        )
    return (
        'baseline_cell = "no_client"',
        "",
        "[[matrix.factors]]",
        'name = "read_clients"',
        'allowed_paths = ["benchmark.c13_read_clients"]',
        "",
        "[[matrix.cells]]",
        'name = "no_client"',
        "factors = []",
        "confirmatory = true",
        "",
        "[[matrix.cells]]",
        'name = "single_watch"',
        'factors = ["read_clients"]',
        "confirmatory = true",
        "",
        "[[matrix.cells.overrides]]",
        'path = "benchmark.c13_read_clients"',
        "value = 1",
        "",
        "[[matrix.cells]]",
        'name = "eight_read_clients"',
        'factors = ["read_clients"]',
        "confirmatory = true",
        "",
        "[[matrix.cells.overrides]]",
        'path = "benchmark.c13_read_clients"',
        "value = 8",
    )


def _repository_root(start: Path) -> Path:
    working_directory = start if start.is_dir() else start.parent
    try:
        completed = subprocess.run(
            (
                "git",
                "-C",
                str(working_directory),
                "rev-parse",
                "--show-toplevel",
            ),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExperimentValidationError(
            f"cannot locate source repository: {exc}"
        ) from exc
    return Path(completed.stdout.strip()).resolve(strict=True)


def _clean_build_revision(repository: Path) -> str:
    try:
        status = subprocess.run(
            (
                "git",
                "-C",
                str(repository),
                "status",
                "--porcelain",
                "--untracked-files=no",
            ),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        ).stdout
        revision = subprocess.run(
            ("git", "-C", str(repository), "rev-parse", "HEAD"),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExperimentValidationError(
            f"cannot freeze source revision: {exc}"
        ) from exc
    if status.strip():
        raise ExperimentValidationError(
            "tracked worktree must be clean before formal microbenchmarks"
        )
    return revision


def microbenchmark_result_payload(
    results: Sequence[StudyExecutionResult],
) -> dict[str, object]:
    studies: list[dict[str, object]] = []
    for result in results:
        report = load_json_object(
            Path(result.study_directory) / "report" / "report.v1.json",
            description="microbenchmark report",
        )
        studies.append(
            {
                "study_id": result.study_id,
                "study_directory": result.study_directory,
                "state": result.state,
                "report_digest": report["content_digest"],
            }
        )
    return {"schema_version": 1, "studies": studies}
