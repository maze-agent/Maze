"""Ray dataflow and invocation lifetime; no application-level node scheduler."""

import asyncio
import json
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from typing import Literal as Choice

import ray

from maze.graph import (
    DAG,
    AsyncOperation,
    Input,
    Literal,
    Output,
    Step,
    Values,
    compile,
)


@dataclass(frozen=True)
class NodeResult:
    outputs: Values
    metrics: Mapping[str, int | float] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


class NodeExecutionError(RuntimeError):
    """A single constructor argument keeps Ray exception serialization reliable."""

    def __init__(self, node_id: str):
        super().__init__(node_id)
        self.node_id = node_id

    def __str__(self) -> str:
        return f"Node execution failed: {self.node_id}"


class NodeContractError(NodeExecutionError):
    pass


def _arguments(step: Step, values: tuple) -> dict[str, Any]:
    arguments = dict(zip((n for n, _ in step.inputs), values, strict=True))
    try:
        for name, check in step.input_checks:
            check(arguments[name])
    except (TypeError, ValueError) as exc:
        raise NodeContractError(step.id) from exc
    return arguments


def _pack(
    step: Step, result: Values | NodeResult, started: float, metrics: bool
) -> tuple:
    custom = {}
    metadata = {}
    if isinstance(result, NodeResult):
        custom = dict(result.metrics)
        metadata = dict(result.metadata)
        result = result.outputs
    if not isinstance(result, Mapping) or set(result) != set(step.outputs):
        raise NodeContractError(step.id)
    try:
        for name, check in step.output_checks:
            check(result[name])
    except (TypeError, ValueError) as exc:
        raise NodeContractError(step.id) from exc
    if len(custom) > 32 or any(type(v) not in (int, float) for v in custom.values()):
        raise ValueError("Custom metrics must contain at most 32 numeric fields")
    if len(json.dumps(custom, allow_nan=False).encode()) > 4096:
        raise ValueError("Custom metrics too large")
    if len(json.dumps(metadata, allow_nan=False).encode()) > 4096:
        raise ValueError("Node metadata too large")
    signal = {"node": step.id}
    if metrics:
        signal.update(
            execution_ms=(time.perf_counter() - started) * 1000,
            source="node_wrapper",
            custom=custom,
        )
        if metadata:
            signal["metadata"] = metadata
    return (*(result[name] for name in step.outputs), signal)


@ray.remote
def _execute(step: Step, metrics: bool, *values: Any) -> tuple:
    started = time.perf_counter()
    try:
        result = step.function(**_arguments(step, values))
        return _pack(step, result, started, metrics)
    except NodeExecutionError:
        raise
    except Exception as exc:
        raise NodeExecutionError(step.id) from exc


@ray.remote(num_cpus=0, max_retries=0)
def _probe(step: Step) -> bool:
    """Force worker-side code deserialization before deployment readiness."""
    return callable(step.function)


@ray.remote
class _AsyncNode:
    def __init__(self, step: Step) -> None:
        self.step = step

    async def ready(self) -> bool:
        if isinstance(self.step.function, AsyncOperation):
            await self.step.function.ready()
        return True

    async def close(self) -> None:
        if isinstance(self.step.function, AsyncOperation):
            await self.step.function.aclose()

    async def execute(self, metrics: bool, *values: Any) -> tuple:
        started = time.perf_counter()
        try:
            result = await self.step.function(**_arguments(self.step, values))
            return _pack(self.step, result, started, metrics)
        except NodeExecutionError:
            raise
        except Exception as exc:
            raise NodeExecutionError(self.step.id) from exc


@dataclass
class Invocation:
    calls: list[tuple[ray.ObjectRef, bool]] = field(default_factory=list)
    signals: list[ray.ObjectRef] = field(default_factory=list)
    futures: list[asyncio.Future] = field(default_factory=list)
    cleanup_complete: bool = True

    def pending(self) -> list[tuple[ray.ObjectRef, bool]]:
        if not self.calls:
            return []
        ready, _ = ray.wait(
            [r for r, _ in self.calls], num_returns=len(self.calls), timeout=0
        )
        complete = set(ready)
        return [(r, actor) for r, actor in self.calls if r not in complete]

    async def cleanup(self, grace_s: float) -> None:
        self.cleanup_complete = False
        task = asyncio.create_task(self._cleanup(grace_s))
        # A second cancellation must not cut short the bounded remote cleanup.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.result()

    async def _cleanup(self, grace_s: float) -> None:
        # Future cancellation only stops waiting; explicitly cancel every remote call.
        for future in self.futures:
            future.cancel()
        for ref, _ in self.pending():
            with suppress(Exception):
                ray.cancel(ref, recursive=False)
        deadline = time.monotonic() + grace_s
        while self.pending() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        for ref, actor in self.pending():
            if not actor:
                with suppress(Exception):
                    ray.cancel(ref, force=True, recursive=False)
        self.cleanup_complete = not self.pending()


