"""Shared helpers for structurally migrated Maze workflows.

The first migration pass preserves task names, DAG edges, resource class, and model
anchors. The task bodies intentionally use explicit Ascend-Maze inputs and outputs;
dataset-specific logic can replace individual steps without changing the topology.
"""

from __future__ import annotations

from dataclasses import dataclass

from ascend_maze import Workflow, task


@dataclass(frozen=True, slots=True)
class NodeSpec:
    name: str
    kind: str
    model: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowSpec:
    name: str
    source: str
    kind: str
    nodes: tuple[NodeSpec, ...]
    edges: tuple[tuple[str, str], ...]


def _initial_state(
    *,
    dag_id: str,
    dag_source: str,
    dag_type: str,
    question: str,
    answer: str,
    supplementary_files: object,
    metadata: object,
    step_name: str,
) -> dict[str, object]:
    return {
        "dag_id": dag_id,
        "dag_source": dag_source,
        "dag_type": dag_type,
        "question": question,
        "answer": answer,
        "supplementary_files": supplementary_files,
        "metadata": metadata,
        "completed_steps": (step_name,),
    }


def _merge_states(
    *,
    step_name: str,
    task_kind: str,
    model: str,
    state0: dict[str, object] | None,
    state1: dict[str, object] | None,
    state2: dict[str, object] | None,
    state3: dict[str, object] | None,
) -> dict[str, object]:
    merged: dict[str, object] = {}
    completed: list[str] = []
    for state in (state0, state1, state2, state3):
        if state is None:
            continue
        merged.update(state)
        raw_steps = state.get("completed_steps", ())
        if isinstance(raw_steps, (tuple, list)):
            completed.extend(str(item) for item in raw_steps)
    completed.append(step_name)
    merged["completed_steps"] = tuple(dict.fromkeys(completed))
    merged["last_step"] = step_name
    merged["last_task_kind"] = task_kind
    if model:
        merged["last_model"] = model
    return merged


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 128})
def start_cpu_step(
    dag_id: str,
    question: str,
    answer: str = "",
    supplementary_files: object = None,
    metadata: object = None,
    dag_source: str = "",
    dag_type: str = "",
    step_name: str = "",
) -> dict[str, object]:
    state = _initial_state(
        dag_id=dag_id,
        dag_source=dag_source,
        dag_type=dag_type,
        question=question,
        answer=answer,
        supplementary_files=supplementary_files,
        metadata=metadata,
        step_name=step_name,
    )
    return {"state": state}


@task(task_kind="io", resources={"cpu_num": 1, "mem": 128, "io_num": 1})
def start_io_step(
    dag_id: str,
    question: str,
    answer: str = "",
    supplementary_files: object = None,
    metadata: object = None,
    dag_source: str = "",
    dag_type: str = "",
    step_name: str = "",
) -> dict[str, object]:
    state = _initial_state(
        dag_id=dag_id,
        dag_source=dag_source,
        dag_type=dag_type,
        question=question,
        answer=answer,
        supplementary_files=supplementary_files,
        metadata=metadata,
        step_name=step_name,
    )
    return {"state": state}


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def start_npu_step(
    dag_id: str,
    question: str,
    answer: str = "",
    supplementary_files: object = None,
    metadata: object = None,
    dag_source: str = "",
    dag_type: str = "",
    step_name: str = "",
) -> dict[str, object]:
    state = _initial_state(
        dag_id=dag_id,
        dag_source=dag_source,
        dag_type=dag_type,
        question=question,
        answer=answer,
        supplementary_files=supplementary_files,
        metadata=metadata,
        step_name=step_name,
    )
    return {"state": state}


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def cpu_step(
    state0: dict[str, object] | None = None,
    state1: dict[str, object] | None = None,
    state2: dict[str, object] | None = None,
    state3: dict[str, object] | None = None,
    step_name: str = "",
    model: str = "",
) -> dict[str, object]:
    state = _merge_states(
        step_name=step_name,
        task_kind="cpu",
        model=model,
        state0=state0,
        state1=state1,
        state2=state2,
        state3=state3,
    )
    return {"state": state}


