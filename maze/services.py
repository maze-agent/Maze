"""Attached non-streaming chat completions; no model processes or GPU ownership."""

import asyncio
import json
import os
from typing import Any

import httpx

from maze.graph import AsyncOperation
from maze.runtime import NodeResult
from maze.spec import ServiceSpec, check_json


class ChatCompletion(AsyncOperation):
    def __init__(self, spec: ServiceSpec) -> None:
        self.spec = spec
        self.client: httpx.AsyncClient | None = None

    async def ready(self) -> None:
        if self.client is not None:
            return
        headers = {}
        if self.spec.api_key_env:
            token = os.environ.get(self.spec.api_key_env)
            if not token:
                raise ValueError(
                    "Service API key environment variable is missing on worker"
                )
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.AsyncClient(
            base_url=self.spec.endpoint.rstrip("/") + "/",
            headers=headers,
            timeout=self.spec.timeout_s,
            transport=httpx.AsyncHTTPTransport(
                retries=0,
                limits=httpx.Limits(
                    max_connections=self.spec.max_connections,
                    max_keepalive_connections=self.spec.max_connections,
                ),
            ),
            follow_redirects=False,
            trust_env=False,
        )
        try:
            data = await self._request("GET", "models")
            if not any(
                item.get("id") == self.spec.model for item in data.get("data", [])
            ):
                raise ValueError("Configured model is not advertised by service")
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        if self.client is None:
            raise RuntimeError("Service adapter is not ready")
        # Total timeout also bounds pool waiting and slow, continuously arriving bytes.
        async with asyncio.timeout(self.spec.timeout_s):
            async with self.client.stream(method, path, json=body) as response:
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > self.spec.max_response_bytes:
                        raise ValueError(
                            "Service response exceeds configured size limit"
                        )
                data = json.loads(content)
                check_json(data)
                if not isinstance(data, dict):
                    raise ValueError("Expected a JSON service response object")
                return data

    async def __call__(
        self,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> NodeResult:
        if not isinstance(messages, list) or not messages:
            raise ValueError("Expected nonempty text chat messages")
        for message in messages:
            if (
                not isinstance(message, dict)
                or set(message) != {"role", "content"}
                or message["role"] not in ("system", "user", "assistant")
                or not isinstance(message["content"], str)
            ):
                raise ValueError(
                    "Only system, user and assistant text messages are supported"
                )
        body: dict[str, Any] = {
            "model": self.spec.model,
            "messages": messages,
            "stream": False,
            **self.parameters(max_tokens, temperature),
        }
        response = await self._request("POST", "chat/completions", body)
        return self.result(response)

    def parameters(
        self, max_tokens: int | None = None, temperature: float | None = None
    ) -> dict[str, Any]:
        tokens = self.spec.max_output_tokens if max_tokens is None else max_tokens
        if type(tokens) is not int or not 1 <= tokens <= self.spec.max_output_tokens:
            raise ValueError("max_tokens exceeds the declared service bounds")
        body: dict[str, Any] = {"max_tokens": tokens}
        if temperature is not None:
            if type(temperature) not in (int, float) or not 0 <= temperature <= 2:
                raise ValueError("temperature must be between 0 and 2")
            body["temperature"] = temperature
        return body

    def result(self, response: dict[str, Any]) -> NodeResult:
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("Expected one non-streaming chat choice")
        text = choices[0]["message"]["content"]
        if not isinstance(text, str):
            raise ValueError("Expected text chat content")
        metadata: dict[str, Any] = {"requested_model": self.spec.model}
        if "model" in response:
            if not isinstance(response["model"], str):
                raise ValueError("Expected model name")
            metadata["model"] = response["model"]
        usage = response.get("usage")
        if usage is not None:
            if not isinstance(usage, dict):
                raise ValueError("Invalid token usage")
            counts = {}
            for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if name in usage:
                    if type(usage[name]) is not int or usage[name] < 0:
                        raise ValueError("Invalid token usage")
                    counts[name] = usage[name]
            metadata["usage"] = counts
        return NodeResult(
            outputs={"text": text, "response": response}, metadata={"llm": metadata}
        )
