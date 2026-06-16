"""Pure helpers for run operations: resume spec building (C5), agent metric
aggregation (C7) and run error summarization (C11).

These are deliberately pure functions (no IO) so they are easy to unit test;
the HTTP endpoints in server.py wire them to persisted run data.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Tuple


SUCCEEDED = "succeeded"
FAILED_STATES = ("failed", "timed_out")


class ResumeError(ValueError):
    """Raised when a run cannot be resumed from its persisted state."""


# --------------------------------------------------------------------------
# C5: resume (re-run only unfinished tasks)
# --------------------------------------------------------------------------

def _parse_ref(value: str) -> Tuple[str, str]:
    parts = str(value).split(".")
    if len(parts) == 3 and parts[1] == "output":
        return parts[0], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1]
    raise ResumeError(f"cannot parse task output reference: {value}")


def _node_dependencies(node: Dict[str, Any], edges: List[Dict[str, str]]) -> Dict[str, Tuple[str, str]]:
    """Return {target_input: (source_task, source_output)} for one node."""
    deps: Dict[str, Tuple[str, str]] = {}
    for edge in edges:
        if edge.get("target_task_id") == node["id"]:
            deps[edge["target_input"]] = (edge["source_task_id"], edge["source_output"])
    for name, info in (node.get("inputs") or {}).items():
        if isinstance(info, dict) and info.get("input_schema") == "from_task" and info.get("value"):
            deps[name] = _parse_ref(info["value"])
    return deps


def build_resume_spec(dag_spec: Dict[str, Any], task_nodes: Dict[str, Any]) -> Dict[str, Any]:
    """Build a new DAG submit spec that re-runs only non-succeeded tasks.

    Dependencies from already-succeeded tasks are injected as literal inputs
    using the persisted task result. Raises ResumeError if a required upstream
    output is not available in the persisted run.
    """
    nodes = dag_spec.get("nodes") or []
    edges = dag_spec.get("edges") or []
    status_of = {tid: (node or {}).get("status") for tid, node in (task_nodes or {}).items()}
    result_of = {tid: ((node or {}).get("result_summary") or {}) for tid, node in (task_nodes or {}).items()}

    rerun_ids = [n["id"] for n in nodes if status_of.get(n["id"]) != SUCCEEDED]
    if not rerun_ids:
        raise ResumeError("nothing to resume: all tasks already succeeded")
    if len(rerun_ids) == len(nodes):
        # No succeeded prefix to skip; behaves like a full rerun.
        return _spec_subset(dag_spec, rerun_ids, edges, result_of, set(rerun_ids))

    rerun_set = set(rerun_ids)
    return _spec_subset(dag_spec, rerun_ids, edges, result_of, rerun_set)


def _spec_subset(
    dag_spec: Dict[str, Any],
    rerun_ids: List[str],
    edges: List[Dict[str, str]],
    result_of: Dict[str, Any],
    rerun_set: set,
) -> Dict[str, Any]:
    by_id = {n["id"]: n for n in dag_spec.get("nodes") or []}
    new_nodes: List[Dict[str, Any]] = []
    new_edges: List[Dict[str, str]] = []

    for tid in rerun_ids:
        node = copy.deepcopy(by_id[tid])
        deps = _node_dependencies(node, edges)
        inputs_payload: Dict[str, Any] = {}

        # Preserve literal (from_user) inputs.
        for name, info in (node.get("inputs") or {}).items():
            if isinstance(info, dict) and info.get("input_schema") == "from_user" and info.get("has_value"):
                inputs_payload[name] = {"value": info.get("value")}

        # Resolve dependencies.
        for target_input, (src, out) in deps.items():
            if src in rerun_set:
                inputs_payload[target_input] = {"from": f"{src}.{out}"}
                new_edges.append({"from": f"{src}.{out}", "to": f"{tid}.{target_input}"})
            else:
                src_result = result_of.get(src) or {}
                if out not in src_result:
                    raise ResumeError(
                        f"cannot resume: missing persisted output {src}.{out} required by {tid}; "
                        "use /rerun instead or enable artifact mode"
                    )
                inputs_payload[target_input] = {"value": src_result[out]}

        new_nodes.append({
            "id": node["id"],
            "task_name": node.get("task_name"),
            "code_str": node.get("code_str"),
            "code_ser": node.get("code_ser"),
            "inputs": inputs_payload,
            "outputs": [o["name"] for o in node.get("outputs") or []],
            "resources": node.get("resources"),
            "fallback": node.get("fallback"),
            "fallback_policy": node.get("fallback_policy"),
            "timeout_seconds": node.get("timeout_seconds"),
            "max_retries": node.get("max_retries"),
            "retry_backoff_seconds": node.get("retry_backoff_seconds"),
            "retry_on": node.get("retry_on"),
        })

    return {
        "schema": dag_spec.get("schema") or "maze.workflow/v1",
        "name": (dag_spec.get("name") or "dag-workflow") + "-resume",
        "description": dag_spec.get("description"),
        "nodes": new_nodes,
        "edges": new_edges,
        "run": dag_spec.get("run") or {},
    }


# --------------------------------------------------------------------------
# C7: aggregate agent / task performance across runs
# --------------------------------------------------------------------------

def aggregate_agent_metrics(run_snapshots: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate per-task-name performance across many run snapshots."""
    agents: Dict[str, Dict[str, Any]] = {}

    for snap in run_snapshots:
        for task in (snap.get("task_nodes") or {}).values():
            name = task.get("task_name") or task.get("task_id")
            if not name:
                continue
            bucket = agents.setdefault(name, {
                "task_name": name,
                "runs": 0,
                "succeeded": 0,
                "failed": 0,
                "degraded": 0,
                "_durations": [],
                "tokens_in": 0,
                "tokens_out": 0,
                "cost_usd": 0.0,
            })
            bucket["runs"] += 1
            status = task.get("status")
            if status == SUCCEEDED:
                bucket["succeeded"] += 1
            elif status in FAILED_STATES:
                bucket["failed"] += 1
            if task.get("degraded"):
                bucket["degraded"] += 1
            dur = task.get("duration_seconds")
            if isinstance(dur, (int, float)):
                bucket["_durations"].append(float(dur))
            metrics = task.get("metrics") or {}
            if isinstance(metrics.get("tokens_in"), (int, float)):
                bucket["tokens_in"] += int(metrics["tokens_in"])
            if isinstance(metrics.get("tokens_out"), (int, float)):
                bucket["tokens_out"] += int(metrics["tokens_out"])
            if isinstance(metrics.get("cost_usd"), (int, float)):
                bucket["cost_usd"] += float(metrics["cost_usd"])

    result = []
    for bucket in agents.values():
        durations = sorted(bucket.pop("_durations"))
        bucket["avg_duration_seconds"] = round(sum(durations) / len(durations), 6) if durations else None
        bucket["p50_duration_seconds"] = _percentile(durations, 0.5)
        bucket["p95_duration_seconds"] = _percentile(durations, 0.95)
        bucket["max_duration_seconds"] = durations[-1] if durations else None
        bucket["success_rate"] = round(bucket["succeeded"] / bucket["runs"], 6) if bucket["runs"] else None
        bucket["cost_usd"] = round(bucket["cost_usd"], 6)
        result.append(bucket)

    result.sort(key=lambda b: b["task_name"])
    return {"agents": result, "agent_count": len(result)}


