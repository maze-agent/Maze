"""A small HTTP client; the server owns execution."""

from typing import Any

import httpx


class MazeClient:
    def __init__(self, endpoint: str, token: str, timeout_s: float = 630) -> None:
        self.http = httpx.Client(
            base_url=endpoint,
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {token}"},
        )

    def request(self, method: str, path: str, body: Any = None) -> Any:
        response = self.http.request(method, path, json=body)
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "MazeClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
