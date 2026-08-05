from types import MethodType

import pytest

from maze.core.path.path import MaPath, WorkflowRunConflictError
from maze.core.workflow.static_run import StaticRunStore


RUN_ID = "d4c98c23-e3f3-4df8-889f-41cab7e5f2f2"


def _path(tmp_path):
    path = object.__new__(MaPath)
    path.static_run_store = StaticRunStore(tmp_path)
    starts = []

    def start(self, workflow_id, **kwargs):
        starts.append((workflow_id, kwargs))
        self.static_run_store.save_run({
            "run_id": kwargs["run_id"],
            "workflow_id": workflow_id,
            "submission_digest": kwargs["submission_digest"],
            "status": "submitted",
        })
        return kwargs["run_id"]

    path._start_workflow = MethodType(start, path)
    return path, starts


def test_stable_run_id_replays_only_the_same_submission(tmp_path):
    path, starts = _path(tmp_path)
    kwargs = {
        "run_id": RUN_ID,
        "inputs": {"question": "same"},
    }

    assert path.run_workflow("workflow", **kwargs) == RUN_ID
    assert path.run_workflow("workflow", **kwargs) == RUN_ID
    assert len(starts) == 1

    with pytest.raises(WorkflowRunConflictError):
        path.run_workflow(
            "workflow",
            run_id=RUN_ID,
            inputs={"question": "different"},
        )


def test_stable_run_id_is_validated_before_persistence(tmp_path):
    path, starts = _path(tmp_path)

    with pytest.raises(ValueError, match="canonical UUID"):
        path.run_workflow("workflow", run_id="../not-a-run")

    assert starts == []
    assert path.static_run_store.list_runs() == []
