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


DEFAULT_REVIEW_SYSTEM_PROMPT = (
    "You are a workflow DAG design reviewer for the Maze distributed task runner. "
    "Given a DAG design digest (nodes, edges, dependencies, resources, fallbacks, "
    "structure metrics), judge whether the design is good and give concrete, "
    "actionable suggestions: parallelism opportunities, over/under-provisioned "
    "resources, missing fallbacks for heavy nodes, long dependency chains, "
    "isolated or redundant nodes, and reliability. Be specific and concise."
)


def build_dag_design_digest(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Build a structure digest of a normalized maze.workflow/v1 DAG spec."""
    nodes = spec.get("nodes") or []
    edges = spec.get("edges") or []
    node_ids = [n["id"] for n in nodes]

    in_deg = {nid: 0 for nid in node_ids}
    out_deg = {nid: 0 for nid in node_ids}
    children: Dict[str, List[str]] = {nid: [] for nid in node_ids}
    for edge in edges:
        src = edge.get("source_task_id")
        tgt = edge.get("target_task_id")
        if src in out_deg:
            out_deg[src] += 1
        if tgt in in_deg:
            in_deg[tgt] += 1
        if src in children and tgt is not None:
            children[src].append(tgt)

    node_digest = []
    gpu_nodes = 0
    fallback_nodes = 0
    for n in nodes:
        resources = n.get("resources") or {}
        has_fallback = bool(n.get("fallback"))
        if (resources.get("gpu") or 0) > 0:
            gpu_nodes += 1
        if has_fallback:
            fallback_nodes += 1
        node_digest.append({
            "id": n["id"],
            "task_name": n.get("task_name"),
            "inputs": list((n.get("inputs") or {}).keys()),
            "outputs": [o.get("name") for o in (n.get("outputs") or [])],
            "resources": resources,
            "has_fallback": has_fallback,
            "in_degree": in_deg.get(n["id"], 0),
            "out_degree": out_deg.get(n["id"], 0),
        })

    return {
        "name": spec.get("name"),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "roots": [nid for nid in node_ids if in_deg.get(nid, 0) == 0],
        "leaves": [nid for nid in node_ids if out_deg.get(nid, 0) == 0],
        "isolated": [nid for nid in node_ids if in_deg.get(nid, 0) == 0 and out_deg.get(nid, 0) == 0],
        "max_fan_out": max(out_deg.values()) if out_deg else 0,
        "max_fan_in": max(in_deg.values()) if in_deg else 0,
        "max_depth": _longest_chain(node_ids, children),
        "gpu_nodes": gpu_nodes,
        "fallback_nodes": fallback_nodes,
        "nodes": node_digest,
        "edges": [
            {"from": f"{e.get('source_task_id')}.{e.get('source_output')}",
             "to": f"{e.get('target_task_id')}.{e.get('target_input')}"}
            for e in edges
        ],
    }


def _longest_chain(node_ids: List[str], children: Dict[str, List[str]]) -> int:
    """Longest path length (in nodes) of a DAG via memoized DFS."""
    memo: Dict[str, int] = {}

    def depth(nid: str) -> int:
        if nid in memo:
            return memo[nid]
        best = 1
        for child in children.get(nid, []):
            best = max(best, 1 + depth(child))
        memo[nid] = best
        return best

    return max((depth(nid) for nid in node_ids), default=0)


def review_dag_with_llm(
    spec: Dict[str, Any],
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
    """Summarize a DAG design and make a single LLM call for a design review."""
    import json

    config = _load_openai_react_config(
        base_url=base_url,
        model=model,
        api_key=api_key,
        api_key_env=api_key_env,
        config_path=config_path,
    )
    digest = build_dag_design_digest(spec)

    messages = [
        {"role": "system", "content": system_prompt or DEFAULT_REVIEW_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps({"dag_design": digest}, ensure_ascii=False)},
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
    review = response.json()["choices"][0]["message"].get("content", "").strip()

    return {
        "name": spec.get("name"),
        "model": config["model"],
        "digest": digest,
        "review": review,
    }


def _short(value: Any, max_length: int = 400) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return value if len(value) <= max_length else value[:max_length] + "..."
    text = str(value)
    return text if len(text) <= max_length else text[:max_length] + "..."
