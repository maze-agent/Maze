"""Single-shot run analysis: summarize a run and ask an external LLM for
optimization suggestions.

Intentionally simple: build a compact digest from a run snapshot, make ONE
OpenAI-compatible chat completion call, and return the suggestion text. No
cross-run aggregation, no internal agent loop.
"""

from __future__ import annotations

from typing import Any, Dict, List

import requests

from maze.client.maze.react_llm import _load_openai_react_config


DEFAULT_SYSTEM_PROMPT = (
    "You are a workflow performance analyst for the Maze distributed task runner. "
    "Given a single run digest (task durations, pending reasons, fallbacks, token "
    "cost, errors), give concise, concrete optimization suggestions: resource "
    "tuning, parallelism, fallback usage, and failure fixes. Be specific and brief."
)


def build_run_digest(snapshot: Dict[str, Any], *, slow_top_n: int = 5) -> Dict[str, Any]:
    """Build a compact, LLM-friendly summary from a static run snapshot."""
    task_nodes = snapshot.get("task_nodes") or {}
    tasks: List[Dict[str, Any]] = []
    for task in task_nodes.values():
        tasks.append({
            "task": task.get("task_name") or task.get("task_id"),
            "status": task.get("status"),
            "duration_seconds": task.get("duration_seconds"),
            "resources": task.get("resources"),
            "variant": task.get("variant"),
            "degraded": task.get("degraded"),
            "pending_reason": task.get("pending_reason"),
            "error": _short(task.get("error")),
            "metrics": task.get("metrics") or {},
        })

    def _dur(item):
        return item.get("duration_seconds") or 0

    slowest = sorted(tasks, key=_dur, reverse=True)[:slow_top_n]
    degraded = [t["task"] for t in tasks if t.get("degraded")]
    failed = [
        {"task": t["task"], "error": t["error"]}
        for t in tasks
        if t.get("status") in ("failed", "timed_out")
    ]
    pending = [
        {"task": t["task"], "reason": t["pending_reason"]}
        for t in tasks
        if t.get("pending_reason")
    ]

    return {
        "run_id": snapshot.get("run_id"),
        "status": snapshot.get("status"),
        "duration_seconds": snapshot.get("duration_seconds"),
        "task_counts": snapshot.get("task_counts") or {},
        "error_summary": _short(snapshot.get("error_summary")),
        "metrics": snapshot.get("metrics") or {},
        "slowest_tasks": slowest,
        "degraded_tasks": degraded,
        "failed_tasks": failed,
        "pending_tasks": pending,
        "task_total": len(tasks),
    }


def analyze_run_with_llm(
    snapshot: Dict[str, Any],
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    config_path: str | None = None,
    system_prompt: str | None = None,
    temperature: float = 0,
    max_tokens: int = 512,
    timeout: int = 60,
) -> Dict[str, Any]:
    """Summarize the run and make a single LLM call for suggestions."""
    import json

    config = _load_openai_react_config(
        base_url=base_url,
        model=model,
        api_key=api_key,
        api_key_env=api_key_env,
        config_path=config_path,
    )
    digest = build_run_digest(snapshot)

    messages = [
        {"role": "system", "content": system_prompt or DEFAULT_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps({"run_digest": digest}, ensure_ascii=False)},
    ]
    response = requests.post(
        config["base_url"] + "/chat/completions",
        headers={
            "Authorization": "Bearer " + config["api_key"],
            "Content-Type": "application/json",
        },
        json={
            "model": config["model"],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    suggestion = response.json()["choices"][0]["message"].get("content", "").strip()

    return {
        "run_id": snapshot.get("run_id"),
        "model": config["model"],
        "digest": digest,
        "suggestion": suggestion,
    }


def _short(value: Any, max_length: int = 400) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return value if len(value) <= max_length else value[:max_length] + "..."
    text = str(value)
    return text if len(text) <= max_length else text[:max_length] + "..."
