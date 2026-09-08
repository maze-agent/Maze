"""Real TCP fixture + real Ray Actors. No model downloads or GPU inference."""

import asyncio
import json
from collections.abc import Iterator
from contextlib import suppress
from typing import Any

import pytest
import ray

from maze.engine import Engine, EngineError
from maze.spec import DagSpec, DeploymentSpec, InvokeRequest, ServiceSpec


@pytest.fixture(scope="module", autouse=True)
def cluster() -> Iterator[None]:
    assert not ray.is_initialized()
    ray.init(
        address="local",
        num_cpus=2,
        object_store_memory=256 * 1024**2,
        include_dashboard=False,
        runtime_env={"env_vars": {"MAZE_FIXTURE_MODEL_TOKEN": "test-service-only"}},
    )
    yield
    ray.shutdown()


class ChatServer:
    """Small HTTP/1.1 keepalive peer, with explicit gates for cancellation tests."""

    def __init__(self) -> None:
        self.writers: set[asyncio.StreamWriter] = set()
        self.requests: list[dict[str, Any]] = []
        self.peers: set[tuple] = set()
        self.active = 0
        self.peak = 0
        self.held = asyncio.Event()
        self.release = asyncio.Event()
        self.paired = asyncio.Event()
        self.pairs = 0
        self.require_auth = False

    async def __aenter__(self) -> "ChatServer":
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.endpoint = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/v1"
        return self

    async def __aexit__(self, *args: object) -> None:
        self.release.set()
        self.paired.set()
        self.server.close()
        await self.server.wait_closed()
        for writer in tuple(self.writers):
            writer.close()
            await writer.wait_closed()

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.writers.add(writer)
        self.peers.add(writer.get_extra_info("peername"))
        try:
            while True:
                header = (await reader.readuntil(b"\r\n\r\n")).decode()
                lines = header.split("\r\n")
                method, path, _ = lines[0].split()
                headers = dict(
                    line.split(": ", 1) for line in lines[1:] if ": " in line
                )
                size = int(
                    next(
                        (
                            v
                            for k, v in headers.items()
                            if k.lower() == "content-length"
                        ),
                        "0",
                    )
                )
                payload = await reader.readexactly(size)
                status = 200
                if method == "GET" and path == "/v1/models":
                    data = {"data": [{"id": "fixture-model"}]}
                else:
                    assert method == "POST" and path == "/v1/chat/completions"
                    body = json.loads(payload)
                    self.requests.append(body)
                    text = body["messages"][0]["content"]
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                    try:
                        if text == "hold":
                            self.held.set()
                            await self.release.wait()
                        if text == "pair":
                            self.pairs += 1
                            if self.pairs == 2:
                                self.paired.set()
                            await asyncio.wait_for(self.paired.wait(), 5)
                        await asyncio.sleep(0.01)
                    finally:
                        self.active -= 1
                    data = {
                        "model": "fixture-model",
                        "choices": [{"message": {"content": text}}],
                        "usage": {
                            "prompt_tokens": 2,
                            "completion_tokens": 1,
                            "total_tokens": 3,
                        },
                    }
                    if text == "missing_usage":
                        data.pop("usage")
                    elif text == "large":
                        data["extra"] = "x" * 10000
                    elif text == "error":
                        status, data = 503, {"error": "fixture failure"}
                if self.require_auth and not any(
                    k.lower() == "authorization" and v == "Bearer test-service-only"
                    for k, v in headers.items()
                ):
                    status, data = 401, {"error": "unauthorized"}
                encoded = json.dumps(data).encode()
                writer.write(
                    (
                        f"HTTP/1.1 {status} Test\r\nContent-Type: application/json\r\n"
                        f"Content-Length: {len(encoded)}\r\n\r\n"
                    ).encode()
                    + encoded
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.writers.discard(writer)
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()


def chat_spec() -> DagSpec:
    return DagSpec.model_validate(
        {
            "schema": "maze.dag/v1",
            "name": "chat",
            "inputs": {"messages": {"type": "array"}},
            "nodes": [
                {
                    "id": "chat",
                    "kind": "llm",
                    "service_id": "model",
                    "execution": "async_actor",
                    "resources": {"num_cpus": 0},
                    "max_concurrency": 2,
                    "inputs": {"messages": {"type": "array"}},
                    "outputs": {
                        "text": {"type": "string"},
                        "response": {"type": "object"},
                    },
                }
            ],
            "edges": [
                {
                    "from": {"input": "messages"},
                    "to": {"node": "chat", "port": "messages"},
                }
            ],
            "outputs": {
                "text": {"from": {"node": "chat", "port": "text"}, "type": "string"},
                "response": {
                    "from": {"node": "chat", "port": "response"},
                    "type": "object",
                },
            },
        }
    )


def request(text: str, **options: Any) -> InvokeRequest:
    return InvokeRequest(
        input={"messages": [{"role": "user", "content": text}]}, **options
    )


def settings(server: ChatServer, **options: Any) -> DeploymentSpec:
    return DeploymentSpec(
        services=(
            ServiceSpec(
                service_id="model",
                model="fixture-model",
                endpoint=server.endpoint,
                max_output_tokens=16,
                **options,
            ),
        )
    )


def test_attached_reuses_connections_and_keeps_usage_per_request() -> None:
    async def scenario() -> None:
        async with ChatServer() as server:
            engine = Engine()
            key = await engine.deploy(chat_spec(), settings(server))
            try:
                await engine.deployments[key].runtime.prepare()
                result = await engine.invoke(key, request("hello"))
                assert result["output"]["text"] == "hello"
                metadata = result["metrics"]["nodes"]["chat"]["metadata"]["llm"]
                assert metadata["usage"]["total_tokens"] == 3
                missing = await engine.invoke(key, request("missing_usage"))
                assert (
                    "usage"
                    not in missing["metrics"]["nodes"]["chat"]["metadata"]["llm"]
                )
                assert (
                    len(server.peers) == 1
                )  # /models + both calls reuse TCP connection.
                assert all(
                    r["max_tokens"] == 16 and r["stream"] is False
                    for r in server.requests
                )
                results = await asyncio.gather(
                    *(engine.invoke(key, request("pair")) for _ in range(2))
                )
                assert all(r["output"]["text"] == "pair" for r in results)
                assert server.peak == 2
                assert not engine.requests
            finally:
                await engine.close()
            for _ in range(100):
                if not server.writers:
                    break
                await asyncio.sleep(0.01)
            assert not server.writers
            # Attached service still accepts clients after deployment removal.
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", server.server.sockets[0].getsockname()[1]
            )
            writer.close()
            await writer.wait_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize("timeout", [False, True])
