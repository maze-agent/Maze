"""Thin HTTP adapter; business state lives exclusively in Engine."""

import asyncio
import hmac
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from maze.engine import Engine, EngineError
from maze.spec import DagSpec, DeploymentSpec, InvokeRequest, Model, parse_document


class DeployRequest(Model):
    dag: DagSpec
    deployment: DeploymentSpec = DeploymentSpec()


class TokenAuth:
    """Authenticate without wrapping ASGI receive or consuming disconnect events."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not hmac.compare_digest(
            Headers(scope=scope).get("authorization", "").encode(), self.expected
        ):
            response = JSONResponse(
                status_code=401, content={"error": {"code": "UNAUTHORIZED"}}
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_app(
    engine: Engine, token: str, stop: Callable[[], None] | None = None
) -> FastAPI:
    app = FastAPI(title="Maze v2", version="2.0.0.dev0")
    app.add_middleware(TokenAuth, token=token)

    @app.exception_handler(EngineError)
    async def engine_error(request: Request, exc: EngineError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "cleanup": exc.cleanup,
                    "invocation_id": exc.invocation_id,
                    "deployment_id": exc.deployment_id,
                    "node_id": exc.node_id,
                    "retryable": exc.code
                    in ("OVERLOADED", "DEPLOYMENT_NOT_READY", "ENGINE_UNAVAILABLE"),
                }
            },
        )

    async def body(request: Request, model: type[Model]) -> Any:
        maximum = engine.config.limits.max_input_bytes + 65536
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > maximum:
                raise EngineError("INPUT_TOO_LARGE", 413, "Request body too large")
        try:
            return model.model_validate(parse_document(data.decode(), maximum))
        except (
            ValueError,
            TypeError,
            ValidationError,
            UnicodeError,
            yaml.YAMLError,
        ) as exc:
            raise EngineError("INPUT_INVALID", 422, "Invalid request document") from exc

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/readyz")
    async def ready() -> dict[str, str]:
        if not engine.accepting:
            raise EngineError("ENGINE_UNAVAILABLE", 503, "Engine is stopping")
        return {"status": "ready"}

    @app.post("/v1/deployments")
    async def deploy(request: Request) -> dict[str, Any]:
        data = await body(request, DeployRequest)
        key = await engine.deploy(data.dag, data.deployment)
        return engine.show(key)

    @app.get("/v1/deployments")
    async def deployments() -> list[dict[str, Any]]:
        return [engine.show(key) for key in engine.deployments]

    @app.get("/v1/deployments/{key}")
    async def show(key: str) -> dict[str, Any]:
        return engine.show(key)

    @app.post("/v1/deployments/{key}/invoke")
    async def invoke(key: str, request: Request) -> dict[str, Any]:
        data = await body(request, InvokeRequest)
        task = asyncio.create_task(engine.invoke(key, data))
        try:
            while not task.done():
                if await request.is_disconnected():
                    if not task.cancelling():
                        task.cancel()
                    break
                await asyncio.wait({task}, timeout=0.05)
            return await task
        finally:
            if not task.done():
                if not task.cancelling():
                    task.cancel()
                with suppress(EngineError, asyncio.CancelledError):
                    await task

    @app.post("/v1/invocations/{key}/cancel")
    async def cancel(key: str) -> dict[str, str]:
        engine.cancel(key)
        return {"status": "cancelling"}

    @app.delete("/v1/deployments/{key}")
    async def undeploy(key: str) -> dict[str, str]:
        await engine.undeploy(key)
        return {"status": "removed"}

    @app.post("/v1/stop")
    async def shutdown() -> dict[str, str]:
        await engine.close()
        if stop:
            stop()
        return {"status": "stopping"}

    # Bounded manual parsing avoids FastAPI echoing invalid secret-bearing bodies.
    # Explicit schemas keep this adapter's actual request contracts in OpenAPI.
    original = app.openapi

    def openapi() -> dict[str, Any]:
        schema = original()
        for path, model in (
            ("/v1/deployments", DeployRequest),
            ("/v1/deployments/{key}/invoke", InvokeRequest),
        ):
            document = model.model_json_schema(
                ref_template="#/components/schemas/{model}"
            )
            schema.setdefault("components", {}).setdefault("schemas", {}).update(
                document.pop("$defs", {})
            )
            schema["paths"][path]["post"]["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": document}},
            }
        return schema

    app.openapi = openapi
    return app
