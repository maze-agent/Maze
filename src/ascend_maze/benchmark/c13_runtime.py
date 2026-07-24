"""Formal C14 adapter implemented only with C13 public surfaces."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import secrets
import shutil
import stat
import sys
from time import perf_counter_ns
from typing import BinaryIO, cast

from ascend_maze.api.workflow import Workflow
from ascend_maze.benchmark.admission import (
    AscendAdmissionGate,
    capture_host_resources,
    host_recovery_issues,
)
from ascend_maze.benchmark.canonical import thaw
from ascend_maze.benchmark.contracts import (
    BENCHMARK_OVERRIDE_PATHS,
    CellSpec,
    ExperimentSpec,
)
from ascend_maze.benchmark.persistence import atomic_write_json
from ascend_maze.benchmark.runtime import (
    BenchmarkRuntimeClient,
    BenchmarkRuntimeFactory,
    ResourceRecoveryResult,
    ResourceSnapshot,
    RunFlushResult,
    SubmissionReceipt,
    TerminalRunResult,
)
from ascend_maze.compiler.ir import CompiledWorkflow
from ascend_maze.config import load_config, load_model_catalog
from ascend_maze.control import InMemoryController, RuntimeClient
from ascend_maze.control.local_rpc import (
    ControllerStatus,
    LocalControlServer,
    UdsRuntimeClient,
)
from ascend_maze.core.errors import ExperimentValidationError
from ascend_maze.core.time import monotonic_time_ms, wall_time_ms
from ascend_maze.placement import NodeCapacity
from ascend_maze.recording import InMemoryRecorder


class C13BenchmarkRuntimeFactory(BenchmarkRuntimeFactory):
    analysis_after_each_trial = True

    def __init__(
        self,
        *,
        maze_command: Sequence[str] | None = None,
        startup_timeout_seconds: float = 60.0,
        admission_gate: AscendAdmissionGate | None = None,
    ) -> None:
        command = tuple(maze_command or _default_maze_command())
        if not command or any(not item for item in command):
            raise ValueError("maze command must be a non-empty argv")
        if startup_timeout_seconds <= 0:
            raise ValueError("Controller startup timeout must be positive")
        self.maze_command = command
        self.startup_timeout_seconds = startup_timeout_seconds
        self.admission_gate = admission_gate

    async def open(
        self,
        *,
        spec: ExperimentSpec,
        cell: CellSpec,
        trial_attempt_id: str,
        trial_directory: str,
        resume: bool,
    ) -> BenchmarkRuntimeClient:
        benchmark_overrides = tuple(
            item.path
            for item in cell.overrides
            if item.path in BENCHMARK_OVERRIDE_PATHS
        )
        if benchmark_overrides:
            raise ExperimentValidationError(
                "C13 Runtime cannot execute benchmark-only overrides: "
                + ", ".join(benchmark_overrides)
            )
        root = Path(trial_directory).resolve(strict=False)
        root.mkdir(parents=True, exist_ok=True)
        override_path = root / "controller_config_overrides.json"
        override_payload = {
            "schema_version": 1,
            "schema": "ascend-maze.controller-config-overrides.v1",
            "build_revision": spec.build_revision,
            "expected_config_fingerprint": cell.config_snapshot.config_fingerprint,
            "overrides": [item.canonical_payload() for item in cell.overrides],
        }
        if override_path.exists():
            from ascend_maze.benchmark.persistence import load_json_object

            if dict(
                load_json_object(override_path, description="config overrides")
            ) != (override_payload):
                raise ExperimentValidationError(
                    "Controller config overrides changed during resume"
                )
        else:
            atomic_write_json(override_path, override_payload)
        loaded = load_config(
            spec.base_config_path,
            build_revision=spec.build_revision,
            config_overrides=tuple(
                (item.path, thaw(item.value)) for item in cell.overrides
            ),
        )
        if (
            loaded.snapshot.config_fingerprint
            != cell.config_snapshot.config_fingerprint
        ):
            raise ExperimentValidationError(
                "managed Controller ConfigSnapshot does not match the frozen Cell"
            )
        host_baseline: Mapping[str, object] | None = None
        if self.admission_gate is not None:
            evidence = self.admission_gate.admit(
                spec,
                study_directory=_find_study_root(root),
            )
            host_baseline = evidence.host_baseline
        _prepare_runtime_security(
            loaded.config.control.runtime_directory,
            loaded.config.control.cluster_token_file,
        )
        model_readiness: list[tuple[str, int, int]] = []
        catalog_path = loaded.config.inference.model_catalog_path
        if catalog_path is not None:
            catalog = load_model_catalog(
                catalog_path,
                environment_fingerprint=spec.workload.required_environment_fingerprint,
            )
            model_readiness.extend(
                (
                    model.model_id,
                    max(1, model.min_replicas),
                    model.startup_timeout_ms,
                )
                for model in catalog.specs
            )
        client = RuntimeClient(
            Path(loaded.config.control.socket_path),
            max_inline_control_bytes=loaded.config.control.max_inline_control_bytes,
            shared_filesystem_roots=loaded.config.data.shared_filesystem_roots,
        )
        client.config_fingerprint = cell.config_snapshot.config_fingerprint
        process: asyncio.subprocess.Process | None = None
        stdout: BinaryIO | None = None
        stderr: BinaryIO | None = None
        started_marker = root / "controller_started.json"
        matched = await _controller_matches(
            client,
            expected_build_revision=spec.build_revision,
            expected_environment=spec.workload.required_environment_fingerprint,
            expected_config_fingerprint=cell.config_snapshot.config_fingerprint,
        )
        if not matched:
            stdout = (root / "controller.stdout.log").open("ab", buffering=0)
            stderr = (root / "controller.stderr.log").open("ab", buffering=0)
            env = os.environ.copy()
            env["ASCEND_MAZE_BUILD_REVISION"] = spec.build_revision
            argv = (
                *self.maze_command,
                "controller",
                "start",
                "--config",
                spec.base_config_path,
                "--config-overrides",
                str(override_path),
            )
            if not started_marker.exists():
                argv = (*argv, "--fresh-recovery")
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=stdout,
                stderr=stderr,
                env=env,
                start_new_session=True,
            )
            deadline = asyncio.get_running_loop().time() + self.startup_timeout_seconds
            while not await _controller_matches(
                client,
                expected_build_revision=spec.build_revision,
                expected_environment=spec.workload.required_environment_fingerprint,
                expected_config_fingerprint=cell.config_snapshot.config_fingerprint,
            ):
                if process.returncode is not None:
                    stdout.close()
                    stderr.close()
                    raise ExperimentValidationError(
                        f"managed Controller exited during startup: {process.returncode}"
                    )
                if asyncio.get_running_loop().time() >= deadline:
                    process.terminate()
                    await process.wait()
                    stdout.close()
                    stderr.close()
                    raise TimeoutError("managed Controller startup deadline expired")
                await asyncio.sleep(0.05)
        if not started_marker.exists():
            atomic_write_json(
                started_marker,
                {
                    "schema_version": 1,
                    "trial_attempt_id": trial_attempt_id,
                    "resumed_open": resume,
                    "config_fingerprint": cell.config_snapshot.config_fingerprint,
                },
            )
        return C13BenchmarkRuntime(
            client,
            process=process,
            stdout=stdout,
            stderr=stderr,
            shutdown_drain_timeout_ms=loaded.config.control.shutdown_drain_timeout_ms,
            model_readiness=tuple(model_readiness),
            standby_min_idle=loaded.config.worker.standby_min_idle,
            preparation_timeout_ms=max(
                [
                    loaded.config.worker.binding_deadline_ms,
                    *[item[2] for item in model_readiness],
                ]
            ),
            host_baseline=host_baseline,
            hbm_recovery_tolerance_mb=(loaded.config.worker.hbm_recovery_tolerance_mb),
        )


class C13BenchmarkRuntime(BenchmarkRuntimeClient):
    def __init__(
        self,
        client: RuntimeClient,
        *,
        process: asyncio.subprocess.Process | None,
        stdout: BinaryIO | None,
        stderr: BinaryIO | None,
        shutdown_drain_timeout_ms: int,
        model_readiness: tuple[tuple[str, int, int], ...] = (),
        standby_min_idle: int = 0,
        preparation_timeout_ms: int = 300_000,
        host_baseline: Mapping[str, object] | None = None,
        hbm_recovery_tolerance_mb: int = 64,
    ) -> None:
        self.client = client
        self.process = process
        self.stdout = stdout
        self.stderr = stderr
        self.shutdown_drain_timeout_ms = shutdown_drain_timeout_ms
        self.model_readiness = model_readiness
        self.standby_min_idle = standby_min_idle
        self.preparation_timeout_ms = preparation_timeout_ms
        self.host_baseline = host_baseline
        self.hbm_recovery_tolerance_mb = hbm_recovery_tolerance_mb
        self._shutdown_result: Mapping[str, object] | None = None

    async def prepare_trial(self) -> Mapping[str, object]:
        ready_models: list[dict[str, object]] = []
        for model_id, replicas, startup_timeout_ms in self.model_readiness:
            result = await self.client.wait_model_ready(
                model_id,
                replicas=replicas,
                timeout_seconds=max(1.0, startup_timeout_ms / 1_000),
            )
            ready_models.append(
                {"model_id": model_id, "replicas": replicas, "result": result}
            )
        standby = await self._wait_standby_ready()
        return {"models": ready_models, "standby": standby}

    async def _wait_standby_ready(self) -> Mapping[str, object]:
        if self.standby_min_idle == 0:
            return {"target_per_profile": 0, "ready": True}
        deadline = monotonic_time_ms() + self.preparation_timeout_ms
        last: Mapping[str, object] = {}
        while True:
            last = await self.client.query("GetWorkerPools")
            pool = _mapping(last.get("worker_pool"), "Worker Pool snapshot")
            raw_workers = pool.get("workers")
            if not isinstance(raw_workers, list):
                raise ExperimentValidationError("Worker Pool workers are invalid")
            ready_by_profile = {"cpu": 0, "io": 0, "npu_host": 0}
            for raw in raw_workers:
                worker = _mapping(raw, "Worker snapshot")
                profile = worker.get("profile")
                state = worker.get("state")
                if profile in ready_by_profile and state == "idle":
                    ready_by_profile[cast(str, profile)] += 1
            if all(
                value >= self.standby_min_idle for value in ready_by_profile.values()
            ):
                return {
                    "target_per_profile": self.standby_min_idle,
                    "ready_by_profile": ready_by_profile,
                    "ready": True,
                }
            if monotonic_time_ms() >= deadline:
                raise TimeoutError(
                    "Standby Worker watermarks were not ready before Trial warmup: "
                    f"{ready_by_profile}"
                )
            await asyncio.sleep(0.1)

    async def resource_snapshot(self) -> ResourceSnapshot:
        status = await self.client.get_controller_status()
        system, cluster, workers, models, recorder = await asyncio.gather(
            self.client.query("GetSystemSnapshot"),
            self.client.query("GetClusterSnapshot", filter="resources"),
            self.client.query("GetWorkerPools"),
            self.client.query("GetModelInstances"),
            self.client.query("GetRecorderStatus"),
        )
        meta = _mapping(system.get("meta"), "system snapshot meta")
        fingerprint = _string(meta.get("config_fingerprint"), "config fingerprint")
        host_audit = (
            None
            if self.host_baseline is None
            else await asyncio.to_thread(capture_host_resources)
        )
        return ResourceSnapshot.create(
            captured_at_wall_ms=wall_time_ms(),
            controller_generation=status.controller_generation,
            config_fingerprint=fingerprint,
            payload={
                "controller_status": {
                    "build_revision": status.build_revision,
                    "environment_fingerprint": status.environment_fingerprint,
                    "healthy_node_count": status.healthy_node_count,
                },
                "system": system,
                "cluster_resources": cluster,
                "worker_pools": workers,
                "model_instances": models,
                "recorder": recorder,
                "host_audit": host_audit,
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
        if not isinstance(workflow, (Workflow, CompiledWorkflow)):
            raise ExperimentValidationError(
                "benchmark workload is not a Workflow or CompiledWorkflow"
            )
        outcome = await self.client.submit(
            workflow,
            inputs=inputs,
            submission_id=submission_id,
            run_deadline_ms=run_deadline_ms,
        )
        state = _string(outcome.get("state"), "submission state")
        raw_run_id = outcome.get("run_id")
        run_id = None if raw_run_id is None else _string(raw_run_id, "Run ID")
        raw_error = outcome.get("error")
        return SubmissionReceipt(
            submission_id=submission_id,
            state=state,
            run_id=run_id,
            replayed=bool(outcome.get("replayed", False)),
            error=None if raw_error is None else str(raw_error),
        )

    async def wait_terminal(
        self, run_id: str, *, deadline_monotonic_ms: int
    ) -> TerminalRunResult:
        remaining_ms = deadline_monotonic_ms - monotonic_time_ms()
        if remaining_ms <= 0:
            raise TimeoutError("Run terminal deadline expired")
        async for _ in self.client.watch_run(
            run_id,
            timeout_seconds=remaining_ms / 1_000,
        ):
            pass
        shown = await self.client.query("GetRun", resource_id=run_id)
        snapshot = _mapping(shown.get("run"), "Run snapshot")
        status = _string(snapshot.get("status"), "Run status")
        return TerminalRunResult.create(run_id, status, shown)

    async def flush_run(self, run_id: str, *, request_id: str) -> RunFlushResult:
        result = await self.client.run_action("FlushRun", run_id, request_id=request_id)
        raw_files = result.get("committed_files", [])
        if not isinstance(raw_files, list) or any(
            not isinstance(path, str) or not path for path in raw_files
        ):
            raise ExperimentValidationError("C13 FlushResult files are invalid")
        recording_complete = result.get("recording_complete")
        if not isinstance(recording_complete, bool):
            raise ExperimentValidationError(
                "C13 FlushResult recording_complete is invalid"
            )
        return RunFlushResult.create(
            run_id,
            recording_complete,
            tuple(raw_files),
            result,
        )

    async def cancel_run(self, run_id: str, *, request_id: str) -> None:
        await self.client.run_action("CancelRun", run_id, request_id=request_id)

    async def destroy_run(
        self,
        run_id: str,
        *,
        request_id: str,
        force: bool = False,
    ) -> None:
        await self.client.run_action(
            "DestroyRun",
            run_id,
            request_id=request_id,
            force=force,
        )

    async def wait_for_recovery(
        self,
        before: ResourceSnapshot,
        *,
        run_ids: tuple[str, ...],
        deadline_monotonic_ms: int,
    ) -> tuple[ResourceSnapshot, ResourceRecoveryResult]:
        del before
        while True:
            snapshot = await self.resource_snapshot()
            payload = thaw(snapshot.payload)
            remaining_ids = tuple(
                run_id for run_id in run_ids if _contains_value(payload, run_id)
            )
            system = _mapping(
                _mapping(payload, "resource payload").get("system"),
                "system snapshot",
            )
            nonterminal = system.get("nonterminal_run_count")
            recovered = not remaining_ids and nonterminal == 0
            if recovered:
                return snapshot, ResourceRecoveryResult.create(
                    recovered=True,
                    checked_at_wall_ms=wall_time_ms(),
                    reason_code=None,
                    details={"remaining_run_ids": [], "nonterminal_run_count": 0},
                )
            if monotonic_time_ms() >= deadline_monotonic_ms:
                return snapshot, ResourceRecoveryResult.create(
                    recovered=False,
                    checked_at_wall_ms=wall_time_ms(),
                    reason_code="resource_recovery_failed",
                    details={
                        "remaining_run_ids": list(remaining_ids),
                        "nonterminal_run_count": nonterminal,
                    },
                )
            await asyncio.sleep(
                min(
                    0.1,
                    max(0.001, (deadline_monotonic_ms - monotonic_time_ms()) / 1_000),
                )
            )

    async def shutdown(self, *, request_id: str) -> Mapping[str, object]:
        if self._shutdown_result is not None:
            return self._shutdown_result
        normalized: dict[str, object]
        try:
            detach_error: str | None = None
            try:
                self.client.close()
            except Exception as exc:
                detach_error = _exception_text(exc)
            try:
                result = await self.client.shutdown_controller(
                    request_id=request_id,
                    drain_timeout_ms=self.shutdown_drain_timeout_ms,
                    timeout_seconds=max(
                        5.0, self.shutdown_drain_timeout_ms / 1_000 + 5
                    ),
                )
                normalized = dict(result)
            except Exception as exc:
                normalized = {
                    "cleanup_confirmed": False,
                    "timed_out": isinstance(exc, (TimeoutError, asyncio.TimeoutError)),
                    "exit_code": 1,
                    "rpc_error": _exception_text(exc),
                }
            if detach_error is not None:
                normalized["cleanup_confirmed"] = False
                normalized["client_detach_error"] = detach_error
            if self.process is not None:
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=10.0)
                except TimeoutError:
                    self.process.terminate()
                    try:
                        await asyncio.wait_for(self.process.wait(), timeout=10.0)
                    except TimeoutError:
                        self.process.kill()
                        await self.process.wait()
                    normalized["cleanup_confirmed"] = False
                    normalized["timed_out"] = True
                if self.process.returncode not in {None, 0}:
                    normalized["cleanup_confirmed"] = False
                    normalized["exit_code"] = self.process.returncode
            self._shutdown_result = normalized
            return normalized
        finally:
            if self.stdout is not None:
                self.stdout.close()
                self.stdout = None
            if self.stderr is not None:
                self.stderr.close()
                self.stderr = None

    async def finalize_recovery(
        self,
        before: ResourceSnapshot,
        after: ResourceSnapshot,
        recovery: ResourceRecoveryResult,
        *,
        deadline_monotonic_ms: int,
    ) -> tuple[ResourceSnapshot, ResourceRecoveryResult]:
        if self.host_baseline is None:
            return after, recovery
        shutdown = dict(self._shutdown_result or {})
        while True:
            host = await asyncio.to_thread(capture_host_resources)
            issues = host_recovery_issues(
                self.host_baseline,
                host,
                hbm_tolerance_mb=self.hbm_recovery_tolerance_mb,
            )
            shutdown_clean = (
                shutdown.get("cleanup_confirmed") is True
                and shutdown.get("timed_out") is not True
            )
            recovered = recovery.recovered and shutdown_clean and not issues
            final = ResourceSnapshot.create(
                captured_at_wall_ms=wall_time_ms(),
                controller_generation=after.controller_generation,
                config_fingerprint=after.config_fingerprint,
                payload={
                    "pre_shutdown_controller": thaw(after.payload),
                    "shutdown": shutdown,
                    "host_audit": host,
                },
            )
            if recovered or monotonic_time_ms() >= deadline_monotonic_ms:
                reason = None
                if not recovered:
                    reason = (
                        recovery.reason_code
                        if not recovery.recovered
                        else "safe_shutdown_failed"
                        if not shutdown_clean
                        else "host_resource_recovery_failed"
                    )
                return final, ResourceRecoveryResult.create(
                    recovered=recovered,
                    checked_at_wall_ms=wall_time_ms(),
                    reason_code=reason,
                    details={
                        "controller_recovery": recovery.canonical_payload(),
                        "shutdown_cleanup_confirmed": shutdown_clean,
                        "host_recovery_issues": issues,
                    },
                )
            await asyncio.sleep(0.25)


async def _controller_matches(
    client: RuntimeClient,
    *,
    expected_build_revision: str,
    expected_environment: str,
    expected_config_fingerprint: str,
) -> bool:
    try:
        status = await client.get_controller_status(timeout_seconds=0.5)
        if (
            status.build_revision != expected_build_revision
            or status.environment_fingerprint != expected_environment
        ):
            raise ExperimentValidationError(
                "running Controller identity does not match the frozen Study"
            )
        await client.verify_compatibility(timeout_seconds=0.5)
        system = await client.query("GetSystemSnapshot", timeout_seconds=0.5)
        meta = _mapping(system.get("meta"), "system snapshot meta")
        if meta.get("config_fingerprint") != expected_config_fingerprint:
            raise ExperimentValidationError(
                "running Controller config does not match the frozen Cell"
            )
        return True
    except ExperimentValidationError:
        raise
    except Exception:
        return False


def _default_maze_command() -> tuple[str, ...]:
    adjacent = Path(sys.executable).with_name("maze")
    if adjacent.is_file() and os.access(adjacent, os.X_OK):
        return (str(adjacent),)
    discovered = shutil.which("maze")
    if discovered is not None:
        return (discovered,)
    return (sys.executable, "-m", "ascend_maze.cli.main")


async def measure_local_control_overhead(
    cell_name: str,
    trial_directory: Path,
    workflow: Workflow,
) -> dict[str, tuple[float, ...]]:
    """Measure the protected C13 UDS path without bypassing public operations."""

    clients_by_cell = {"no_client": 0, "single_watch": 1, "eight_read_clients": 8}
    if cell_name not in clients_by_cell:
        raise ExperimentValidationError("C13 microbenchmark Cell is invalid")
    client_count = clients_by_cell[cell_name]
    socket_path = (trial_directory / "c13-control.sock").resolve(strict=False)
    recorder = InMemoryRecorder()
    controller = InMemoryController(
        config_fingerprint="c" * 64,
        environment_fingerprint="e" * 64,
        build_revision="microbenchmark",
        node_capacities=(
            NodeCapacity(
                node_id="node_c13",
                boot_id="boot_c13",
                node_ip="127.0.0.1",
                cpu_total=16,
                mem_total_mb=16_384,
                cpu_system_reserved=0,
                mem_system_reserved_mb=0,
                io_slots_total=8,
                observed_free_mem_mb=16_384,
            ),
        ),
        recorder=recorder,
    )
    await controller.start()
    server = LocalControlServer(
        socket_path=socket_path,
        status_provider=lambda: ControllerStatus(
            controller_generation=controller.controller_generation,
            build_revision=controller.build_revision,
            environment_fingerprint=controller.environment_fingerprint,
            healthy_node_count=1,
        ),
        control_api=controller,
    )
    await server.start()
    client = UdsRuntimeClient(
        socket_path,
        data_store=controller.data_store,
        data_owner_generation=controller.data_owner_generation,
    )
    submitted: list[str] = []
    dct: list[float] = []
    throughput: list[float] = []
    try:
        for index in range(100):
            started = perf_counter_ns()
            run_id = await client.run(
                workflow,
                inputs={"value": index},
                submission_id=f"c13-{cell_name}-{index:03d}",
            )
            submitted.append(run_id)
            watchers = [
                asyncio.create_task(_consume_run_watch(socket_path, run_id))
                for _ in range(client_count)
            ]
            await controller.wait_run(run_id, timeout_seconds=5)
            if watchers:
                await asyncio.gather(*watchers)
            elapsed_ms = (perf_counter_ns() - started) / 1_000_000
            dct.append(elapsed_ms)
            throughput.append(1_000.0 / elapsed_ms)
        decision_events = sorted(
            (
                event
                for run_id in submitted
                for event in recorder.events(run_id)
                if event.event_type == "scheduling_decision"
                and event.payload.get("placement_selected") is True
            ),
            key=lambda event: event.producer_sequence,
        )
        decisions = [event.run_id for event in decision_events]
        order_match = 1.0 if decisions == submitted else 0.0
        for run_id in submitted:
            await controller.destroy_run(run_id)
    finally:
        await server.close(grace_seconds=0)
        await controller.close(force=True, drain_timeout_ms=0)
    return {
        "dct_ms": tuple(dct),
        "throughput_success_per_s": tuple(throughput),
        "scheduling_order_match": tuple(order_match for _ in range(100)),
    }


async def _consume_run_watch(socket_path: Path, run_id: str) -> None:
    client = UdsRuntimeClient(socket_path)
    async for _ in client.watch_run(run_id, timeout_seconds=5):
        pass


def _find_study_root(trial_root: Path) -> Path:
    for candidate in (trial_root, *trial_root.parents):
        if (candidate / "study_plan.json").is_file():
            return candidate
    raise ExperimentValidationError("cannot locate Study root from Trial directory")


def _prepare_runtime_security(runtime_directory: str, token_file: str) -> None:
    runtime = Path(runtime_directory)
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(runtime, 0o700)
    token = Path(token_file)
    token.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(token.parent, 0o700)
    if token.exists():
        info = token.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size == 0
        ):
            raise ExperimentValidationError(
                "cluster token must be a non-empty private regular file"
            )
        return
    descriptor = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(secrets.token_bytes(32))
        stream.flush()
        os.fsync(stream.fileno())


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ExperimentValidationError(f"{name} is not an object")
    return cast(Mapping[str, object], value)


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ExperimentValidationError(f"{name} is invalid")
    return value


def _contains_value(value: object, expected: str) -> bool:
    if value == expected:
        return True
    if isinstance(value, Mapping):
        return any(_contains_value(item, expected) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_value(item, expected) for item in value)
    return False


def _exception_text(exc: BaseException) -> str:
    name = f"{type(exc).__module__}.{type(exc).__qualname__}"
    message = str(exc).strip()
    return name if not message else f"{name}: {message}"
