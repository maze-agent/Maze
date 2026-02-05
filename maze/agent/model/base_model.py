"""Base model class definition."""
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional


class BaseModel(ABC):
    """Base class for all language models."""
    
    def __init__(self, api_key: str, model_name: str):
        self.api_key = api_key
        self.model_name = model_name
    
    @abstractmethod
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
        pass