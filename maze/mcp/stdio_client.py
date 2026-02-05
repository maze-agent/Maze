
from typing import Literal,Dict,List
from mcp import stdio_client, StdioServerParameters
from maze.mcp.base_client import BaseClient

class StdIOClient(BaseClient):
    def __init__(
        self,
        name: str,
        command: str,
        args: List[str] | None = None,
        env: Dict[str, str] | None = None,
        cwd: str | None = None,
        encoding: str = "utf-8",
        encoding_error_handler: Literal[
            "strict",
            "ignore",
            "replace",
        ] = "strict",
    ) -> None:
        super().__init__(name=name)

        self.client = stdio_client(
            StdioServerParameters(
                command=command,
                args=args or [],
                env=env,
                cwd=cwd,
                encoding=encoding,
                encoding_error_handler=encoding_error_handler,
            ),
        )