def _percentile(sorted_values: List[float], q: float):
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return round(sorted_values[0], 6)
    idx = min(len(sorted_values) - 1, int(round(q * (len(sorted_values) - 1))))
    return round(sorted_values[idx], 6)


# --------------------------------------------------------------------------
# C11: structured error summary for a run
# --------------------------------------------------------------------------

def summarize_run_errors(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Collect structured errors of failed tasks for fast localization."""
    failures = []
    for task in (snapshot.get("task_nodes") or {}).values():
        if task.get("status") not in FAILED_STATES:
            continue
        error = task.get("error") or task.get("last_error")
        failures.append({
            "task_id": task.get("task_id"),
            "task_name": task.get("task_name"),
            "status": task.get("status"),
            "attempt": task.get("attempt"),
            "node_id": (task.get("selected_node") or {}).get("node_id"),
            "stage": _error_field(error, "stage") or _infer_stage(error),
            "error_type": _error_field(error, "error_type"),
            "message": _error_field(error, "message") or (error if isinstance(error, str) else None),
            "origin": _error_field(error, "origin"),
            "traceback": _error_field(error, "traceback"),
        })
    return {
        "run_id": snapshot.get("run_id"),
        "status": snapshot.get("status"),
        "failure_count": len(failures),
        "failures": failures,
        "error_summary": snapshot.get("error_summary"),
    }


def _error_field(error: Any, field: str):
    if isinstance(error, dict):
        return error.get(field)
    return None


def _infer_stage(error: Any) -> str:
    """Best-effort stage classification when not explicitly provided."""
    origin = _error_field(error, "origin")
    error_type = _error_field(error, "error_type")
    if error_type in ("resource_unavailable", "node_lost"):
        return "scheduling"
    if error_type == "artifact_error":
        return "artifact"
    if origin == "runner" or error_type == "user_code":
        return "execution"
    if origin == "scheduler":
        return "scheduling"
    return "unknown"
