import inspect
"""Base tool class definition."""
from typing import Any, Dict


class BaseTool:
    """Base class for all tools."""
    
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
    
    async def run(self, **kwargs) -> Any:
        """Run the tool with given arguments."""
        raise NotImplementedError("Subclasses must implement run method")
    
    def get_schema(self) -> Dict[str, Any]:
        """Get the tool's schema for model consumption."""
        raise NotImplementedError("Subclasses must implement get_schema method")

    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Get the parameters schema for the tool based on its signature"""
        sig = inspect.signature(self._run)
        parameters = {
            "type": "object",
            "properties": {},
            "required": []
        }
        
        for param_name, param in sig.parameters.items():
            # Skip 'self' parameter
            if param_name == 'self':
                continue
                
            param_type = "string"  # default type
            
            # Determine type from annotation if available
            if param.annotation != inspect.Parameter.empty:
                if param.annotation == int:
                    param_type = "integer"
                elif param.annotation == float:
                    param_type = "number"
                elif param.annotation == bool:
                    param_type = "boolean"
                elif param.annotation == list:
                    param_type = "array"
                elif param.annotation == dict:
                    param_type = "object"
                    
            parameters["properties"][param_name] = {
                "type": param_type
            }
            
            # Mark as required if no default value
            if param.default == inspect.Parameter.empty and param_name != 'kwargs':
                parameters["required"].append(param_name)
                
        return parameters

    def to_dict(self) -> Dict[str, Any]:
        """Convert the tool to dictionary format compatible with LLM tools specification"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._get_parameters_schema()
            }
        }
        
    def _run(self, **kwargs) -> Any:
        """Internal method to run the tool - should be implemented by subclasses"""
        raise NotImplementedError("Subclasses must implement _run method")