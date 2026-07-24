"""Public Task-side inference calls without exposing endpoints or adapters."""

from __future__ import annotations

from ascend_maze.inference.context import current_route_context, invoke_current
from ascend_maze.inference.contracts import (
    ChatRequest,
    ChatResponse,
    ModelRouteContext,
)


def chat(
    messages: tuple[dict[str, object], ...] | list[dict[str, object]],
    *,
    max_tokens: int = 128,
    temperature: float = 0.0,
) -> ChatResponse:
    return invoke_current(
        ChatRequest.create(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    )


def get_route_context() -> ModelRouteContext:
    return current_route_context()


__all__ = ["chat", "get_route_context"]
