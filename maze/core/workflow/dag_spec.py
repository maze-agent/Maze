from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Tuple

import networkx as nx

from maze.core.workflow.task import CodeTask
from maze.core.workflow.resources import (
    ResourceSpecError,
    normalize_resources,
    normalize_task_semantics,
)
from maze.core.workflow.workflow import Workflow


class DagSpecError(ValueError):
    """Raised when an external DAG submit spec is invalid."""


NODE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,127}$")


def dag_spec_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = _ensure_mapping(payload, "DAG spec")
    nodes = payload.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise DagSpecError("nodes must be a non-empty list")

    raw_edges = payload.get("edges") or []
    if not isinstance(raw_edges, list):
        raise DagSpecError("edges must be a list")

    normalized_nodes = [_normalize_node(node) for node in nodes]
    node_ids = [node["id"] for node in normalized_nodes]
    duplicates = sorted({node_id for node_id in node_ids if node_ids.count(node_id) > 1})
    if duplicates:
        raise DagSpecError(f"duplicate node ids: {duplicates}")

    node_map = {node["id"]: node for node in normalized_nodes}
    normalized_edges = [_normalize_edge(edge, node_map) for edge in raw_edges]
    _validate_graph(node_ids, normalized_edges)

    run = _normalize_run(payload.get("run") or {})
    input_contract = _normalize_input_contract(payload.get("input_contract"))
    metadata = dict(payload.get("metadata") or {})
    metadata.setdefault("workflow_name", payload.get("name") or "dag-workflow")
    metadata.setdefault("run_kind", "dag")
    stored_spec = {
        "schema": payload.get("schema") or "maze.workflow/v1",
        "workflow_id": _optional_workflow_id(payload.get("workflow_id")),
        "name": payload.get("name") or "dag-workflow",
        "description": payload.get("description"),
        "nodes": normalized_nodes,
        "edges": normalized_edges,
        "run": _stored_run_config(run),
    }
    if input_contract is not None:
        stored_spec["input_contract"] = input_contract
    if "final_output_refs" in payload:
        stored_spec["final_output_refs"] = copy.deepcopy(payload["final_output_refs"])
    metadata["dag_spec"] = stored_spec

    spec = {
        "schema": payload.get("schema") or "maze.workflow/v1",
        "workflow_id": _optional_workflow_id(payload.get("workflow_id")),
        "name": str(payload.get("name") or "dag-workflow"),
        "description": payload.get("description"),
        "nodes": normalized_nodes,
        "edges": normalized_edges,
        "run": run,
        "tags": [str(item) for item in payload.get("tags") or []],
        "metadata": metadata,
    }
    if input_contract is not None:
        spec["input_contract"] = input_contract
    if "final_output_refs" in payload:
        spec["final_output_refs"] = copy.deepcopy(payload["final_output_refs"])
    return spec


def build_dag_workflow(workflow_id: str, spec: Dict[str, Any]) -> Workflow:
    spec = dag_spec_from_payload(spec)
    workflow = Workflow(workflow_id)
    task_inputs = _build_task_inputs(spec["nodes"], spec["edges"])

    for node in spec["nodes"]:
        task = CodeTask(workflow_id, node["id"], node["task_name"])
        task.save_task(
            task_input=task_inputs[node["id"]],
            task_output=_build_task_output(node),
            code_str=node.get("code_str"),
            code_ser=node.get("code_ser"),
            resources=node["resources"],
            task_kind=node.get("task_kind"),
            file_context=node.get("file_context"),
            model_anchor=node.get("model_anchor"),
            max_retries=node.get("max_retries"),
            retry_backoff_seconds=node.get("retry_backoff_seconds", 0),
            retry_on=node.get("retry_on"),
            timeout_seconds=node.get("timeout_seconds"),
        )
        workflow.add_task(node["id"], task)

    for edge in spec["edges"]:
        workflow.add_edge(edge["source_task_id"], edge["target_task_id"])

    input_contract = spec.get("input_contract")
    if input_contract is not None:
        workflow.graph.graph["workflow_input_contract"] = copy.deepcopy(input_contract)
    workflow.graph.graph["dag_definition"] = dag_definition_from_spec(spec)

    return workflow