class Runtime:
    """Compile once. Async Actors belong to this runtime and survive invocations."""

    def __init__(self, dag: DAG) -> None:
        self.plan = compile(dag)
        self.actors: dict[str, Any] = {}

    async def prepare(self, timeout_s: float = 60) -> None:
        if not ray.is_initialized():
            raise RuntimeError("Start Ray before preparing a DAG")
        probes = []
        try:
            for step in self.plan.steps:
                if step.execution == "async_actor" and step.id not in self.actors:
                    self.actors[step.id] = _AsyncNode.options(
                        num_cpus=step.num_cpus,
                        num_gpus=step.num_gpus,
                        max_concurrency=step.max_concurrency,
                        max_pending_calls=step.max_concurrency,
                        max_restarts=0,
                        max_task_retries=0,
                        **({"memory": step.memory_bytes} if step.memory_bytes else {}),
                    ).remote(step)
            probes = [actor.ready.remote() for actor in self.actors.values()]
            probes.extend(
                _probe.remote(step)
                for step in self.plan.steps
                if step.execution == "task"
            )
            async with asyncio.timeout(timeout_s):
                await asyncio.gather(
                    *(asyncio.wrap_future(ref.future()) for ref in probes)
                )
        except BaseException:
            for ref in probes:
                with suppress(Exception):
                    ray.cancel(ref, recursive=False)
            self.close()
            raise

    def close(self) -> None:
        """Caller must drain invocations first; only this deployment's Actors die."""
        for actor in self.actors.values():
            ray.kill(actor, no_restart=True)
        self.actors.clear()

    async def aclose(self, timeout_s: float = 2) -> None:
        """Drain first, close Actor-owned clients, then terminate owned Actors."""
        try:
            async with asyncio.timeout(timeout_s):
                await asyncio.gather(
                    *(
                        asyncio.wrap_future(actor.close.remote().future())
                        for actor in self.actors.values()
                    )
                )
        finally:
            self.close()

    def invoke(self, inputs: Values) -> dict[str, Any]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.ainvoke(inputs))["output"]
        raise RuntimeError("Use await runtime.ainvoke() inside an event loop")

    async def ainvoke(
        self,
        inputs: Values,
        *,
        metrics: Choice["none", "basic", "detailed"] = "none",
        timeout_s: float = 120,
        cleanup_grace_s: float = 2,
        scope: Invocation | None = None,
    ) -> dict[str, Any]:
        if not ray.is_initialized():
            raise RuntimeError("Start Ray before invoking a DAG")
        if set(inputs) != set(self.plan.inputs):
            raise ValueError(f"Expected graph inputs: {self.plan.inputs}")
        if any(
            s.execution == "async_actor" and s.id not in self.actors
            for s in self.plan.steps
        ):
            raise RuntimeError(
                "Call await runtime.prepare() before invoking async nodes"
            )
        scope = scope or Invocation()
        refs: dict[Output, ray.ObjectRef] = {}
        started = time.perf_counter()

        def resolve(source: Input | Output | Literal) -> Any:
            match source:
                case Input(name):
                    return inputs[name]
                case Output():
                    return refs[source]
                case Literal(value):
                    return value

        try:
            async with asyncio.timeout(timeout_s):
                for step in self.plan.steps:
                    values = tuple(resolve(source) for _, source in step.inputs)
                    if step.execution == "async_actor":
                        outputs = (
                            self.actors[step.id]
                            .execute.options(
                                num_returns=len(step.outputs) + 1,
                            )
                            .remote(metrics != "none", *values)
                        )
                    else:
                        outputs = _execute.options(
                            name=step.id,
                            num_cpus=step.num_cpus,
                            num_gpus=step.num_gpus,
                            num_returns=len(step.outputs) + 1,
                            max_retries=step.max_retries,
                            retry_exceptions=step.retry_exceptions,
                            **(
                                {"memory": step.memory_bytes}
                                if step.memory_bytes
                                else {}
                            ),
                        ).remote(step, metrics != "none", *values)
                    scope.calls.append((outputs[-1], step.execution == "async_actor"))
                    scope.signals.append(outputs[-1])
                    refs.update(
                        (Output(step.id, port), ref)
                        for port, ref in zip(step.outputs, outputs[:-1], strict=True)
                    )
                wanted = scope.signals + [refs[ref] for _, ref in self.plan.outputs]
                scope.futures = [asyncio.wrap_future(ref.future()) for ref in wanted]
                # gather observes failures as they arrive, not in submission order.
                results = await asyncio.gather(*scope.futures)
                signals = results[: len(scope.signals)]
                output = dict(
                    zip(
                        (name for name, _ in self.plan.outputs),
                        results[len(scope.signals) :],
                        strict=True,
                    )
                )
                response: dict[str, Any] = {"output": output}
                if metrics != "none":
                    response["metrics"] = {
                        "execution_ms": (time.perf_counter() - started) * 1000,
                        "nodes": {s["node"]: s for s in signals},
                        "complete": metrics == "basic",
                        "missing_fields": ["ray_events"]
                        if metrics == "detailed"
                        else [],
                        "attempt_scope": "last_observed_attempt",
                    }
                return response
        except BaseException:
            await scope.cleanup(cleanup_grace_s)
            raise
        finally:
            scope.futures.clear()
            if scope.cleanup_complete:
                scope.calls.clear()
                scope.signals.clear()
