from typing import Any, Literal
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from maze.mcp.base_client import BaseClient

class HttpClient(BaseClient):
    def __init__(
        self,
        name: str,
        transport: Literal["streamable_http", "sse"],
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
        sse_read_timeout: float = 60 * 5,
        **client_kwargs: Any,
    ) -> None:
        assert transport in ["streamable_http", "sse"]
        self.transport = transport

        if self.transport == "streamable_http":
            self.client = streamablehttp_client(
                url=url,
                headers=headers,
                timeout=timeout,
                sse_read_timeout=sse_read_timeout,
                **client_kwargs,
            )
        else:
            self.client = sse_client(
                url=url,
                headers=headers,
                timeout=timeout,
                sse_read_timeout=sse_read_timeout,
                **client_kwargs,
            )
