"""One in-memory owner for deployments, bounded admission and cancellation."""

import asyncio
import json
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from maze.runtime import Invocation, NodeContractError, NodeExecutionError, Runtime
from maze.spec import DagSpec, DeploymentSpec, EngineConfig, InvokeRequest, check_json


class EngineError(Exception):
    def __init__(self, code: str, status: int, message: str, cleanup: str = "complete"):
        super().__init__(message)
        self.code, self.status, self.cleanup = code, status, cleanup
        self.invocation_id: str | None = None
        self.deployment_id: str | None = None
        self.node_id: str | None = None
        self.backend_cleanup: str | None = None


def check_size(value: Any, maximum: int, code: str, status: int) -> None:
    try:
        check_json(value)
        size = len(json.dumps(value, allow_nan=False).encode())
    except (TypeError, ValueError, RecursionError) as exc:
        raise EngineError(
            "RESULT_INVALID" if status == 500 else "INPUT_INVALID",
            status,
            "Expected finite JSON data",
        ) from exc
    if size > maximum:
        raise EngineError(code, status, "JSON data exceeds configured size limit")


@dataclass
class Deployment:
    spec: DagSpec
    config: DeploymentSpec
    runtime: Runtime
    state: str = "preparing"
    active: int = 0
    preparation: asyncio.Task[None] | None = None
    removal: asyncio.Task[None] | None = None


@dataclass
class Request:
    deployment_id: str
    weight: int
    task: asyncio.Task | None
    scope: Invocation = field(default_factory=Invocation)
    admitted: bool = False


