"""Legacy MCP tool wrapper.

This module is not part of the Maze Core Runtime public boundary. It remains
temporarily for compatibility and should move to an extension or be removed in
a later purification phase.
"""

from maze.mcp.base_client import BaseClient

class McpTool:
    def __init__(self, mcp_client:BaseClient, tool_name:str):
        self.mcp_client = mcp_client
        self.tool_name = tool_name

    async def __call__(self, **kwargs):
        return await self.mcp_client.call_tool(self.tool_name, **kwargs)