def dag_definition_from_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Return the immutable graph portion used to validate a stable workflow id."""
    definition = {
        "schema": spec["schema"],
        "nodes": copy.deepcopy(spec["nodes"]),
        "edges": copy.deepcopy(spec["edges"]),
    }
    if spec.get("input_contract") is not None:
        definition["input_contract"] = copy.deepcopy(spec["input_contract"])
    return definition


def dag_file_context(
    spec: Dict[str, Any],
    *,
    artifact_base_url: str | None = None,
    artifact_mode: bool = True,
) -> Dict[str, Any] | None:
    run = spec.get("run") or {}
    explicit = run.get("file_context")
    if explicit is not None:
        return dict(explicit)

    workspace = run.get("workspace_dir") or run.get("workspace")
    if not workspace:
        return None

    context: Dict[str, Any] = {
        "enabled": True,
        "workspace_dir": str(workspace),
    }
    if artifact_mode and artifact_base_url:
        context["artifact_store"] = {
            "type": "head_http",
            "base_url": artifact_base_url.rstrip("/"),
        }
    return context


def _normalize_node(raw: Any) -> Dict[str, Any]:
    node = dict(_ensure_mapping(raw, "node"))
    node_id = str(node.get("id") or "").strip()
    if not NODE_ID_RE.fullmatch(node_id):
        raise DagSpecError(
            f"invalid node id {node_id!r}; use letters, numbers, underscores or hyphens"
        )

    node_type = str(node.get("type") or "code")
    if node_type not in {"code", "python_task", "task"}:
        raise DagSpecError(f"unsupported node type for {node_id}: {node_type}")

    code_str = node.get("code_str", node.get("code"))
    code_ser = node.get("code_ser")
    if not code_str and not code_ser:
        raise DagSpecError(f"node {node_id} requires code_str/code or code_ser")

    outputs = _normalize_outputs(node.get("outputs"), node_id)
    model_anchor = dict(node.get("model_anchor") or {}) or None
    task_kind, resources = _normalize_task_semantics(
        task_kind=node.get("task_kind"),
        resources=node.get("resources"),
        model_anchor=model_anchor,
        node_id=node_id,
    )
    return {
        "id": node_id,
        "type": "code",
        "task_name": str(node.get("task_name") or node.get("name") or node_id),
        "inputs": _normalize_inputs(node.get("inputs") or {}, node_id),
        "outputs": outputs,
        "task_kind": task_kind,
        "resources": resources,
        "code_str": code_str,
        "code_ser": code_ser,
        "file_context": node.get("file_context"),
        "model_anchor": model_anchor,
        "max_retries": _optional_int(node.get("max_retries")),
        "retry_backoff_seconds": max(0.0, float(node.get("retry_backoff_seconds") or 0)),
        "retry_on": _optional_str_list(node.get("retry_on")),
        "timeout_seconds": _optional_float(node.get("timeout_seconds")),
        "metadata": dict(node.get("metadata") or {}),
    }


def _normalize_inputs(inputs: Any, node_id: str) -> Dict[str, Dict[str, Any]]:
    if not isinstance(inputs, dict):
        raise DagSpecError(f"node {node_id} inputs must be an object")

    normalized = {}
    for key, value in inputs.items():
        input_name = str(key)
        data_type = "any"
        if isinstance(value, dict) and ("from" in value or "value" in value or "data_type" in value):
            data_type = str(value.get("data_type") or "any")
            if value.get("input_schema") == "from_run":
                run_value = copy.deepcopy(value.get("value"))
                if _validate_run_input_refs(run_value) == 0:
                    raise DagSpecError(
                        f"node {node_id} input {input_name} has a malformed run input reference"
                    )
                normalized[input_name] = {
                    "key": input_name,
                    "input_schema": "from_run",
                    "value": run_value,
                    "data_type": data_type,
                    "has_value": False,
                }
                continue
            if value.get("input_schema") == "from_task":
                normalized[input_name] = {
                    "key": input_name,
                    "input_schema": "from_task",
                    "value": str(value.get("value") or value.get("from") or ""),
                    "data_type": data_type,
                    "has_value": bool(value.get("has_value", True)),
                }
                continue
            if value.get("input_schema") == "from_user":
                normalized[input_name] = {
                    "key": input_name,
                    "input_schema": "from_user",
                    "value": value.get("value"),
                    "data_type": data_type,
                    "has_value": bool(value.get("has_value", "value" in value)),
                }
                continue
            if "from" in value:
                normalized[input_name] = {
                    "key": input_name,
                    "input_schema": "from_task",
                    "value": str(value["from"]),
                    "data_type": data_type,
                    "has_value": True,
                }
            else:
                normalized[input_name] = {
                    "key": input_name,
                    "input_schema": "from_user",
                    "value": value.get("value"),
                    "data_type": data_type,
                    "has_value": "value" in value,
                }
            continue

        normalized[input_name] = {
            "key": input_name,
            "input_schema": "from_user",
            "value": value,
            "data_type": data_type,
            "has_value": True,
        }
    return normalized


def _normalize_outputs(outputs: Any, node_id: str) -> List[Dict[str, str]]:
    if not isinstance(outputs, list) or not outputs:
        raise DagSpecError(f"node {node_id} outputs must be a non-empty list")

    normalized = []
    seen = set()
    for output in outputs:
        if isinstance(output, str):
            name = output
            data_type = "any"
        elif isinstance(output, dict):
            name = str(output.get("name") or output.get("key") or "")
            data_type = str(output.get("data_type") or "any")
        else:
            raise DagSpecError(f"node {node_id} outputs entries must be strings or objects")
        if not name:
            raise DagSpecError(f"node {node_id} has an output without name")
        if name in seen:
            raise DagSpecError(f"node {node_id} has duplicate output: {name}")
        seen.add(name)
        normalized.append({"name": name, "data_type": data_type})
    return normalized


def _normalize_edge(raw: Any, node_map: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    edge = dict(_ensure_mapping(raw, "edge"))
    if "from" in edge and "to" in edge:
        source_task_id, source_output = _split_endpoint(str(edge["from"]), "from")
        target_task_id, target_input = _split_endpoint(str(edge["to"]), "to")
    else:
        source_task_id = str(edge.get("source") or edge.get("source_task_id") or "")
        target_task_id = str(edge.get("target") or edge.get("target_task_id") or "")
        source_output = str(edge.get("source_output") or edge.get("output") or "")
        target_input = str(edge.get("target_input") or edge.get("input") or "")

    _ensure_node(source_task_id, node_map, "edge source")
    _ensure_node(target_task_id, node_map, "edge target")
    if not source_output:
        raise DagSpecError(f"edge from {source_task_id} is missing source output")
    if not target_input:
        raise DagSpecError(f"edge to {target_task_id} is missing target input")
    if source_output not in {item["name"] for item in node_map[source_task_id]["outputs"]}:
        raise DagSpecError(f"edge references unknown output: {source_task_id}.{source_output}")

    return {
        "source_task_id": source_task_id,
        "source_output": source_output,
        "target_task_id": target_task_id,
        "target_input": target_input,
    }


def _build_task_inputs(nodes: List[Dict[str, Any]], edges: List[Dict[str, str]]) -> Dict[str, Dict[str, Any]]:
    task_inputs = {
        node["id"]: {"input_params": {}}
        for node in nodes
    }
    input_maps = {node["id"]: copy.deepcopy(node["inputs"]) for node in nodes}

    for edge in edges:
        target_inputs = input_maps[edge["target_task_id"]]
        existing = target_inputs.get(edge["target_input"])
        edge_value = f"{edge['source_task_id']}.output.{edge['source_output']}"
        if existing and existing.get("input_schema") == "from_user" and existing.get("has_value"):
            raise DagSpecError(
                f"input {edge['target_task_id']}.{edge['target_input']} has both a literal value and an edge"
            )
        if existing and existing.get("input_schema") == "from_task":
            existing_value = str(existing.get("value") or "")
            if existing_value in {
                edge_value,
                f"{edge['source_task_id']}.{edge['source_output']}",
            }:
                existing["value"] = edge_value
                existing["has_value"] = True
                continue
            raise DagSpecError(
                f"input {edge['target_task_id']}.{edge['target_input']} has conflicting edge references"
            )
        target_inputs[edge["target_input"]] = {
            "key": edge["target_input"],
            "input_schema": "from_task",
            "value": edge_value,
            "data_type": (existing or {}).get("data_type", "any"),
            "has_value": True,
        }

    for node_id, inputs in input_maps.items():
        for idx, (_, input_param) in enumerate(inputs.items(), start=1):
            task_inputs[node_id]["input_params"][str(idx)] = input_param
    return task_inputs


def _build_task_output(node: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "output_params": {
            str(idx): {"key": output["name"], "data_type": output["data_type"]}
            for idx, output in enumerate(node["outputs"], start=1)
        }
    }


def _normalize_run(run: Any) -> Dict[str, Any]:
    run = dict(_ensure_mapping(run, "run")) if run else {}
    normalized = {
        "workspace_dir": run.get("workspace_dir") or run.get("workspace"),
        "artifact_mode": bool(run.get("artifact_mode", True)),
        "file_context": run.get("file_context"),
        "timeout_seconds": _optional_float(run.get("timeout_seconds")),
        "tags": [str(item) for item in run.get("tags") or []],
        "metadata": dict(run.get("metadata") or {}),
    }
    if "inputs" in run:
        if run["inputs"] is not None and not isinstance(run["inputs"], dict):
            raise DagSpecError("run inputs must be an object")
        normalized["inputs"] = copy.deepcopy(run["inputs"])
    if "idempotency_key" in run or "idempotency_fingerprint" in run:
        normalized["idempotency_key"] = run.get("idempotency_key")
        normalized["idempotency_fingerprint"] = run.get("idempotency_fingerprint")
    return normalized


def _stored_run_config(run: Dict[str, Any]) -> Dict[str, Any]:
    stored = copy.deepcopy(run)
    stored.pop("inputs", None)
    stored.pop("idempotency_key", None)
    stored.pop("idempotency_fingerprint", None)
    return stored


def _normalize_input_contract(value: Any) -> Dict[str, Any] | None:
    if value is None:
        return None
    contract = _ensure_mapping(value, "input_contract")
    constants = contract.get("constants")
    runtime = contract.get("runtime")
    if not isinstance(constants, list) or not all(isinstance(key, str) for key in constants):
        raise DagSpecError("workflow input constants must be a list of names")
    if len(constants) != len(set(constants)) or not isinstance(runtime, dict):
        raise DagSpecError("malformed workflow input contract")
    if not all(isinstance(key, str) for key in runtime):
        raise DagSpecError("workflow runtime input names must be strings")
    if set(constants) & set(runtime):
        raise DagSpecError("workflow input cannot be both constant and runtime-bound")
    normalized_runtime = {}
    for key, raw_spec in runtime.items():
        if not isinstance(raw_spec, dict) or not isinstance(raw_spec.get("required"), bool):
            raise DagSpecError(f"malformed workflow runtime input: {key}")
        expected = {"required"} if raw_spec["required"] else {"required", "default"}
        if set(raw_spec) != expected:
            raise DagSpecError(f"malformed workflow runtime input: {key}")
        normalized_runtime[key] = copy.deepcopy(raw_spec)
    return {
        "constants": sorted(constants),
        "runtime": normalized_runtime,
    }


def _validate_run_input_refs(value: Any) -> int:
    if isinstance(value, dict):
        if "__maze_run_input__" in value:
            valid = (
                set(value) == {"__maze_run_input__", "key"}
                and value["__maze_run_input__"] is True
                and isinstance(value.get("key"), str)
                and bool(value["key"])
            )
            if not valid:
                raise DagSpecError("malformed workflow run input reference")
            return 1
        return sum(_validate_run_input_refs(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_validate_run_input_refs(item) for item in value)
    return 0


def _optional_workflow_id(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise DagSpecError("workflow_id must be a non-empty string without surrounding whitespace")
    return value


def _normalize_resources(resources: Dict[str, Any] | None) -> Dict[str, Any]:
    try:
        return normalize_resources(resources)
    except ResourceSpecError as exc:
        raise DagSpecError(str(exc)) from exc


def _normalize_task_semantics(
    *,
    task_kind: Any,
    resources: Dict[str, Any] | None,
    model_anchor: Dict[str, Any] | None,
    node_id: str,
) -> tuple[str, Dict[str, Any]]:
    try:
        return normalize_task_semantics(
            task_kind=task_kind,
            resources=resources,
            model_anchor=model_anchor,
        )
    except ResourceSpecError as exc:
        raise DagSpecError(f"node {node_id}: {exc}") from exc


def _validate_graph(node_ids: List[str], edges: List[Dict[str, str]]) -> None:
    graph = nx.DiGraph()
    graph.add_nodes_from(node_ids)
    for edge in edges:
        graph.add_edge(edge["source_task_id"], edge["target_task_id"])
    if not nx.is_directed_acyclic_graph(graph):
        raise DagSpecError("DAG spec contains a cycle")


def _split_endpoint(value: str, label: str) -> Tuple[str, str]:
    parts = value.split(".")
    if len(parts) != 2 or not all(parts):
        raise DagSpecError(f"edge {label} endpoint must use '<node>.<port>' syntax")
    return parts[0], parts[1]


def _ensure_node(node_id: str, node_map: Dict[str, Dict[str, Any]], label: str) -> None:
    if node_id not in node_map:
        raise DagSpecError(f"{label} references unknown node: {node_id}")


def _ensure_mapping(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise DagSpecError(f"{label} must be an object")
    return value


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else max(0, int(value))


def _optional_str_list(value: Any) -> List[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise DagSpecError("retry_on must be a list")
    return [str(item) for item in value]