class Engine:
    """Use from one event loop. Counts invocations, never CPU/GPU allocations."""

    def __init__(self, config: EngineConfig | None = None) -> None:
        self.config = config or EngineConfig()
        self.deployments: dict[str, Deployment] = {}
        self.requests: dict[str, Request] = {}
        self.waiting: deque[str] = deque()
        self.condition = asyncio.Condition()
        self.active = 0
        self.inflight = 0
        self.accepting = True

    async def deploy(self, spec: DagSpec, config: DeploymentSpec | None = None) -> str:
        if not self.accepting:
            raise EngineError("ENGINE_UNAVAILABLE", 503, "Engine is stopping")
        if len(spec.nodes) > min(
            self.config.limits.max_nodes_per_dag,
            self.config.limits.max_inflight_node_calls,
        ):
            raise EngineError("DAG_INVALID", 422, "DAG exceeds node budget")
        try:
            snapshot = DagSpec.model_validate(
                spec.model_dump(by_alias=True)
            ).model_copy(deep=True)
            settings = DeploymentSpec.model_validate(
                (config or DeploymentSpec()).model_dump(by_alias=True)
            ).model_copy(deep=True)
            runtime = Runtime(snapshot.to_dag(settings.services))
        except (ValueError, TypeError, ImportError, AttributeError) as exc:
            raise EngineError("DAG_INVALID", 422, "Invalid DAG or callable") from exc
        deployment_id = "dep_" + uuid4().hex
        deployment = Deployment(snapshot, settings, runtime)
        self.deployments[deployment_id] = deployment
        deployment.preparation = asyncio.create_task(
            runtime.prepare(self.config.timeouts.deployment_s)
        )
        try:
            await deployment.preparation
            if not self.accepting or deployment.state == "draining":
                raise EngineError("DEPLOYMENT_NOT_READY", 503, "Deployment is draining")
            deployment.state = "ready"
        except BaseException as exc:
            runtime.close()
            if deployment.state != "draining":
                deployment.state = "failed"
            if (
                isinstance(exc, asyncio.CancelledError)
                and deployment.state != "draining"
            ):
                raise
            error = EngineError(
                "DEPLOYMENT_NOT_READY", 503, "Deployment preparation failed"
            )
            error.deployment_id = deployment_id
            raise error from exc
        finally:
            deployment.preparation = None
        return deployment_id

    def show(self, deployment_id: str) -> dict[str, Any]:
        deployment = self._get(deployment_id)
        return {
            "deployment_id": deployment_id,
            "name": deployment.spec.name,
            "status": deployment.state,
            "active_invocations": deployment.active,
            "waiting_invocations": sum(
                self.requests[key].deployment_id == deployment_id
                for key in self.waiting
            ),
        }

    def _get(self, deployment_id: str) -> Deployment:
        if deployment_id not in self.deployments:
            raise EngineError("NOT_FOUND", 404, "Deployment not found")
        return self.deployments[deployment_id]

    def _release(self, request: Request) -> None:
        if request.admitted:
            self.active -= 1
            self.inflight -= request.weight
            self.deployments[request.deployment_id].active -= 1
            request.admitted = False

    def _reap(self) -> None:
        # Keep unconfirmed remote work charged against capacity, with no input history.
        for key, request in tuple(self.requests.items()):
            if request.task is None and not request.scope.pending():
                self._release(request)
                del self.requests[key]

    def _fits(self, deployment: Deployment, weight: int) -> bool:
        limits = self.config.limits
        actor_limit = min(
            (
                s.max_concurrency
                for s in deployment.runtime.plan.steps
                if s.execution == "async_actor"
            ),
            default=1024,
        )
        return (
            self.active < limits.max_active_invocations
            and self.inflight + weight <= limits.max_inflight_node_calls
            and deployment.active
            < min(deployment.config.max_active_invocations, actor_limit)
        )

    async def invoke(self, deployment_id: str, body: InvokeRequest) -> dict[str, Any]:
        invocation_id = body.invocation_id or "inv_" + uuid4().hex
        try:
            return await self._invoke(deployment_id, body, invocation_id)
        except EngineError as exc:
            exc.invocation_id = invocation_id
            exc.deployment_id = deployment_id
            deployment = self.deployments.get(deployment_id)
            if (
                deployment
                and exc.code
                in ("INVOCATION_CANCELLED", "INVOCATION_TIMEOUT", "NODE_FAILED")
                and any(node.kind == "llm" for node in deployment.spec.nodes)
            ):
                exc.backend_cleanup = "unconfirmed"
            raise

    async def _invoke(
        self, deployment_id: str, body: InvokeRequest, invocation_id: str
    ) -> dict[str, Any]:
        deployment = self._get(deployment_id)
        if not self.accepting or deployment.state != "ready":
            raise EngineError("DEPLOYMENT_NOT_READY", 503, "Deployment is not ready")
        timeout = body.timeout_s or self.config.timeouts.default_invocation_s
        if timeout > self.config.timeouts.max_invocation_s:
            raise EngineError(
                "INPUT_INVALID", 422, "Deadline exceeds configured maximum"
            )
        check_size(
            body.input, self.config.limits.max_input_bytes, "INPUT_TOO_LARGE", 413
        )
        try:
            values = deployment.spec.check_inputs(body.input)
        except ValueError as exc:
            raise EngineError(
                "INPUT_INVALID", 422, "Invalid invocation inputs"
            ) from exc
        started = time.monotonic()
        self._reap()
        if invocation_id in self.requests:
            raise EngineError("ID_CONFLICT", 409, "Invocation ID is already active")
        weight = len(deployment.runtime.plan.steps)
        if (self.waiting or not self._fits(deployment, weight)) and (
            len(self.waiting) >= self.config.limits.max_waiting_invocations
        ):
            raise EngineError("OVERLOADED", 429, "Invocation queue is full")
        request = Request(deployment_id, weight, asyncio.current_task())
        self.requests[invocation_id] = request
        self.waiting.append(invocation_id)
        try:
            async with asyncio.timeout(timeout):
                async with self.condition:
                    while self.waiting[0] != invocation_id or not self._fits(
                        deployment, weight
                    ):
                        if not self.accepting or deployment.state != "ready":
                            raise EngineError(
                                "DEPLOYMENT_NOT_READY", 503, "Deployment is draining"
                            )
                        # Reap unconfirmed cleanup without storing business history.
                        with suppress(TimeoutError):
                            await asyncio.wait_for(self.condition.wait(), 0.1)
                        self._reap()
                    if not self.accepting or deployment.state != "ready":
                        raise EngineError(
                            "DEPLOYMENT_NOT_READY", 503, "Deployment is draining"
                        )
                    self.waiting.popleft()
                    self.active += 1
                    self.inflight += weight
                    deployment.active += 1
                    request.admitted = True
                    self.condition.notify_all()
                admitted = time.monotonic()
                result = await deployment.runtime.ainvoke(
                    values,
                    metrics=body.metrics,
                    # One outer deadline covers queue and execution.
                    timeout_s=timeout + 1,
                    cleanup_grace_s=self.config.timeouts.cancellation_grace_s,
                    scope=request.scope,
                )
                try:
                    deployment.spec.check_outputs(result["output"])
                except ValueError as exc:
                    raise EngineError(
                        "RESULT_INVALID", 500, "Invalid final output type"
                    ) from exc
                check_size(
                    result["output"],
                    self.config.limits.max_output_bytes,
                    "RESULT_TOO_LARGE",
                    500,
                )
                result.update(
                    invocation_id=invocation_id,
                    deployment_id=deployment_id,
                    status="succeeded",
                )
                if "metrics" in result:
                    result["metrics"].update(
                        engine_elapsed_ms=(time.monotonic() - started) * 1000,
                        admission_wait_ms=(admitted - started) * 1000,
                    )
                return result
        except TimeoutError as exc:
            raise EngineError(
                "INVOCATION_TIMEOUT",
                504,
                "Invocation deadline exceeded",
                "complete" if request.scope.cleanup_complete else "incomplete",
            ) from exc
        except asyncio.CancelledError as exc:
            raise EngineError(
                "INVOCATION_CANCELLED",
                409,
                "Invocation cancelled",
                "complete" if request.scope.cleanup_complete else "incomplete",
            ) from exc
        except EngineError:
            raise
        except Exception as exc:
            cause = getattr(exc, "cause", None)
            error = EngineError(
                "RESULT_INVALID"
                if isinstance(cause, NodeContractError)
                else "NODE_FAILED",
                500,
                "Node execution failed",
                "complete" if request.scope.cleanup_complete else "incomplete",
            )
            if isinstance(cause, NodeExecutionError):
                error.node_id = cause.node_id
            raise error from exc
        finally:
            async with self.condition:
                if invocation_id in self.waiting:
                    self.waiting.remove(invocation_id)
                request.task = None
                if request.scope.cleanup_complete:
                    self._release(request)
                    self.requests.pop(invocation_id, None)
                self.condition.notify_all()

    def cancel(self, invocation_id: str) -> None:
        request = self.requests.get(invocation_id)
        if request is None or request.task is None:
            raise EngineError("NOT_FOUND", 404, "Active invocation not found")
        # Repeated cancellation must not interrupt the first cancellation's cleanup.
        if not request.task.cancelling():
            request.task.cancel()

    async def undeploy(self, deployment_id: str) -> None:
        deployment = self._get(deployment_id)
        if deployment.removal is None or deployment.removal.done():
            deployment.state = "draining"
            deployment.removal = asyncio.create_task(
                self._undeploy(deployment_id, deployment)
            )
        # HTTP caller cancellation must not abandon deployment-owned cleanup.
        await asyncio.shield(deployment.removal)

    async def _undeploy(self, deployment_id: str, deployment: Deployment) -> None:
        deployment.state = "draining"
        if deployment.preparation is not None:
            deployment.preparation.cancel()
            await asyncio.gather(deployment.preparation, return_exceptions=True)
        async with self.condition:
            self.condition.notify_all()
        tasks = [
            r.task
            for r in self.requests.values()
            if r.deployment_id == deployment_id and r.task is not None
        ]
        if tasks:
            _, pending = await asyncio.wait(
                tasks, timeout=self.config.timeouts.shutdown_grace_s
            )
            for task in pending:
                if not task.cancelling():
                    task.cancel()
            if pending:
                await asyncio.wait(
                    pending, timeout=self.config.timeouts.cancellation_grace_s + 1
                )
        self._reap()
        if any(r.deployment_id == deployment_id for r in self.requests.values()):
            raise EngineError(
                "CLEANUP_INCOMPLETE",
                503,
                "Deployment still has pending cleanup",
                "incomplete",
            )
        try:
            await deployment.runtime.aclose(self.config.timeouts.cancellation_grace_s)
        except Exception as exc:
            raise EngineError(
                "CLEANUP_INCOMPLETE", 503, "Actor resource cleanup failed", "incomplete"
            ) from exc
        self.deployments.pop(deployment_id, None)

    async def close(self) -> None:
        self.accepting = False
        results = await asyncio.gather(
            *(self.undeploy(key) for key in tuple(self.deployments)),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
