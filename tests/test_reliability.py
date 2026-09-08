import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import ray

import maze.runtime as runtime_module
from maze import DAG, Literal, Node, Output, Runtime
from maze.engine import Engine, EngineError
from maze.runtime import Invocation
from maze.spec import DagSpec, EngineConfig, InvokeRequest, Limits, Timeouts
from tests.test_engine import delay_spec
from tests.test_spec import arithmetic_spec


@pytest.fixture(scope="module", autouse=True)
def cluster() -> Iterator[None]:
    assert not ray.is_initialized()
    ray.init(
        address="local",
        num_cpus=2,
        object_store_memory=256 * 1024**2,
        include_dashboard=False,
    )
    yield
    ray.shutdown()


def echo(x: Any) -> dict[str, Any]:
    return {"value": x}


def wrong_type(x: Any) -> dict[str, str]:
    return {"value": "not a number"}


def unpicklable_result(x: Any) -> dict[str, Any]:
    return {"value": threading.Lock()}


class UnpicklableError(Exception):
    def __init__(self) -> None:
        super().__init__("test error")
        self.lock = threading.Lock()


def unpicklable_exception(x: Any) -> dict[str, Any]:
    raise UnpicklableError()


def fail_twice(x: str) -> dict[str, int]:
    # Local counter is test fault injection, not business retry state.
    marker = Path(x)
    count = int(marker.read_text()) + 1 if marker.exists() else 1
    marker.write_text(str(count))
    if count < 3:
        raise ValueError("synthetic transient failure")
    return {"value": 42}


def single_spec(function: str = "echo", output_type: str = "any") -> DagSpec:
    return DagSpec.model_validate(
        {
            "schema": "maze.dag/v1",
            "name": "single",
            "inputs": {"x": {}},
            "nodes": [
                {
                    "id": "node",
                    "callable": f"tests.test_reliability:{function}",
                    "inputs": {"x": {}},
                    "outputs": {"value": {"type": output_type}},
                }
            ],
            "edges": [{"from": {"input": "x"}, "to": {"node": "node", "port": "x"}}],
            "outputs": {"result": {"from": {"node": "node", "port": "value"}}},
        }
    )


@pytest.mark.parametrize(
    ("function", "code"),
    [
        ("wrong_type", "RESULT_INVALID"),
        ("unpicklable_result", "NODE_FAILED"),
        ("unpicklable_exception", "NODE_FAILED"),
    ],
)
def test_remote_contract_and_serialization_failures(function: str, code: str) -> None:
    async def scenario() -> None:
        engine = Engine()
        key = await engine.deploy(
            single_spec(function, "number" if function == "wrong_type" else "any")
        )
        try:
            with pytest.raises(EngineError) as error:
                await engine.invoke(key, InvokeRequest(input={"x": 1}, timeout_s=5))
            assert error.value.code == code
            assert error.value.deployment_id == key
            assert error.value.invocation_id
            if function == "wrong_type":
                assert error.value.node_id == "node"
            assert engine.inflight == 0
        finally:
            await engine.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("retries", "exceptions", "count"), [(0, False, 1), (2, False, 1), (2, True, 3)]
)
def test_finite_ray_retries_are_explicit(
    tmp_path: Path, retries: int, exceptions: bool, count: int
) -> None:
    path = tmp_path / "attempts"
    runtime = Runtime(
        DAG(
            (),
            (
                Node(
                    "retry",
                    fail_twice,
                    {"x": Literal(str(path))},
                    pure=True,
                    max_retries=retries,
                    retry_exceptions=exceptions,
                ),
            ),
            {"result": Output("retry")},
        )
    )
    if count == 3:
        assert runtime.invoke({}) == {"result": 42}
    else:
        with pytest.raises(ray.exceptions.RayTaskError):
            runtime.invoke({})
    assert path.read_text() == str(count)


def test_partial_submission_cancels_already_submitted_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = runtime_module._execute.options
    submissions = 0

    def options(**kwargs: Any) -> Any:
        nonlocal submissions
        submissions += 1
        if submissions == 2:
            raise RuntimeError("synthetic submission failure")
        return original(**kwargs)

    monkeypatch.setattr(runtime_module._execute, "options", options)

    async def scenario() -> None:
        scope = Invocation()
        with pytest.raises(RuntimeError, match="synthetic submission"):
            await Runtime(arithmetic_spec().to_dag()).ainvoke({"x": 3}, scope=scope)
        assert scope.cleanup_complete and not scope.calls and not scope.futures

    asyncio.run(scenario())


