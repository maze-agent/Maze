from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class AgentMCPServerConfig:
    """Configuration for one MCP server used by an agent run."""

    name: str
    transport: str = "stdio"
    command: str | None = None
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] | None = None
    cwd: str | None = None
    url: str | None = None
    headers: Dict[str, str] | None = None
    timeout: float = 30
    tool_prefix: str | None = None


class AgentMCPTool:
    def __init__(
        self,
        *,
        server_name: str,
        tool_name: str,
        agent_tool_name: str,
        description: str,
        input_schema: Dict[str, Any],
        output_schema: Dict[str, Any] | None,
        client: Any,
        loop_runner: "_AsyncLoopThread | None" = None,
    ):
        self.server_name = server_name
        self.tool_name = tool_name
        self.agent_tool_name = agent_tool_name
        self.description = description
        self.input_schema = input_schema or {"type": "object", "properties": {}}
        self.output_schema = output_schema
        self.client = client
        self.loop_runner = loop_runner

    def __call__(self, **kwargs):
        if self.loop_runner is not None:
            return self.loop_runner.run(self.client.call_tool(self.tool_name, **kwargs))
        return run_async_blocking(self.client.call_tool(self.tool_name, **kwargs))


class AgentMCPClientManager:
    def __init__(self, clients: List[Any] | None = None, configs: List[AgentMCPServerConfig | dict] | None = None):
        self.clients = list(clients or [])
        self.configs = [
            config if isinstance(config, AgentMCPServerConfig) else AgentMCPServerConfig(**config)
            for config in (configs or [])
        ]
        self._owned_clients: List[Any] = []
        self._connected_clients: List[Any] = []
        self._loop_runner = _AsyncLoopThread()

    def connect_blocking(self) -> None:
        self._loop_runner.run(self.connect())

    def discover_tools_blocking(self) -> List[AgentMCPTool]:
        return self._loop_runner.run(self.discover_tools())

    def close_blocking(self) -> None:
        try:
            self._loop_runner.run(self.close())
        finally:
            self._loop_runner.stop()

    async def connect(self) -> None:
        for config in self.configs:
            client = self._client_from_config(config)
            client._maze_agent_tool_prefix = config.tool_prefix
            self.clients.append(client)
            self._owned_clients.append(client)

        for client in self.clients:
            try:
                if not getattr(client, "is_connected", False):
                    await client.connect()
                if client not in self._connected_clients:
                    self._connected_clients.append(client)
            except Exception:
                await self.close()
                raise

    async def close(self) -> None:
        for client in reversed(self._connected_clients):
            try:
                if getattr(client, "is_connected", False):
                    await client.close()
            except Exception:
                pass
        self._connected_clients = []

    async def discover_tools(self) -> List[AgentMCPTool]:
        discovered = []
        used_agent_tool_names = set()
        for client in self.clients:
            server_name = getattr(client, "name", None) or getattr(client, "server_name", None) or "mcp"
            tool_prefix = getattr(client, "_maze_agent_tool_prefix", None) or server_name
            tools = await client.list_tools()
            for tool in tools:
                tool_name = str(getattr(tool, "name", ""))
                if not tool_name:
                    continue
                agent_tool_name = _unique_agent_tool_name(
                    _agent_tool_name(tool_prefix, tool_name),
                    used_agent_tool_names,
                )
                discovered.append(
                    AgentMCPTool(
                        server_name=server_name,
                        tool_name=tool_name,
                        agent_tool_name=agent_tool_name,
                        description=str(getattr(tool, "description", "") or ""),
                        input_schema=dict(getattr(tool, "inputSchema", None) or {}),
                        output_schema=getattr(tool, "outputSchema", None),
                        client=client,
                        loop_runner=self._loop_runner,
                    )
                )
        return discovered

    def _client_from_config(self, config: AgentMCPServerConfig):
        if config.transport == "stdio":
            from maze.mcp import StdIOClient

            if not config.command:
                raise ValueError(f"MCP stdio server {config.name} requires command")
            return StdIOClient(
                name=config.name,
                command=config.command,
                args=config.args,
                env=config.env,
                cwd=config.cwd,
            )

        if config.transport in {"streamable_http", "sse"}:
            from maze.mcp import HttpClient

            if not config.url:
                raise ValueError(f"MCP HTTP server {config.name} requires url")
            return HttpClient(
                name=config.name,
                transport=config.transport,
                url=config.url,
                headers=config.headers,
                timeout=config.timeout,
            )

        raise ValueError(f"Unsupported MCP transport: {config.transport}")


