from abc import ABC
from contextlib import AsyncExitStack
from typing import List
import mcp
from mcp import ClientSession
#from ._mcp_function import MCPToolFunction

class BaseClient():
    is_connected: bool

    def __init__(self, name: str) -> None:
        self.client = None
        self.stack = None
        self.session = None
        self.is_connected = False
        self._cached_tools = None

    async def connect(self) -> None:
        """Connect to MCP server."""
        if self.is_connected:
            raise RuntimeError(
                "The MCP server is already connected. Call close() "
                "before connecting again.",
            )

        self.stack = AsyncExitStack()

        try:
            context = await self.stack.enter_async_context(
                self.client,
            )
            read_stream, write_stream = context[0], context[1]
            self.session = ClientSession(read_stream, write_stream)
            await self.stack.enter_async_context(self.session)
            await self.session.initialize()

            self.is_connected = True
            print("MCP client connected.")
        except Exception:
            await self.stack.aclose()
            self.stack = None
            raise

    async def close(self, ignore_errors: bool = True) -> None:
        """Clean up the MCP client resources. You must call this method when
        your application is done.

        Args:
            ignore_errors (`bool`):
                Whether to ignore errors during cleanup. Defaults to `True`.
        """
        if not self.is_connected:
            raise RuntimeError(
                "The MCP server is not connected. Call connect() before "
                "closing.",
            )

        try:
            await self.stack.aclose()
        except Exception as e:
            if not ignore_errors:
                raise e
            print(e)
        finally:
            self.stack = None
            self.session = None
            self.is_connected = False

    async def call_tool(self, tool_name: str, **kwargs):
        print(f"Calling tool: {tool_name}")
        print(f"Arguments: {kwargs}")
        res = await self.session.call_tool(
            tool_name,
            arguments=kwargs,
        )
        return res
        
    async def list_tools(self) -> List[mcp.types.Tool]:
        res = await self.session.list_tools()

        # Cache the tools for later use
        self._cached_tools = res.tools
        return res.tools