@task(task_kind="io", resources={"cpu_num": 1, "mem": 1024, "io_num": 1})
def io_step(
    state0: dict[str, object] | None = None,
    state1: dict[str, object] | None = None,
    state2: dict[str, object] | None = None,
    state3: dict[str, object] | None = None,
    step_name: str = "",
    model: str = "",
) -> dict[str, object]:
    state = _merge_states(
        step_name=step_name,
        task_kind="io",
        model=model,
        state0=state0,
        state1=state1,
        state2=state2,
        state3=state3,
    )
    return {"state": state}


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def npu_step(
    state0: dict[str, object] | None = None,
    state1: dict[str, object] | None = None,
    state2: dict[str, object] | None = None,
    state3: dict[str, object] | None = None,
    step_name: str = "",
    model: str = "",
) -> dict[str, object]:
    state = _merge_states(
        step_name=step_name,
        task_kind="npu",
        model=model,
        state0=state0,
        state1=state1,
        state2=state2,
        state3=state3,
    )
    return {"state": state}


def build_workflow(spec: WorkflowSpec) -> Workflow:
    workflow = Workflow(spec.name)
    dag_id = workflow.input("dag_id")
    question = workflow.input("question")
    answer = workflow.input("answer")
    supplementary_files = workflow.input("supplementary_files")
    metadata = workflow.input("metadata")

    predecessors: dict[str, list[str]] = {node.name: [] for node in spec.nodes}
    for source, target in spec.edges:
        predecessors[target].append(source)

    handles = {}
    for node in spec.nodes:
        parent_names = predecessors[node.name]
        model_anchor = None
        if node.kind == "npu":
            model_anchor = {
                "model": node.model or _fallback_model(node.name),
                "mode": "service",
            }
        if not parent_names:
            handle = workflow.add_task(
                _start_task_function(node.kind),
                task_name=node.name,
                inputs={
                    "dag_id": dag_id,
                    "question": question,
                    "answer": answer,
                    "supplementary_files": supplementary_files,
                    "metadata": metadata,
                    "dag_source": spec.source,
                    "dag_type": spec.kind,
                    "step_name": node.name,
                },
                model_anchor=model_anchor,
            )
        else:
            task_func = _task_function(node.kind)
            inputs: dict[str, object] = {
                "step_name": node.name,
                "model": node.model or "",
            }
            for index, parent_name in enumerate(parent_names):
                inputs[f"state{index}"] = handles[parent_name].outputs["state"]
            handle = workflow.add_task(
                task_func,
                task_name=node.name,
                inputs=inputs,
                model_anchor=model_anchor,
            )
        handles[node.name] = handle

    for source, target in spec.edges:
        workflow.add_edge(handles[source], handles[target])
    return workflow


def _start_task_function(kind: str) -> object:
    if kind == "cpu":
        return start_cpu_step
    if kind == "io":
        return start_io_step
    if kind == "npu":
        return start_npu_step
    raise ValueError(f"unknown migrated task kind: {kind}")


def _task_function(kind: str) -> object:
    if kind == "cpu":
        return cpu_step
    if kind == "io":
        return io_step
    if kind == "npu":
        return npu_step
    raise ValueError(f"unknown migrated task kind: {kind}")


def _fallback_model(task_name: str) -> str:
    lowered = task_name.lower()
    if "speech" in lowered or "whisper" in lowered:
        return "whisper-large-v3"
    if "ocr" in lowered:
        return "easyocr"
    if "blip" in lowered or "caption" in lowered:
        return "blip-image-captioning"
    if "vlm" in lowered or "vision" in lowered or "image" in lowered:
        return "qwen2.5-vl-32b"
    return "qwen3-32b"


def nodes(raw: tuple[tuple[str, str, str | None], ...]) -> tuple[NodeSpec, ...]:
    return tuple(NodeSpec(name=name, kind=kind, model=model) for name, kind, model in raw)


def edges(raw: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    return raw


def spec_inputs() -> tuple[str, ...]:
    return ("dag_id", "question", "answer", "supplementary_files", "metadata")