def discover_mcp_tools_blocking(
    *,
    clients: List[Any] | None = None,
    configs: List[AgentMCPServerConfig | dict] | None = None,
) -> tuple[AgentMCPClientManager | None, List[AgentMCPTool]]:
    if not clients and not configs:
        return None, []
    manager = AgentMCPClientManager(clients=clients, configs=configs)
    try:
        manager.connect_blocking()
        return manager, manager.discover_tools_blocking()
    except Exception:
        close_mcp_manager_blocking(manager)
        raise


def close_mcp_manager_blocking(manager: AgentMCPClientManager | None) -> None:
    if manager is not None:
        manager.close_blocking()


class _AsyncLoopThread:
    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._start_error: BaseException | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return

        self._started.clear()
        self._start_error = None
        self._thread = threading.Thread(
            target=self._run_loop,
            name="maze-agent-mcp-loop",
            daemon=True,
        )
        self._thread.start()
        self._started.wait(timeout=10)
        if self._start_error is not None:
            raise RuntimeError("Failed to start MCP event loop") from self._start_error
        if self._loop is None:
            raise RuntimeError("Timed out starting MCP event loop")

    def run(self, awaitable):
        self.start()
        if self._loop is None:
            raise RuntimeError("MCP event loop is not available")
        future = asyncio.run_coroutine_threadsafe(awaitable, self._loop)
        return future.result()

    def stop(self) -> None:
        loop = self._loop
        thread = self._thread
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive():
            thread.join(timeout=10)
        self._loop = None
        self._thread = None

    def _run_loop(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._started.set()
            loop.run_forever()
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
        except BaseException as exc:
            self._start_error = exc
            self._started.set()
            raise


def run_async_blocking(awaitable):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    raise RuntimeError(
        "Maze MCP agent integration cannot run inside an already running asyncio loop yet. "
        "Use the synchronous Python SDK entry point from a normal thread/process."
    )


def normalize_mcp_tool_result(result: Any) -> Dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    content = getattr(result, "content", None)
    is_error = bool(getattr(result, "isError", False))
    normalized_content = [_normalize_mcp_content(item) for item in (content or [])]
    return {
        "structured_content": structured,
        "content": normalized_content,
        "is_error": is_error,
    }


def _normalize_mcp_content(item: Any) -> Dict[str, Any]:
    item_type = getattr(item, "type", None)
    if item_type == "text":
        return {"type": "text", "text": getattr(item, "text", "")}
    if item_type == "image":
        return {
            "type": "image",
            "data": getattr(item, "data", None),
            "mimeType": getattr(item, "mimeType", None),
        }
    if item_type == "audio":
        return {
            "type": "audio",
            "data": getattr(item, "data", None),
            "mimeType": getattr(item, "mimeType", None),
        }
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json")
    return {"type": str(item_type or type(item).__name__), "value": str(item)}


def _agent_tool_name(server_name: str, tool_name: str) -> str:
    safe_server = _safe_tool_part(server_name)
    safe_tool = _safe_tool_part(tool_name)
    return f"{safe_server}__{safe_tool}"


def _unique_agent_tool_name(base_name: str, used_names: set[str]) -> str:
    if base_name not in used_names:
        used_names.add(base_name)
        return base_name

    suffix = 2
    while f"{base_name}_{suffix}" in used_names:
        suffix += 1
    unique_name = f"{base_name}_{suffix}"
    used_names.add(unique_name)
    return unique_name


def _safe_tool_part(value: str) -> str:
    safe = "".join(char if char.isalnum() or char == "_" else "_" for char in str(value))
    safe = safe.strip("_")
    return safe or "mcp"
