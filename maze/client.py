"""A small HTTP client; the server owns execution."""

from typing import Any

import httpx

from maze.spec import DagSpec, DeploymentSpec, InvokeRequest


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

    def deploy(
        self, dag: DagSpec, deployment: DeploymentSpec | None = None
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/v1/deployments",
            {
                "dag": dag.document(),
                "deployment": (deployment or DeploymentSpec()).document(),
            },
        )

    def invoke(self, deployment_id: str, request: InvokeRequest) -> dict[str, Any]:
        return self.request(
            "POST", f"/v1/deployments/{deployment_id}/invoke", request.document()
        )

    def undeploy(self, deployment_id: str) -> dict[str, Any]:
        return self.request("DELETE", f"/v1/deployments/{deployment_id}")

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "MazeClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
