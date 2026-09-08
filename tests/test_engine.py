import asyncio
from collections.abc import Iterator

import httpx
import pytest
import ray

from maze.api import create_app
from maze.engine import Engine, EngineError
from maze.spec import DagSpec, EngineConfig, InvokeRequest, Limits, Timeouts
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


def delay_spec() -> DagSpec:
    return DagSpec.model_validate(
        {
            "schema": "maze.dag/v1",
            "name": "delay",
            "inputs": {"x": {"type": "number"}, "seconds": {"type": "number"}},
            "nodes": [
                {
                    "id": "delay",
                    "callable": "maze_examples.arithmetic:delay",
                    "execution": "async_actor",
                    "resources": {"num_cpus": 0},
                    "inputs": {"x": {"type": "number"}, "seconds": {"type": "number"}},
                    "outputs": {"value": {"type": "number"}},
                }
            ],
            "edges": [
                {"from": {"input": name}, "to": {"node": "delay", "port": name}}
                for name in ("x", "seconds")
            ],
            "outputs": {
                "result": {"from": {"node": "delay", "port": "value"}, "type": "number"}
            },
        }
    )


def test_async_actor_metrics_and_cancel_isolation() -> None:
    async def scenario() -> None:
        engine = Engine()
        key = await engine.deploy(delay_spec())
        try:
            a = asyncio.create_task(
                engine.invoke(
                    key, InvokeRequest(input={"x": 1, "seconds": 5}, invocation_id="a")
                )
            )
            b = asyncio.create_task(
                engine.invoke(
                    key,
                    InvokeRequest(input={"x": 2, "seconds": 0.05}, invocation_id="b"),
                )
            )
            await asyncio.sleep(0.03)
            engine.cancel("a")
            with pytest.raises(EngineError) as error:
                await a
            assert error.value.code == "INVOCATION_CANCELLED"
            result = await b
            assert result["output"] == {"result": 2}
            assert "delay" in result["metrics"]["nodes"]
            assert (
                await engine.invoke(
                    key, InvokeRequest(input={"x": 3, "seconds": 0}, metrics="none")
                )
            )["output"] == {"result": 3}
            assert not engine.requests
        finally:
            await engine.close()

    asyncio.run(scenario())


def test_queue_limit_deadline_and_budget_release() -> None:
    async def scenario() -> None:
        engine = Engine(
            EngineConfig(
                limits=Limits(max_active_invocations=1, max_waiting_invocations=1),
                timeouts=Timeouts(cancellation_grace_s=0.5),
            )
        )
        key = await engine.deploy(delay_spec())
        try:
            active = asyncio.create_task(
                engine.invoke(
                    key,
                    InvokeRequest(
                        input={"x": 1, "seconds": 0.5}, invocation_id="active"
                    ),
                )
            )
            await asyncio.sleep(0.03)
            queued = asyncio.create_task(
                engine.invoke(
                    key,
                    InvokeRequest(
                        input={"x": 2, "seconds": 0},
                        invocation_id="queued",
                        timeout_s=0.1,
                    ),
                )
            )
            await asyncio.sleep(0.02)
            with pytest.raises(EngineError) as error:
                await engine.invoke(key, InvokeRequest(input={"x": 3, "seconds": 0}))
            assert error.value.status == 429
            with pytest.raises(EngineError) as error:
                await queued
            assert error.value.status == 504
            await active
            assert not engine.requests and engine.inflight == engine.active == 0
            with pytest.raises(EngineError) as error:
                await engine.invoke(
                    key, InvokeRequest(input={"x": 4, "seconds": 5}, timeout_s=0.05)
                )
            assert error.value.status == 504
        finally:
            await engine.close()

    asyncio.run(scenario())


def test_http_deploy_invoke_validation_and_undeploy() -> None:
    async def scenario() -> None:
        engine = Engine()
        app = create_app(engine, "test-only-token")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/readyz")).status_code == 401
            client.headers["authorization"] = "Bearer test-only-token"
            assert (await client.get("/readyz")).status_code == 200
            response = await client.post(
                "/v1/deployments",
                json={
                    "dag": arithmetic_spec().model_dump(
                        by_alias=True, exclude_unset=True
                    )
                },
            )
            assert response.status_code == 200, response.text
            key = response.json()["deployment_id"]
            result = await client.post(
                f"/v1/deployments/{key}/invoke", json={"input": {"x": 3}}
            )
            assert result.status_code == 200, result.text
            assert result.json()["output"] == {"result": 10}
            invalid = await client.post(
                f"/v1/deployments/{key}/invoke", json={"input": {"x": "SECRET"}}
            )
            assert invalid.status_code == 422 and "SECRET" not in invalid.text
            assert (await client.delete(f"/v1/deployments/{key}")).status_code == 200
            assert (await client.get(f"/v1/deployments/{key}")).status_code == 404
            assert (await client.get("/openapi.json")).status_code == 200
        await engine.close()

    asyncio.run(scenario())


def test_repeated_calls_release_state_and_deployments_are_isolated() -> None:
    async def scenario() -> None:
        engine = Engine()
        first = await engine.deploy(arithmetic_spec())
        second = await engine.deploy(arithmetic_spec())
        try:
            for _ in range(5):
                results = await asyncio.gather(
                    *(
                        engine.invoke(
                            first, InvokeRequest(input={"x": n}, metrics="detailed")
                        )
                        for n in range(8)
                    )
                )
                assert [r["output"]["result"] for r in results] == [
                    3 * n + 1 for n in range(8)
                ]
                assert results[0]["metrics"]["missing_fields"] == ["ray_events"]
                assert not engine.requests and not engine.waiting
                assert engine.active == engine.inflight == 0
            await engine.undeploy(first)
            assert (await engine.invoke(second, InvokeRequest(input={"x": 5})))[
                "output"
            ] == {"result": 16}
        finally:
            await engine.close()

    asyncio.run(scenario())
