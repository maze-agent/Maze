"""
OpenAI-compatible HTTP inference routing.

Workflows should depend on :class:`RemoteLlmRoute` instead of threading raw
``api_url`` / ``api_key`` strings through every helper. Values are still stored
on the DAG context under the legacy keys ``api_url`` and ``api_key`` for
compatibility, but those are resolved from environment variables when the
resource layer does not override them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional


def _normalize_auth_header(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.lower().startswith("bearer "):
        return s
    return f"Bearer {s}"


def default_cloud_completions_url() -> str:
    """Absolute ``.../chat/completions`` URL for hosted OpenAI-compatible APIs."""
    u = os.environ.get("MAZE_CLOUD_COMPLETIONS_URL", "").strip()
    if u:
        return u
    u = os.environ.get("SILICONFLOW_CHAT_COMPLETIONS_URL", "").strip()
    if u:
        return u
    base = os.environ.get("OPENAI_BASE_URL", "").strip().rstrip("/")
    if base:
        return f"{base}/v1/chat/completions"
    return ""


def default_cloud_authorization() -> Optional[str]:
    """Bearer token (with or without ``Bearer `` prefix) from the environment."""
    return (
        os.environ.get("MAZE_CLOUD_API_KEY")
        or os.environ.get("SILICONFLOW_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or None
    )


@dataclass(frozen=True)
class RemoteLlmRoute:
    """Resolved POST target for ``/v1/chat/completions``-style calls."""

    completions_url: str
    authorization: Optional[str] = None

    def headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        auth = _normalize_auth_header(self.authorization)
        if auth:
            h["Authorization"] = auth
        return h

    @staticmethod
    def for_vllm_worker_base(base_url: str) -> "RemoteLlmRoute":
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("vLLM worker base URL is empty")
        return RemoteLlmRoute(completions_url=f"{base}/v1/chat/completions", authorization=None)

    @staticmethod
    def for_openai_compatible_chat(full_completions_url: str, token_or_bearer: Optional[str]) -> "RemoteLlmRoute":
        url = (full_completions_url or "").strip()
        if not url:
            raise ValueError("Remote chat completions URL is empty")
        return RemoteLlmRoute(completions_url=url, authorization=token_or_bearer)

    @staticmethod
    def from_dag_context(context: Any) -> "RemoteLlmRoute":
        """Load hosted-route credentials published by :class:`DAGContextManager`."""
        import ray

        url = ray.get(context.get.remote("api_url"))
        secret = ray.get(context.get.remote("api_key"))
        return RemoteLlmRoute.for_openai_compatible_chat(url, secret)

    def redacted_log_hint(self) -> str:
        host = self.completions_url.split("/")[2] if "://" in self.completions_url else self.completions_url
        return f"RemoteLlmRoute(host={host}, auth={'set' if self.authorization else 'none'})"