def test_attached_cancel_closes_client_request_without_killing_sibling(
    timeout: bool,
) -> None:
    async def scenario() -> None:
        async with ChatServer() as server:
            engine = Engine()
            key = await engine.deploy(chat_spec(), settings(server))
            try:
                held = asyncio.create_task(
                    engine.invoke(
                        key,
                        request(
                            "hold", invocation_id="held", timeout_s=1 if timeout else 10
                        ),
                    )
                )
                await asyncio.wait_for(server.held.wait(), 5)
                sibling = asyncio.create_task(engine.invoke(key, request("other")))
                if not timeout:
                    engine.cancel("held")
                with pytest.raises(EngineError) as error:
                    await held
                assert error.value.code == (
                    "INVOCATION_TIMEOUT" if timeout else "INVOCATION_CANCELLED"
                )
                assert error.value.backend_cleanup == "unconfirmed"
                assert (await sibling)["output"]["text"] == "other"
                assert not engine.requests
                # Backend work intentionally outlives cancellation on the client.
                assert server.active == 1
                assert (await engine.invoke(key, request("again")))["output"][
                    "text"
                ] == "again"
            finally:
                server.release.set()
                await engine.close()

    asyncio.run(scenario())


def test_attached_failure_timeout_and_response_limit_have_no_implicit_retries() -> None:
    async def scenario() -> None:
        async with ChatServer() as server:
            engine = Engine()
            key = await engine.deploy(
                chat_spec(), settings(server, timeout_s=0.3, max_response_bytes=1000)
            )
            try:
                for text in ("error", "large", "hold"):
                    with pytest.raises(EngineError) as error:
                        await engine.invoke(key, request(text))
                    assert error.value.code == "NODE_FAILED"
                    assert error.value.node_id == "chat"
                    assert (
                        len(
                            [
                                r
                                for r in server.requests
                                if r["messages"][0]["content"] == text
                            ]
                        )
                        == 1
                    )
                assert not engine.requests
            finally:
                server.release.set()
                await engine.close()

    asyncio.run(scenario())


def test_attached_readiness_failure_never_becomes_ready() -> None:
    async def scenario() -> None:
        async with ChatServer() as server:
            engine = Engine()
            config = settings(server)
            service = config.services[0].model_copy(update={"model": "absent"})
            try:
                with pytest.raises(EngineError) as error:
                    await engine.deploy(
                        chat_spec(), DeploymentSpec(services=(service,))
                    )
                assert error.value.code == "DEPLOYMENT_NOT_READY"
                assert all(
                    d.state == "failed" and not d.runtime.actors
                    for d in engine.deployments.values()
                )
            finally:
                await engine.close()

    asyncio.run(scenario())


