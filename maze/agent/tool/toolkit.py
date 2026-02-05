"""Toolkit class to manage and register tools."""
from typing import Dict, Callable, Any, Awaitable, Optional
import inspect
from Maze.maze.mcp.mcp_tool_wrapper import McpTool

class Toolkit:
    """Class to manage and register tools."""
    
    def __init__(self):
        self.tools: Dict[str, Callable[..., Awaitable[Any]]] = {}
        self.schemas: Dict[str, dict] = {}

    async def add_mcp_tool(self, mcp_client):
        tools = await mcp_client.list_tools()
        for tool in tools:
            self.tools[tool.name] = McpTool(mcp_client, tool_name=tool.name)
            schema = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": {
                        "type": "object",
                        "properties": tool.inputSchema.get(
                            "properties",
                            {},
                        ),
                        "required": tool.inputSchema.get(
                            "required",
                            [],
                        ),
                    },
                },
            }       
            self.schemas[tool.name] = schema


    async def add_tool(self, func: Callable[..., Awaitable[Any]], schema: Optional[dict] = None):
        """
        Add a tool function to the toolkit.
        
        Args:
            func: Tool function to add
            schema: Schema for the tool (if not provided, will be inferred)
        """
        name = func.__name__
        self.tools[name] = func
        
        if schema is None:
            # Auto-generate schema from function signature if not provided
            sig = inspect.signature(func)
            parameters = {"type": "object", "properties": {}, "required": []}
            
            for param_name, param in sig.parameters.items():
                # For simplicity, assume all parameters are strings unless we can infer otherwise
                parameters["properties"][param_name] = {"type": "string"}
                parameters["required"].append(param_name)
            
            schema = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": func.__doc__ or f"Tool function: {name}",
                    "parameters": parameters
                }
            }
        
        self.schemas[name] = schema
    
    async def get_tool(self, name: str) -> Optional[Callable[..., Awaitable[Any]]]:
        """Get a tool by name."""
        return self.tools.get(name)
    
    async def get_schema(self, name: str) -> Optional[dict]:
        """Get the schema for a tool by name."""
        return self.schemas.get(name)
    
    async def get_all_tools(self) -> Dict[str, Callable[..., Awaitable[Any]]]:
        """Get all registered tools."""
        return self.tools.copy()
    
    async def get_all_schemas(self) -> Dict[str, dict]:
        """Get all registered tool schemas."""
        return self.schemas.copy()