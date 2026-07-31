from types import SimpleNamespace

import pytest

from maze.client.maze.models import TaskOutput
from maze.client.maze.workflow import _encode_output_refs
from maze.core.path.path import MaPath
from maze.core.workflow.static_run import StaticRun
from maze.core.workflow.task import CodeTask
from maze.core.workflow.workflow import Workflow


TASK_RESOURCES = {
    "cpu": 1,
    "cpu_mem": 0,
    "gpu": 0,
    "gpu_mem": 0,
}


def _output_params(*keys: str) -> dict:
    return {
        "output_params": {
            str(index): {"key": key, "data_type": "any"}
            for index, key in enumerate(keys, start=1)
        }
    }


def _add_task(workflow: Workflow, task_id: str, *output_keys: str) -> CodeTask:
    task = CodeTask(workflow.id, task_id, task_id)
    task.save_task(
        task_input={"input_params": {}},
        task_output=_output_params(*output_keys),
        code_str="",
        code_ser="",
        resources=TASK_RESOURCES,
    )
    workflow.add_task(task_id, task)
    return task


def _workflow() -> Workflow:
    workflow = Workflow("template")
    _add_task(workflow, "prepare", "dag_id")
    _add_task(workflow, "fuse", "final_answer", "score")
    workflow.add_edge("prepare", "fuse")
    return workflow


def _output_ref(task_id: str, output_key: str) -> dict:
    return {
        "__maze_output_ref__": True,
        "task_id": task_id,
        "output_key": output_key,
    }


def test_encode_output_refs_recurses_through_dict_list_and_tuple():
    literal_reference_shape = {
        "task_id": "literal-task",
        "output_key": "literal-output",
    }
    encoded = _encode_output_refs(
        {
            "answer": TaskOutput("fuse", "final_answer"),
            "nested": [
                TaskOutput("prepare", "dag_id"),
                {
                    "tuple": (
                        TaskOutput("fuse", "score"),
                        "literal",
                    )
                },
            ],
            "literal": literal_reference_shape,
        }
    )

    assert encoded == {
        "answer": _output_ref("fuse", "final_answer"),
        "nested": [
            _output_ref("prepare", "dag_id"),
            {
                "tuple": [
                    _output_ref("fuse", "score"),
                    "literal",
                ]
            },
        ],
        "literal": literal_reference_shape,
    }


def test_static_run_resolves_declared_nested_final_outputs():
    final_output_refs = _encode_output_refs(
        {
            "final_answer": TaskOutput("fuse", "final_answer"),
            "details": [
                {"dag_id": TaskOutput("prepare", "dag_id")},
                (TaskOutput("fuse", "score"), "literal"),
            ],
            "literal": {
                "task_id": "not-a-reference",
                "output_key": "still-literal",
            },
        }
    )
    run = StaticRun(
        "run-1",
        "template",
        _workflow(),
        final_output_refs=final_output_refs,
    )

    run.mark_task_finished("prepare", {"dag_id": "gaia-1"})
    run.mark_task_finished(
        "fuse",
        {"final_answer": "FINAL_FUSED_SENTINEL", "score": 0.9},
    )

    assert run.status == "succeeded"
    assert run.result_summary == {
        "final_answer": "FINAL_FUSED_SENTINEL",
        "details": [
            {"dag_id": "gaia-1"},
            [0.9, "literal"],
        ],
        "literal": {
            "task_id": "not-a-reference",
            "output_key": "still-literal",
        },
    }


def test_static_run_without_final_output_refs_keeps_legacy_task_result_map():
    run = StaticRun("run-1", "template", _workflow())

    run.mark_task_finished("prepare", {"dag_id": "gaia-1"})
    run.mark_task_finished(
        "fuse",
        {"final_answer": "FINAL_FUSED_SENTINEL", "score": 0.9},
    )

    assert run.status == "succeeded"
    assert run.result_summary == {
        "fuse": {"final_answer": "FINAL_FUSED_SENTINEL", "score": 0.9},
        "prepare": {"dag_id": "gaia-1"},
    }


class _MemoryStaticRunStore:
    def save_run(self, _snapshot):
        return None

    def append_event(self, _run_id, _event):
        return None


def _path_for(workflow: Workflow) -> tuple[MaPath, list[dict]]:
    path = object.__new__(MaPath)
    path.workflows = {workflow.id: workflow}
    path.submit_workflows = {}
    path.async_que = {}
    path.static_runs = {}
    path.strategy = "Default"
    path.static_run_store = _MemoryStaticRunStore()
    path.global_metrics = SimpleNamespace(on_run_submitted=lambda _run_id: None)
    path.scheduler_process = SimpleNamespace(
        is_alive=lambda: True,
        pid=123,
        exitcode=None,
    )
    sent_messages = []
    path._send_scheduler_message = sent_messages.append
    return path, sent_messages


@pytest.mark.parametrize(
    "final_output_refs",
    [
        {"answer": _output_ref("missing-task", "final_answer")},
        {"answer": _output_ref("fuse", "missing-output")},
    ],
    ids=["unknown-task", "unknown-output"],
)
def test_invalid_final_output_ref_is_rejected_before_dispatch(final_output_refs):
    path, sent_messages = _path_for(_workflow())

    with pytest.raises(ValueError):
        path.run_workflow("template", final_output_refs=final_output_refs)

    assert sent_messages == []