def test_pool_limit_bounds_overlapping_requests() -> None:
    async def scenario() -> None:
        async with ChatServer() as server:
            engine = Engine()
            key = await engine.deploy(chat_spec(), settings(server, max_connections=1))
            try:
                results = await asyncio.gather(
                    *(engine.invoke(key, request(str(n))) for n in range(6))
                )
                assert [r["output"]["text"] for r in results] == [
                    str(n) for n in range(6)
                ]
                assert server.peak == 1 and len(server.peers) == 1
            finally:
                await engine.close()

    asyncio.run(scenario())


def test_generation_bounds_and_input_endpoint_cannot_be_overridden() -> None:
    async def scenario() -> None:
        async with ChatServer() as server:
            engine = Engine()
            data = chat_spec().document()
            data["inputs"]["max_tokens"] = {"type": "integer"}
            data["nodes"][0]["inputs"]["max_tokens"] = {"type": "integer"}
            data["edges"].append(
                {
                    "from": {"input": "max_tokens"},
                    "to": {"node": "chat", "port": "max_tokens"},
                }
            )
            key = await engine.deploy(DagSpec.model_validate(data), settings(server))
            try:
                for value in (0, 17):
                    body = request("hello").input | {"max_tokens": value}
                    with pytest.raises(EngineError) as error:
                        await engine.invoke(key, InvokeRequest(input=body))
                    assert error.value.code == "NODE_FAILED"
                with pytest.raises(EngineError) as error:
                    await engine.invoke(
                        key,
                        InvokeRequest(
                            input=request("hello").input
                            | {"max_tokens": 1, "endpoint": "http://invalid"}
                        ),
                    )
                assert error.value.code == "INPUT_INVALID"
                assert not server.requests
                result = await engine.invoke(
                    key, InvokeRequest(input=request("ok").input | {"max_tokens": 5})
                )
                assert result["output"]["text"] == "ok"
                assert server.requests[0]["max_tokens"] == 5
            finally:
                await engine.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"resources": {"num_gpus": 1}},
        {"execution": "task"},
        {"callable": "maze_examples.arithmetic:double"},
        {"service_id": None},
        {"retry": {"pure": True, "max_retries": 1}},
        {"outputs": {"text": {"type": "number"}, "response": {"type": "object"}}},
    ],
)
def test_llm_rejects_invalid_deployment_contract(change: dict) -> None:
    data = chat_spec().document()
    data["nodes"][0].update(change)
    with pytest.raises(ValueError):
        DagSpec.model_validate(data)


def test_unknown_duplicate_services_and_invalid_defaults_fail_before_preparation() -> (
    None
):
    service = ServiceSpec(
        service_id="model",
        model="fixture-model",
        endpoint="http://127.0.0.1:1/v1",
        max_output_tokens=16,
    )
    with pytest.raises(ValueError, match="Unknown LLM service"):
        chat_spec().to_dag()
    with pytest.raises(ValueError, match="Duplicate service"):
        chat_spec().to_dag((service, service))
    data = chat_spec().document()
    data["nodes"][0]["inputs"]["max_tokens"] = {"type": "integer", "default": 17}
    with pytest.raises(ValueError, match="max_tokens"):
        DagSpec.model_validate(data).to_dag((service,))
    for endpoint in (
        "file:///tmp/model",
        "http://user:secret@host/v1",
        "http://host:bad/v1",
        "http://host/v1?api_key=secret",
    ):
        with pytest.raises(ValueError):
            ServiceSpec(service_id="model", model="model", endpoint=endpoint)


def test_worker_credentials_and_failed_preparation_are_isolated() -> None:
    async def scenario() -> None:
        async with ChatServer() as server:
            server.require_auth = True
            engine = Engine()
            key = await engine.deploy(
                chat_spec(), settings(server, api_key_env="MAZE_FIXTURE_MODEL_TOKEN")
            )
            try:
                assert (await engine.invoke(key, request("authorized")))["output"][
                    "text"
                ] == "authorized"
                with pytest.raises(EngineError) as error:
                    await engine.deploy(
                        chat_spec(),
                        settings(server, api_key_env="MAZE_FIXTURE_ABSENT_TOKEN"),
                    )
                assert error.value.code == "DEPLOYMENT_NOT_READY"
                assert (await engine.invoke(key, request("still_alive")))["output"][
                    "text"
                ] == "still_alive"
            finally:
                await engine.close()

    asyncio.run(scenario())
