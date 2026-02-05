from maze.mcp.base_client import BaseClient

class McpTool:
    def __init__(self, mcp_client:BaseClient, tool_name:str):
        self.mcp_client = mcp_client
        self.tool_name = tool_name

    async def __call__(self, **kwargs):
        return await self.mcp_client.call_tool(self.tool_name, **kwargs)
