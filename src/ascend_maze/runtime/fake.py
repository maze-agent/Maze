"""Asynchronous local worker simulation behind the real RuntimeBackend contract."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter
from threading import RLock
from typing import Callable, cast

from ascend_maze.contracts.errors import ErrorInfo
from ascend_maze.contracts.recording import ProducerFlushResult, RunRecordingContext
from ascend_maze.contracts.resources import ExecutionTarget, PlacementLease
from ascend_maze.contracts.runtime import (
    CodeHandle,
    CodePackage,
    DispatchHandle,
    ExecutionRequest,
)
from ascend_maze.core.errors import ContractValidationError
from ascend_maze.core.identifiers import stable_id
from ascend_maze.core.time import monotonic_time_ms
from ascend_maze.data.in_memory import InMemoryDataStore
from ascend_maze.inference.context import install_route_session
from ascend_maze.inference.contracts import InferenceCallError
from ascend_maze.inference.coordinator import InferenceCoordinator
from ascend_maze.runtime.events import RuntimeEvent, RuntimeEventKind
from ascend_maze.runtime.code_loader import (
    load_code_package,
    validate_loaded_callable,
)


@dataclass(frozen=True, slots=True)
class FakeExecutionPlan:
    start_delay_ms: int = 0
    execution_delay_ms: int = 0
    fail_before_start: str | None = None
    fail_after_start: str | None = None
    ignore_cancel: bool = False
    duplicate_terminal_event: bool = False


@dataclass(slots=True)
class _CodeRecord:
    handle: CodeHandle
    func: Callable[..., object]
    reference_count: int


@dataclass(slots=True)
class _DispatchRecord:
    request: ExecutionRequest
    lease: PlacementLease
    handle: DispatchHandle
    func: Callable[..., object]
    plan: FakeExecutionPlan
    task: asyncio.Task[None] | None
    cancel_requested: bool = False
    terminal: bool = False


@dataclass(frozen=True, slots=True)
class FakeTaskTimingRecord:
    dispatch_id: str
    run_id: str
    task_id: str
    attempt: int
    task_kind: str
    execution_target: str
    route_lease_id: str | None
    started_at_ms: int
    status: str
    error_code: str | None
    input_fetch_ms: int
    callable_execute_ms: int
    chat_request_ms: int
    output_put_ms: int
    task_total_ms: int
    input_handle_count: int
    output_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "dispatch_id": self.dispatch_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "task_kind": self.task_kind,
            "execution_target": self.execution_target,
            "route_lease_id": self.route_lease_id,
            "started_at_ms": self.started_at_ms,
            "status": self.status,
            "error_code": self.error_code,
            "input_fetch_ms": self.input_fetch_ms,
            "callable_execute_ms": self.callable_execute_ms,
            "chat_request_ms": self.chat_request_ms,
            "output_put_ms": self.output_put_ms,
            "task_total_ms": self.task_total_ms,
            "input_handle_count": self.input_handle_count,
            "output_count": self.output_count,
            "task_runtime_overhead_ms": max(
                0,
                self.task_total_ms
                - self.input_fetch_ms
                - self.callable_execute_ms
                - self.output_put_ms,
            ),
            "callable_minus_chat_ms": max(
                0,
                self.callable_execute_ms - self.chat_request_ms,
            ),
        }


class FakeRuntimeBackend:
    """Run ordinary Python functions while preserving distributed event boundaries."""

    backend_name = "fake"

    def __init__(
        self,
        *,
        data_store: InMemoryDataStore,
        owner_generation: str,
        environment_fingerprint: str,
        event_sink: Callable[[RuntimeEvent], None] | None = None,
        inference: InferenceCoordinator | None = None,
    ) -> None:
        self.data_store = data_store
        self.owner_generation = owner_generation
        self.environment_fingerprint = environment_fingerprint
        self._event_sink = event_sink
        self.inference = inference
        self._code: dict[str, _CodeRecord] = {}
        self._dispatches: dict[str, _DispatchRecord] = {}
        self._attempt_dispatches: dict[tuple[str, str, int], str] = {}
        self._task_timings: list[FakeTaskTimingRecord] = []
        self._plans: dict[tuple[str, int], FakeExecutionPlan] = {}
        self._registered_callables: dict[str, Callable[..., object]] = {}
        self._retired_runs: set[str] = set()
        self._started = False
        self._closed = False
        self._lock = RLock()

    def set_event_sink(self, sink: Callable[[RuntimeEvent], None]) -> None:
        self._event_sink = sink

    def register_callable(
        self,
        definition_id: str,
        func: Callable[..., object],
    ) -> None:
        self._registered_callables[definition_id] = func

    def set_plan(self, task_id: str, attempt: int, plan: FakeExecutionPlan) -> None:
        self._plans[(task_id, attempt)] = plan

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("runtime backend is closed")
        self._started = True

    async def prepare(
        self,
        definitions: tuple[CodePackage, ...],
    ) -> tuple[CodeHandle, ...]:
        if not self._started or self._closed:
            raise RuntimeError("runtime backend is not running")
        definition_ids = [package.definition_id for package in definitions]
        if len(definition_ids) != len(set(definition_ids)):
            raise ContractValidationError("CodePackage definitions must be unique")
        prepared: list[tuple[CodePackage, Callable[..., object], CodeHandle]] = []
        for package in definitions:
            if package.environment_fingerprint != self.environment_fingerprint:
                raise ContractValidationError("code package environment mismatch")
            existing = self._code.get(package.definition_id)
            if existing is not None:
                if existing.handle.code_hash != package.code_hash:
                    raise ContractValidationError("definition code hash conflict")
                prepared.append((package, existing.func, existing.handle))
                continue
            func = self._registered_callables.get(package.definition_id)
            if func is None:
                func = self._load_package_callable(package)
            self._validate_callable(func, package)
            handle = CodeHandle(
                code_handle_id=stable_id(
                    "code",
                    package.definition_id,
                    package.code_hash,
                    package.environment_fingerprint,
                ),
                definition_id=package.definition_id,
                code_hash=package.code_hash,
            )
            prepared.append((package, func, handle))

        handles: list[CodeHandle] = []
        for package, func, handle in prepared:
            existing = self._code.get(package.definition_id)
            if existing is None:
                self._code[package.definition_id] = _CodeRecord(handle, func, 1)
            else:
                existing.reference_count += 1
            handles.append(handle)
        return tuple(handles)

    async def dispatch(
        self,
        request: ExecutionRequest,
        lease: PlacementLease,
    ) -> DispatchHandle:
        if not self._started or self._closed:
            raise RuntimeError("runtime backend is not running")
        existing = self._dispatches.get(request.dispatch_id)
        if existing is not None:
            if existing.request != request or existing.lease != lease:
                raise ContractValidationError("dispatch_id payload conflict")
            return existing.handle
        attempt_key = (request.run_id, request.task_id, request.attempt)
        conflicting = self._attempt_dispatches.get(attempt_key)
        if conflicting is not None and conflicting != request.dispatch_id:
            raise ContractValidationError("attempt already has another dispatch_id")
        if (
            lease.run_id != request.run_id
            or lease.task_id != request.task_id
            or lease.attempt != request.attempt
        ):
            raise ContractValidationError("PlacementLease does not match request")
        if request.environment_fingerprint != self.environment_fingerprint:
            raise ContractValidationError("execution environment mismatch")
        if request.execution_target is ExecutionTarget.MODEL_SERVICE:
            if self.inference is None or request.model_route is None:
                raise ContractValidationError(
                    "FakeRuntime model service dispatch requires C11"
                )
            route = request.model_route
            if (
                route.run_id != request.run_id
                or route.task_id != request.task_id
                or route.attempt != request.attempt
            ):
                raise ContractValidationError(
                    "ModelRouteLease does not match execution request"
                )
            if self.inference.route_snapshot(route.route_lease_id).lease != route:
                raise ContractValidationError(
                    "ModelRouteLease payload does not match C11 authority"
                )
        code = self._code.get(request.code_handle.definition_id)
        if code is None or code.handle != request.code_handle:
            raise ContractValidationError("CodeHandle is not prepared")
        handle = DispatchHandle(
            dispatch_id=request.dispatch_id,
            backend_name=self.backend_name,
            run_id=request.run_id,
            task_id=request.task_id,
            attempt=request.attempt,
            lease_id=lease.lease_id,
            route_lease_id=(
                None
                if request.model_route is None
                else request.model_route.route_lease_id
            ),
            worker_endpoint_id=f"fake://{lease.node_id}/{request.dispatch_id}",
        )
        plan = self._plans.get(
            (request.task_id, request.attempt), FakeExecutionPlan()
        )
        record = _DispatchRecord(request, lease, handle, code.func, plan, None)
        task = asyncio.create_task(self._execute(record))
        record.task = task
        self._dispatches[request.dispatch_id] = record
        self._attempt_dispatches[attempt_key] = request.dispatch_id
        return handle

    async def cancel(self, handle: DispatchHandle, reason: str) -> None:
        del reason
        record = self._dispatches.get(handle.dispatch_id)
        if record is None:
            return
        if record.handle != handle:
            raise ContractValidationError("DispatchHandle payload conflict")
        if record.cancel_requested or record.terminal:
            return
        record.cancel_requested = True
        if not record.plan.ignore_cancel:
            assert record.task is not None
            record.task.cancel()
            await asyncio.gather(record.task, return_exceptions=True)

    async def release_code(self, handles: tuple[CodeHandle, ...]) -> None:
        for handle in handles:
            record = self._code.get(handle.definition_id)
            if record is None:
                continue
            if record.handle != handle:
                raise ContractValidationError("CodeHandle payload conflict")
            if record.reference_count > 0:
                record.reference_count -= 1
            if record.reference_count == 0:
                del self._code[handle.definition_id]

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = [
            record.task
            for record in self._dispatches.values()
            if record.task is not None and not record.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_idle(self) -> None:
        tasks = [
            record.task
            for record in self._dispatches.values()
            if record.task is not None and not record.task.done()
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def code_reference_count(self) -> int:
        return sum(record.reference_count for record in self._code.values())

    def active_dispatch_count(self, run_id: str | None = None) -> int:
        return sum(
            not record.terminal and (run_id is None or record.request.run_id == run_id)
            for record in self._dispatches.values()
        )

    def dispatch_record_count(self, run_id: str | None = None) -> int:
        return sum(
            run_id is None or record.request.run_id == run_id
            for record in self._dispatches.values()
        )

    def dispatch_invalidated(self, dispatch_id: str) -> bool:
        record = self._dispatches.get(dispatch_id)
        return record is None or record.terminal or record.cancel_requested

    def worker_released(self, dispatch_id: str) -> bool:
        record = self._dispatches.get(dispatch_id)
        return record is None or record.terminal

    def task_timing_records(
        self,
        run_id: str | None = None,
    ) -> tuple[dict[str, object], ...]:
        with self._lock:
            records = [
                record.as_dict()
                for record in self._task_timings
                if run_id is None or record.run_id == run_id
            ]
        return tuple(
            sorted(
                records,
                key=lambda item: (
                    cast(int, item["started_at_ms"]),
                    str(item["task_id"]),
                    cast(int, item["attempt"]),
                ),
            )
        )

    def producer_for_lease(self, lease: PlacementLease) -> str | None:
        del lease
        return None

    def producer_is_persistent(self, lease: PlacementLease) -> bool:
        del lease
        return False

    async def prepare_run_recording(
        self,
        context: RunRecordingContext,
        lease: PlacementLease,
    ) -> None:
        del context, lease

    async def flush_run_recorders(
        self,
        run_id: str,
        timeout_ms: int,
    ) -> tuple[ProducerFlushResult, ...]:
        del run_id, timeout_ms
        return ()

    async def release_run(self, run_id: str) -> int:
        self._retired_runs.add(run_id)
        dispatch_ids = [
            dispatch_id
            for dispatch_id, record in self._dispatches.items()
            if record.request.run_id == run_id and record.terminal
        ]
        for dispatch_id in dispatch_ids:
            self._drop_dispatch(dispatch_id)
        if not any(
            record.request.run_id == run_id for record in self._dispatches.values()
        ):
            self._retired_runs.discard(run_id)
        return len(dispatch_ids)

    async def _execute(self, record: _DispatchRecord) -> None:
        request = record.request
        task_started_at_ms: int | None = None
        task_started_perf: float | None = None
        input_fetch_ms = 0
        callable_execute_ms = 0
        output_put_ms = 0
        input_handle_count = 0
        output_count = 0
        terminal_status = "unknown"
        terminal_error_code: str | None = None
        try:
            await self._delay(record.plan.start_delay_ms)
            if record.plan.fail_before_start is not None:
                terminal_status = "dispatch_failed"
                terminal_error_code = record.plan.fail_before_start
                self._emit_failure(
                    record,
                    RuntimeEventKind.DISPATCH_FAILED,
                    record.plan.fail_before_start,
                    phase="dispatched",
                )
                return
            if request.execution_target is ExecutionTarget.MODEL_SERVICE:
                assert self.inference is not None
                assert request.model_route is not None
                if not self.inference.activate_route(
                    request.model_route.route_lease_id
                ):
                    terminal_status = "dispatch_failed"
                    terminal_error_code = "model_route_invalidated"
                    self._emit_failure(
                        record,
                        RuntimeEventKind.DISPATCH_FAILED,
                        "model_route_invalidated",
                        phase="dispatched",
                    )
                    return
            self._emit(
                RuntimeEvent.create(
                    kind=RuntimeEventKind.WORKER_STARTED,
                    dispatch_id=request.dispatch_id,
                    run_id=request.run_id,
                    task_id=request.task_id,
                    attempt=request.attempt,
                    lease_id=record.lease.lease_id,
                    route_lease_id=record.handle.route_lease_id,
                    occurred_at_ms=monotonic_time_ms(),
                )
            )
            task_started_at_ms = monotonic_time_ms()
            task_started_perf = perf_counter()
            await self._delay(record.plan.execution_delay_ms)
            if record.plan.fail_after_start is not None:
                terminal_status = "failed"
                terminal_error_code = record.plan.fail_after_start
                self._emit_failure(
                    record,
                    RuntimeEventKind.TASK_FAILED,
                    record.plan.fail_after_start,
                    phase="user_code",
                )
                return
            kwargs: dict[str, object] = {}
            started = perf_counter()
            for argument in request.arguments:
                if argument.kind == "literal":
                    kwargs[argument.name] = argument.literal
                elif argument.kind == "data_handle":
                    assert argument.data_handle is not None
                    input_handle_count += 1
                    kwargs[argument.name] = self.data_store.get(argument.data_handle)
            input_fetch_ms = _elapsed_ms(started)
            try:
                started = perf_counter()
                if request.execution_target is ExecutionTarget.MODEL_SERVICE:
                    assert self.inference is not None
                    assert request.model_route is not None
                    session = self.inference.create_attempt_session(
                        request.model_route
                    )
                    with install_route_session(session):
                        result = await self._call_user(record.func, kwargs)
                    summary = session.summary()
                    if summary.request_inflight or not summary.context_cleared:
                        raise InferenceCallError(
                            "model_protocol_failed",
                            "model route context was not clean at Task terminal",
                        )
                else:
                    result = await asyncio.to_thread(record.func, **kwargs)
                callable_execute_ms = _elapsed_ms(started)
            except InferenceCallError as exc:
                callable_execute_ms = _elapsed_ms(started)
                terminal_status = "failed"
                terminal_error_code = exc.error_code
                self._emit_failure(
                    record,
                    RuntimeEventKind.TASK_FAILED,
                    exc.error_code,
                    phase="user_code",
                    message=str(exc),
                )
                return
            except Exception as exc:
                callable_execute_ms = _elapsed_ms(started)
                terminal_status = "failed"
                terminal_error_code = "user_code_failed"
                self._emit_failure(
                    record,
                    RuntimeEventKind.TASK_FAILED,
                    "user_code_failed",
                    phase="user_code",
                    message=f"{type(exc).__name__}: {exc}",
                )
                return
            if not isinstance(result, dict) or tuple(sorted(result)) != tuple(
                sorted(request.expected_outputs)
            ):
                terminal_status = "failed"
                terminal_error_code = "invalid_task_output"
                self._emit_failure(
                    record,
                    RuntimeEventKind.TASK_FAILED,
                    "invalid_task_output",
                    phase="publishing",
                )
                return
            output_handles = []
            try:
                started = perf_counter()
                for output_name in request.expected_outputs:
                    handle = self.data_store.put_staged(
                        result[output_name], self.owner_generation
                    )
                    output_handles.append((output_name, handle))
                output_put_ms = _elapsed_ms(started)
                output_count = len(output_handles)
            except Exception as exc:
                output_put_ms = _elapsed_ms(started)
                for _, handle in output_handles:
                    self.data_store.release(handle)
                terminal_status = "failed"
                terminal_error_code = "result_publish_failed"
                self._emit_failure(
                    record,
                    RuntimeEventKind.TASK_FAILED,
                    "result_publish_failed",
                    phase="publishing",
                    message=f"{type(exc).__name__}: {exc}",
                )
                return
            event = RuntimeEvent.create(
                kind=RuntimeEventKind.TASK_RESULT,
                dispatch_id=request.dispatch_id,
                run_id=request.run_id,
                task_id=request.task_id,
                attempt=request.attempt,
                lease_id=record.lease.lease_id,
                route_lease_id=record.handle.route_lease_id,
                occurred_at_ms=monotonic_time_ms(),
                output_handles=tuple(output_handles),
            )
            self._emit(event)
            if record.plan.duplicate_terminal_event:
                self._emit(event)
            terminal_status = "succeeded"
        except asyncio.CancelledError:
            terminal_status = "cancelled"
            self._emit(
                RuntimeEvent.create(
                    kind=RuntimeEventKind.TASK_CANCELLED,
                    dispatch_id=request.dispatch_id,
                    run_id=request.run_id,
                    task_id=request.task_id,
                    attempt=request.attempt,
                    lease_id=record.lease.lease_id,
                    route_lease_id=record.handle.route_lease_id,
                    occurred_at_ms=monotonic_time_ms(),
                )
            )
        finally:
            if task_started_at_ms is not None and task_started_perf is not None:
                chat_request_ms = self._chat_request_ms(record)
                timing = FakeTaskTimingRecord(
                    dispatch_id=request.dispatch_id,
                    run_id=request.run_id,
                    task_id=request.task_id,
                    attempt=request.attempt,
                    task_kind=request.task_kind,
                    execution_target=request.execution_target.value,
                    route_lease_id=record.handle.route_lease_id,
                    started_at_ms=task_started_at_ms,
                    status=terminal_status,
                    error_code=terminal_error_code,
                    input_fetch_ms=input_fetch_ms,
                    callable_execute_ms=callable_execute_ms,
                    chat_request_ms=chat_request_ms,
                    output_put_ms=output_put_ms,
                    task_total_ms=_elapsed_ms(task_started_perf),
                    input_handle_count=input_handle_count,
                    output_count=output_count,
                )
                with self._lock:
                    self._task_timings.append(timing)
            record.terminal = True
            if record.request.run_id in self._retired_runs:
                self._drop_dispatch(record.request.dispatch_id)

    def _emit_failure(
        self,
        record: _DispatchRecord,
        kind: RuntimeEventKind,
        error_code: str,
        *,
        phase: str,
        message: str | None = None,
    ) -> None:
        request = record.request
        category = (
            "model"
            if error_code.startswith("model_")
            else
            "user"
            if error_code == "user_code_failed"
            else "data"
            if error_code in {"invalid_task_output", "result_publish_failed"}
            else "worker"
        )
        error = ErrorInfo(
            schema_version=1,
            error_code=error_code,
            category=category,
            origin="worker" if kind is RuntimeEventKind.TASK_FAILED else "runtime",
            message=message or error_code,
            retryable_hint=error_code
            in {"worker_acquire_failed", "worker_start_failed"},
            classification_confidence="exact",
            execution_phase=phase,
            run_id=request.run_id,
            task_id=request.task_id,
            attempt=request.attempt,
            dispatch_id=request.dispatch_id,
            lease_id=record.lease.lease_id,
            route_lease_id=record.handle.route_lease_id,
            model_instance_id=(
                None
                if request.model_route is None
                else request.model_route.instance_id
            ),
            occurred_at_ms=monotonic_time_ms(),
        )
        self._emit(
            RuntimeEvent.create(
                kind=kind,
                dispatch_id=request.dispatch_id,
                run_id=request.run_id,
                task_id=request.task_id,
                attempt=request.attempt,
                lease_id=record.lease.lease_id,
                route_lease_id=record.handle.route_lease_id,
                occurred_at_ms=monotonic_time_ms(),
                error=error,
            )
        )

    def _emit(self, event: RuntimeEvent) -> None:
        if self._event_sink is None:
            raise RuntimeError("runtime event sink is not configured")
        self._event_sink(event)

    def _drop_dispatch(self, dispatch_id: str) -> None:
        record = self._dispatches.pop(dispatch_id, None)
        if record is None:
            return
        key = (
            record.request.run_id,
            record.request.task_id,
            record.request.attempt,
        )
        self._attempt_dispatches.pop(key, None)
        run_id = record.request.run_id
        if not any(
            item.request.run_id == run_id for item in self._dispatches.values()
        ):
            self._retired_runs.discard(run_id)

    def _chat_request_ms(self, record: _DispatchRecord) -> int:
        if self.inference is None or record.handle.route_lease_id is None:
            return 0
        return sum(
            item.duration_ms
            for item in self.inference.request_records(record.handle.route_lease_id)
        )

    @staticmethod
    async def _delay(milliseconds: int) -> None:
        if milliseconds > 0:
            await asyncio.sleep(milliseconds / 1000)

    @staticmethod
    async def _call_user(
        func: Callable[..., object], kwargs: dict[str, object]
    ) -> object:
        task = asyncio.create_task(asyncio.to_thread(func, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    @classmethod
    def _load_package_callable(
        cls,
        package: CodePackage,
    ) -> Callable[..., object]:
        return load_code_package(package)

    @staticmethod
    def _validate_callable(
        func: Callable[..., object],
        package: CodePackage,
    ) -> None:
        validate_loaded_callable(func, package)


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1_000))
