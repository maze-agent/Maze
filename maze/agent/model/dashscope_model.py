"""DashScope model implementation."""
from typing import List, Dict, Any, Optional
import dashscope
from .base_model import BaseModel


class DashScopeModel(BaseModel):
    """DashScope model implementation."""
    
    def __init__(self, api_key: str, model_name: str = "qwen-max"):
        super().__init__(api_key, model_name)
        self.api_key = api_key
    
    async def chat_completion(
        self, 
        messages: List[Dict[str, str]], 
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Perform chat completion with optional tools.
        
        Args:
            messages: List of messages in the conversation
            tools: Optional list of tools that can be used
            **kwargs: Additional parameters
            
        Returns:
            Model response
        """
        # Prepare messages for DashScope
        formatted_messages = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            formatted_messages.append({"role": role, "content": content})
        
        params = {
            "api_key": self.api_key,
            "model": self.model_name,
            "messages": formatted_messages,
        }
        
        if tools:
            # Convert tools to DashScope format
            dashscope_tools = []
            for tool in tools:
                if "function" in tool:
                    function = tool["function"]
                    dashscope_tool = {
                        "type": "function",
                        "function": {
                            "name": function["name"],
                            "description": function.get("description", ""),
                            "parameters": function["parameters"]
                        }
                    }
                    dashscope_tools.append(dashscope_tool)
            
            params["tools"] = dashscope_tools
            params["tool_choice"] = "auto"  # Let the model decide when to use tools
        
        # Add any additional parameters
        params.update(kwargs)
        # Call DashScope asynchronously
        response = await dashscope.aigc.generation.AioGeneration.call(**params)

        # Process the response
        result = {
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": response.output.choices[0].message.role,
                        "content": response.output.choices[0].message.content,
                        "tool_calls": response.output.choices[0].message.get("tool_calls", [])
                    },
                    "finish_reason": response.output.choices[0].finish_reason
                }
            ]
        }
        
        return result