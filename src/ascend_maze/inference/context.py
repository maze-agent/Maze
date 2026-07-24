"""Attempt-scoped inference context used by synchronous Task callables."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar, Token
from threading import RLock
from typing import Callable, Iterator, Protocol

from ascend_maze.contracts.runtime import ModelRouteLease
from ascend_maze.core.clock import Clock, SystemClock
from ascend_maze.inference.contracts import (
    AttemptInferenceSummary,
    ChatRequest,
    ChatResponse,
    InferenceCallError,
    InferenceRequestRecord,
    ModelRouteContext,
)


class InferenceRequestLifecycle(Protocol):
    def request_started(self, route_lease_id: str) -> object: ...

    def request_finished(self, route_lease_id: str) -> None: ...


class InferenceChatAdapter(Protocol):
    async def invoke_chat(
        self,
        context: ModelRouteContext,
        request: ChatRequest,
    ) -> ChatResponse: ...


class AttemptInferenceSession:
    def __init__(
        self,
        *,
        lease: ModelRouteLease,
        router: InferenceRequestLifecycle,
        adapter: InferenceChatAdapter,
        instance_placement_lease_id: str,
        record_sink: Callable[[InferenceRequestRecord], None],
        clock: Clock | None = None,
    ) -> None:
        self.lease = lease
        self.router = router
        self.adapter = adapter
        self.instance_placement_lease_id = instance_placement_lease_id
        self.record_sink = record_sink
        self.clock = clock or SystemClock()
        self.context = ModelRouteContext(
            route_lease_id=lease.route_lease_id,
            model_id=lease.model_id,
            adapter_name=lease.adapter_name,
            endpoint_id=lease.endpoint_id,
            instance_id=lease.instance_id,
            instance_generation=lease.instance_generation,
        )
        self._call_index = 0
        self._inflight = False
        self._context_cleared = False
        self._lock = RLock()

    def invoke(self, request: ChatRequest) -> ChatResponse:
        with self._lock:
            if self._inflight:
                raise InferenceCallError(
                    "model_route_concurrent_call_forbidden",
                    "one ModelRouteLease cannot execute concurrent chat calls",
                )
            self._inflight = True
            self._call_index += 1
            call_index = self._call_index
        started = self.clock.monotonic_ms()
        status = "failed"
        response: ChatResponse | None = None
        error_code: str | None = None
        request_registered = False
        try:
            self.router.request_started(self.lease.route_lease_id)
            request_registered = True
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise InferenceCallError(
                    "model_chat_async_context_unsupported",
                    "synchronous Task chat cannot run on an asyncio event loop thread",
                )
            response = asyncio.run(self.adapter.invoke_chat(self.context, request))
            status = "succeeded"
            return response
        except InferenceCallError as exc:
            error_code = exc.error_code
            raise
        except Exception as exc:
            error_code = "model_inference_failed"
            raise InferenceCallError(
                error_code,
                f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            duration = max(0, self.clock.monotonic_ms() - started)
            try:
                if request_registered:
                    self.router.request_finished(self.lease.route_lease_id)
            finally:
                try:
                    self.record_sink(
                        InferenceRequestRecord(
                            route_lease_id=self.lease.route_lease_id,
                            call_index=call_index,
                            run_id=self.lease.run_id,
                            task_id=self.lease.task_id,
                            attempt=self.lease.attempt,
                            model_id=self.lease.model_id,
                            instance_id=self.lease.instance_id,
                            instance_generation=self.lease.instance_generation,
                            instance_placement_lease_id=(
                                self.instance_placement_lease_id
                            ),
                            started_at_ms=started,
                            duration_ms=duration,
                            status=status,
                            input_tokens=(
                                None if response is None else response.input_tokens
                            ),
                            output_tokens=(
                                None if response is None else response.output_tokens
                            ),
                            engine_queue_depth=(
                                None
                                if response is None
                                else response.engine_queue_depth
                            ),
                            prefix_cache_hit=(
                                None if response is None else response.prefix_cache_hit
                            ),
                            ttft_ms=None if response is None else response.ttft_ms,
                            error_code=error_code,
                        )
                    )
                finally:
                    with self._lock:
                        self._inflight = False

    def mark_context_cleared(self) -> None:
        with self._lock:
            self._context_cleared = True

    def summary(self) -> AttemptInferenceSummary:
        with self._lock:
            return AttemptInferenceSummary(
                route_lease_id=self.lease.route_lease_id,
                request_count=self._call_index,
                request_inflight=self._inflight,
                context_cleared=self._context_cleared,
            )


_CURRENT_SESSION: ContextVar[AttemptInferenceSession | None] = ContextVar(
    "ascend_maze_model_route_session", default=None
)


@contextmanager
def install_route_session(
    session: AttemptInferenceSession,
) -> Iterator[AttemptInferenceSession]:
    if _CURRENT_SESSION.get() is not None:
        raise InferenceCallError(
            "model_route_context_leaked",
            "a previous ModelRouteContext is still installed",
        )
    token: Token[AttemptInferenceSession | None] = _CURRENT_SESSION.set(session)
    try:
        yield session
    finally:
        _CURRENT_SESSION.reset(token)
        session.mark_context_cleared()


def current_route_context() -> ModelRouteContext:
    session = _CURRENT_SESSION.get()
    if session is None:
        raise InferenceCallError(
            "model_route_context_missing",
            "chat() requires an active model service Task Attempt",
        )
    return session.context


def invoke_current(request: ChatRequest) -> ChatResponse:
    session = _CURRENT_SESSION.get()
    if session is None:
        raise InferenceCallError(
            "model_route_context_missing",
            "chat() requires an active model service Task Attempt",
        )
    return session.invoke(request)
