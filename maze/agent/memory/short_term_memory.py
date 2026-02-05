"""Short-term memory implementation for the agent."""
from typing import List, Dict, Any, Optional
from datetime import datetime


class ShortTermMemory:
    """Short-term memory for storing conversation history and context."""
    
    def __init__(self, max_size: int = 100):
        self.max_size = max_size
        self.messages: List[Dict[str, Any]] = []
        self.created_at = datetime.now()
    
    def add_message(self, role: str, content: str, metadata: Optional[Dict[str, Any]] = None):
        """
        Add a message to the memory.
        
        Args:
            role: Role of the message sender (e.g., 'user', 'assistant', 'system')
            content: Content of the message
            metadata: Optional metadata associated with the message
        """
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            "metadata": metadata or {}
        }
        
        self.messages.append(message)
        
        # Trim memory if it exceeds the maximum size
        if len(self.messages) > self.max_size:
            self.messages.pop(0)
    
    def get_messages(self) -> List[Dict[str, Any]]:
        """
        Get all messages in the memory.
        
        Returns:
            List of messages
        """
        return self.messages.copy()
    
    def get_recent_messages(self, count: int) -> List[Dict[str, Any]]:
        """
        Get the most recent messages.
        
        Args:
            count: Number of recent messages to return
            
        Returns:
            List of recent messages
        """
        return self.messages[-count:] if count <= len(self.messages) else self.messages.copy()
    
    def clear(self):
        """Clear all messages from memory."""
        self.messages.clear()
    
    def get_size(self) -> int:
        """Get the current size of the memory."""
        return len(self.messages)
    
    def search(self, query: str) -> List[Dict[str, Any]]:
        """
        Search for messages containing the query string.
        
        Args:
            query: Query string to search for
            
        Returns:
            List of matching messages
        """
        results = []
        query_lower = query.lower()
        
        for message in self.messages:
            if query_lower in message["content"].lower():
                results.append(message)
        
        return results