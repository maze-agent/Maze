"""Legacy MCP client package.

This package is not part of the Maze Core Runtime public boundary. It remains
temporarily for compatibility and should move to an extension or be removed in
a later purification phase.
"""

from maze.mcp.http_client import HttpClient
from maze.mcp.stdio_client import StdIOClient
from maze.mcp.base_client import BaseClient

__all__ = ["HttpClient", "StdIOClient", "BaseClient"]
