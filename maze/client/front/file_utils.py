"""
File handling utility classes
"""

from typing import Optional
from pathlib import Path


class FileInput:
    """
    File input marker class to explicitly specify a parameter as a file path
    
    Example:
        task = workflow.add_task(
            process_image,
            inputs={
                "image": FileInput("C:/local/image.jpg"),
                "text": "some text"
            }
        )
    """
    
    def __init__(self, local_path: str):
        """
        Initialize file input
        
        Args:
            local_path: Local file path
        """
        self.local_path = str(local_path)
        self.path = Path(local_path)
        
        # Validate file exists
        if not self.path.exists():
            raise FileNotFoundError(f"File does not exist: {local_path}")
        
        if not self.path.is_file():
            raise ValueError(f"Path is not a file: {local_path}")
    
    @property
    def filename(self) -> str:
        """Get filename"""
        return self.path.name
    
    @property
    def extension(self) -> str:
        """Get file extension"""
        return self.path.suffix
    
    def read_bytes(self) -> bytes:
        """Read file content (bytes)"""
        return self.path.read_bytes()
    
    def __repr__(self) -> str:
        return f"FileInput('{self.local_path}')"


def is_file_type(data_type: str) -> bool:
    """
    Check if data type is a file type
    
    Args:
        data_type: Data type string
        
    Returns:
        bool: Whether it is a file type
        
    Example:
        >>> is_file_type("file")
        True
        >>> is_file_type("file:image")
        True
        >>> is_file_type("str")
        False
    """
    if data_type is None:
        return False
    return data_type.startswith("file")


def extract_file_subtype(data_type: str) -> Optional[str]:
    """
    Extract file subtype
    
    Args:
        data_type: Data type string
        
    Returns:
        Optional[str]: File subtype, or None if not present
        
    Example:
        >>> extract_file_subtype("file:image")
        'image'
        >>> extract_file_subtype("file")
        None
    """
    if not is_file_type(data_type):
        return None
    
    parts = data_type.split(":", 1)
    if len(parts) == 2:
        return parts[1]
    return None

