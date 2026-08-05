import asyncio
import copy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


class _Request:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class _Path:
    def __init__(self):
        self.created_specs = []
        self.run_calls = []

    def create_dag_workflow(self, spec):
        self.created_specs.append(spec)
        return "workflow-1"

    def get_workflow(self, workflow_id):
        assert workflow_id == "workflow-1"
        return SimpleNamespace(tasks={})

    def run_workflow(self, workflow_id, **kwargs):
        self.run_calls.append((workflow_id, kwargs))
        return "run-1"


def _spec(run=None, tags=None):
    spec = {
        "name": "contract-test",
        "nodes": [{
            "id": "task",
            "code": "def task():\n    return {'value': 1}",
            "outputs": ["value"],
        }],
        "edges": [],
    }
    if run is not None:
        spec["run"] = run
    if tags is not None:
        spec["tags"] = tags
    return spec


@pytest.fixture
def endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("MAZE_WORKSPACE_DIR", str(tmp_path))
    from maze.core import server

    path = _Path()
    reachable_contexts = []

    async def worker_reachable(_request, file_context):
        reachable_contexts.append(file_context)
        return file_context

    monkeypatch.setattr(server, "mapath", path)
    monkeypatch.setattr(server, "_worker_reachable_file_context", worker_reachable)
    monkeypatch.setattr(server, "_request_base_url", lambda _request: "http://core.test")
    return server, path, reachable_contexts


def _submit(server, payload):
    return asyncio.run(server.submit_dag_workflow(_Request(payload)))


@pytest.mark.parametrize("body", [None, [], "workflow", 1, True])
def test_submit_rejects_non_object_body_before_side_effects(endpoint, body):
    server, path, reachable_contexts = endpoint

    with pytest.raises(HTTPException) as error:
        _submit(server, body)

    assert error.value.status_code == 400
    assert error.value.detail == "request body must be a JSON object"
    assert path.created_specs == []
    assert path.run_calls == []
    assert reachable_contexts == []


def test_submit_accepts_null_tags_and_uses_envelope_artifact_mode(endpoint):
    server, path, reachable_contexts = endpoint
    payload = {
        "spec": _spec(
            run={"workspace_dir": "/workspace", "tags": ["run"]},
            tags=["spec"],
        ),
        "artifact_mode": False,
        "tags": None,
    }

    response = _submit(server, payload)

    assert response == {
        "status": "success",
        "workflow_id": "workflow-1",
        "run_id": "run-1",
        "spec": path.created_specs[0],
    }
    assert reachable_contexts == [{
        "enabled": True,
        "workspace_dir": "/workspace",
    }]
    assert path.run_calls[0][1]["file_context"] == reachable_contexts[0]
    assert path.run_calls[0][1]["tags"] == ["spec", "run", "dag"]


def test_submit_forwards_atomic_python_run_contract(endpoint):
    server, path, _reachable_contexts = endpoint
    spec = _spec(run={
        "inputs": {"question": "Q"},
        "timeout_seconds": 12,
        "idempotency_key": "submission-1",
        "idempotency_fingerprint": "a" * 64,
    })
    spec.update({
        "workflow_id": "python-template",
        "input_contract": {
            "constants": [],
            "runtime": {"question": {"required": True}},
        },
        "final_output_refs": {
            "answer": {
                "__maze_output_ref__": True,
                "task_id": "task",
                "output_key": "value",
            }
        },
    })

    response = _submit(server, {"spec": spec})

    assert response["workflow_id"] == "workflow-1"
    assert response["idempotency_key"] == "submission-1"
    assert response["idempotency_fingerprint"] == "a" * 64
    submitted_spec = path.created_specs[0]
    assert submitted_spec["workflow_id"] == "python-template"
    assert submitted_spec["input_contract"] == spec["input_contract"]
    assert submitted_spec["final_output_refs"] == spec["final_output_refs"]
    run_kwargs = path.run_calls[0][1]
    assert run_kwargs["inputs"] == {"question": "Q"}
    assert run_kwargs["final_output_refs"] == spec["final_output_refs"]
    assert run_kwargs["idempotency_key"] == "submission-1"
    assert run_kwargs["idempotency_fingerprint"] == "a" * 64