def test_second_cancel_does_not_interrupt_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_cleanup(self: Invocation, grace_s: float) -> None:
            entered.set()
            await release.wait()
            self.cleanup_complete = True

        monkeypatch.setattr(Invocation, "_cleanup", slow_cleanup)
        scope = Invocation()
        task = asyncio.create_task(scope.cleanup(1))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        await asyncio.wait_for(task, 1)
        assert scope.cleanup_complete

    asyncio.run(scenario())


def test_undeploy_during_preparation_and_concurrent_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def preparing(self: Runtime, timeout_s: float) -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        monkeypatch.setattr(Runtime, "prepare", preparing)
        engine = Engine()
        task = asyncio.create_task(engine.deploy(arithmetic_spec()))
        await entered.wait()
        key = next(iter(engine.deployments))
        await asyncio.gather(engine.undeploy(key), engine.undeploy(key))
        with pytest.raises(EngineError):
            await task
        assert cleaned.is_set() and not engine.deployments
        await engine.close()

    asyncio.run(scenario())


def test_close_attempts_every_deployment_on_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        engine = Engine()
        first = await engine.deploy(arithmetic_spec())
        second = await engine.deploy(arithmetic_spec())
        original = engine._undeploy

        async def remove(key: str, deployment: Any) -> None:
            if key == first:
                raise EngineError(
                    "CLEANUP_INCOMPLETE", 503, "synthetic cleanup failure"
                )
            await original(key, deployment)

        monkeypatch.setattr(engine, "_undeploy", remove)
        with pytest.raises(EngineError):
            await engine.close()
        assert first in engine.deployments and second not in engine.deployments
        monkeypatch.setattr(engine, "_undeploy", original)
        await engine.close()

    asyncio.run(scenario())


def test_result_size_and_json_limits() -> None:
    async def scenario() -> None:
        engine = Engine(
            EngineConfig(limits=Limits(max_input_bytes=100, max_output_bytes=20))
        )
        key = await engine.deploy(single_spec())
        try:
            with pytest.raises(EngineError) as error:
                await engine.invoke(key, InvokeRequest(input={"x": "a" * 101}))
            assert error.value.status == 413
            with pytest.raises(EngineError) as error:
                await engine.invoke(key, InvokeRequest(input={"x": "a" * 30}))
            assert error.value.code == "RESULT_TOO_LARGE"
            with pytest.raises(EngineError) as error:
                # Engine also guards SDK callers that bypass model validation.
                await engine.invoke(
                    key, InvokeRequest.model_construct(input={"x": float("nan")})
                )
            assert error.value.code == "INPUT_INVALID"
            assert not engine.requests and engine.active == 0
        finally:
            await engine.close()

    asyncio.run(scenario())


def test_drain_rejects_waiting_and_new_requests() -> None:
    async def scenario() -> None:
        engine = Engine(
            EngineConfig(
                limits=Limits(max_active_invocations=1),
                timeouts=Timeouts(shutdown_grace_s=0.1, cancellation_grace_s=1),
            )
        )
        key = await engine.deploy(delay_spec())
        running = asyncio.create_task(
            engine.invoke(key, InvokeRequest(input={"x": 1, "seconds": 20}))
        )
        await asyncio.sleep(0.02)
        queued = asyncio.create_task(
            engine.invoke(key, InvokeRequest(input={"x": 2, "seconds": 0}))
        )
        await asyncio.sleep(0.02)
        removal = asyncio.create_task(engine.undeploy(key))
        with pytest.raises(EngineError) as error:
            await queued
        assert error.value.status == 503
        with pytest.raises(EngineError) as error:
            await engine.invoke(key, InvokeRequest(input={"x": 3, "seconds": 0}))
        assert error.value.status == 503
        with pytest.raises(EngineError) as error:
            await running
        assert error.value.code == "INVOCATION_CANCELLED"
        await removal
        assert not engine.requests and engine.inflight == 0
        await engine.close()

    asyncio.run(scenario())
