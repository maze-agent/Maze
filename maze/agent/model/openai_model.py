"""OpenAI model implementation."""
from typing import List, Dict, Any, Optional
from openai import AsyncOpenAI
from .base_model import BaseModel


class OpenAIModel(BaseModel):
    """OpenAI model implementation."""
    
    def __init__(self, api_key: str, model_name: str = "gpt-4o"):
        super().__init__(api_key, model_name)
        self.client = AsyncOpenAI(api_key=api_key)
    
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
        params = {
            "model": self.model_name,
            "messages": messages,
        }
        
        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"  # Let the model decide when to use tools
        
        # Add any additional parameters
        params.update(kwargs)
        
        response = await self.client.chat.completions.create(**params)
        
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