def test_stable_workflow_id_reuses_only_the_same_dag():
    from maze.core.path.path import MaPath

    path = object.__new__(MaPath)
    path.workflows = {}
    created = []
    path.global_metrics = SimpleNamespace(on_workflow_created=created.append)
    spec = _spec()
    spec["workflow_id"] = "python-template"

    assert path.create_dag_workflow(spec) == "python-template"
    assert path.create_dag_workflow(copy.deepcopy(spec)) == "python-template"
    assert created == ["python-template"]

    changed = copy.deepcopy(spec)
    changed["nodes"][0]["code"] = "def task():\n    return {'value': 2}"
    with pytest.raises(ValueError, match="different DAG"):
        path.create_dag_workflow(changed)


@pytest.mark.parametrize(
    ("run_options", "envelope", "artifact_enabled"),
    [
        ({"artifact_mode": False}, {"artifact_mode": True}, False),
        ({"artifact_mode": True}, {"artifact_mode": False}, True),
        ({}, {"artifact_mode": False}, False),
        ({}, {}, True),
    ],
)
def test_submit_artifact_mode_precedence(
    endpoint,
    run_options,
    envelope,
    artifact_enabled,
):
    server, _path, reachable_contexts = endpoint
    payload = {
        "spec": _spec(run={"workspace_dir": "/workspace", **run_options}),
        **envelope,
    }

    _submit(server, payload)

    assert ("artifact_store" in reachable_contexts[0]) is artifact_enabled


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"spec": _spec(), "tags": "one"}, "tags must be a list of strings"),
        ({"spec": _spec(), "tags": ["one", 2]}, "tags must be a list of strings"),
        ({"spec": _spec(tags="one")}, "spec.tags must be a list of strings"),
        ({"spec": _spec(tags=["one", 2])}, "spec.tags must be a list of strings"),
        (
            {"spec": _spec(run={"tags": "one"})},
            "spec.run.tags must be a list of strings",
        ),
        (
            {"spec": _spec(run={"tags": ["one", 2]})},
            "spec.run.tags must be a list of strings",
        ),
    ],
)
def test_submit_rejects_invalid_tags_before_side_effects(endpoint, payload, message):
    server, path, reachable_contexts = endpoint

    with pytest.raises(HTTPException) as error:
        _submit(server, payload)

    assert error.value.status_code == 400
    assert error.value.detail == message
    assert path.created_specs == []
    assert path.run_calls == []
    assert reachable_contexts == []


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"spec": _spec(), "artifact_mode": "false"},
            "artifact_mode must be a boolean",
        ),
        (
            {"spec": _spec(run={"artifact_mode": "false"})},
            "spec.run.artifact_mode must be a boolean",
        ),
    ],
)
def test_submit_rejects_invalid_artifact_mode_before_side_effects(
    endpoint,
    payload,
    message,
):
    server, path, reachable_contexts = endpoint

    with pytest.raises(HTTPException) as error:
        _submit(server, payload)

    assert error.value.status_code == 400
    assert error.value.detail == message
    assert path.created_specs == []
    assert path.run_calls == []
    assert reachable_contexts == []


@pytest.mark.parametrize(
    "field",
    [
        "inputs",
        "final_output_refs",
        "idempotency_key",
        "idempotency_fingerprint",
    ],
)
def test_submit_explicitly_rejects_unsupported_run_fields(endpoint, field):
    server, path, reachable_contexts = endpoint

    with pytest.raises(HTTPException) as error:
        _submit(server, {"spec": _spec(), field: None})

    assert error.value.status_code == 400
    assert error.value.detail == f"/workflows/submit does not support fields: {field}"
    assert path.created_specs == []
    assert path.run_calls == []
    assert reachable_contexts == []
