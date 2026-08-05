import copy
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from maze import task, workflow
from maze.client.maze.workflow import MaWorkflow
from maze.client.maze.workflow_authoring import RUN_INPUT_REF_MARKER
from maze.core.path.path import MaPath
from maze.core.scheduler.runtime_estimator import RuntimeEstimator
from maze.core.workflow.task import CodeTask
from maze.core.workflow.dag_spec import DagSpecError, build_dag_workflow, dag_spec_from_payload
from maze.core.workflow.workflow import Workflow


TASK_RESOURCES = {"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0}


@task(resources=TASK_RESOURCES)
def _authored_task(payload: dict, endpoint: str, temperature: float) -> dict:
    return {"answer": payload["question"]}


@workflow
def _authored_workflow(question: str, endpoint: str, temperature: float = 0.25):
    result = _authored_task(
        payload={"question": question},
        endpoint=endpoint,
        temperature=temperature,
    )
    return {"final_answer": result.answer}


@workflow
def _formatted_runtime_input(question: str):
    return _authored_task(
        payload={"question": f"Question: {question}"},
        endpoint="http://model/v1",
        temperature=0.25,
    )


@workflow
def _compared_runtime_input(question: str):
    selected = "A" if question == "A" else "B"
    return _authored_task(
        payload={"question": selected},
        endpoint="http://model/v1",
        temperature=0.25,
    )


class _RecordingClient:
    def __init__(self):
        self.server_url = "http://maze.test"
        self.request_timeout = None
        self.submissions = []

    def _build_file_context(self, file_context=None, **_kwargs):
        return copy.deepcopy(file_context)

    def submit_workflow(self, spec, **_kwargs):
        self.submissions.append(copy.deepcopy(spec))
        return {
            "status": "success",
            "workflow_id": spec["workflow_id"],
            "run_id": "run-1",
        }


class _RecordingWorkflow(MaWorkflow):
    def __init__(self):
        self.client = _RecordingClient()
        super().__init__("template", self.client)

    @property
    def task_inputs(self):
        return [
            {
                "input_params": {
                    str(index): {"key": key, **copy.deepcopy(value)}
                    for index, (key, value) in enumerate(node["inputs"].items(), start=1)
                }
            }
            for node in self._nodes.values()
        ]


def _ref(key):
    return {
        RUN_INPUT_REF_MARKER: True,
        "key": key,
    }


def _input(key, schema, value, has_value):
    return {
        "key": key,
        "input_schema": schema,
        "data_type": "any",
        "value": value,
        "has_value": has_value,
    }


def _core_workflow(workflow_id="template", endpoint="http://model-a/v1"):
    core_workflow = Workflow(workflow_id)
    core_workflow.graph.graph["workflow_input_contract"] = {
        "constants": ["endpoint"],
        "runtime": {
            "question": {"required": True},
            "temperature": {"required": False, "default": 0.25},
            "api_key": {"required": True},
        },
    }
    core_task = CodeTask(workflow_id, "task", "task")
    core_task.save_task(
        task_input={
            "input_params": {
                "1": _input(
                    "payload",
                    "from_run",
                    {"question": _ref("question")},
                    False,
                ),
                "2": _input("endpoint", "from_user", endpoint, True),
                "3": _input("temperature", "from_run", _ref("temperature"), False),
                "4": _input("api_key", "from_run", _ref("api_key"), False),
            }
        },
        task_output={
            "output_params": {
                "1": {"key": "answer", "data_type": "str"},
            }
        },
        code_str="",
        code_ser="",
        resources=TASK_RESOURCES,
    )
    core_workflow.add_task(core_task.task_id, core_task)
    return core_workflow


class _MemoryStaticRunStore:
    def __init__(self):
        self.saved = []
        self.events = []

    def save_run(self, snapshot):
        self.saved.append(copy.deepcopy(snapshot))

    def append_event(self, run_id, event):
        self.events.append((run_id, copy.deepcopy(event)))


def _path_for(*workflows):
    path = object.__new__(MaPath)
    path.workflows = {item.id: item for item in workflows}
    path.submit_workflows = {}
    path.async_que = {}
    path.static_runs = {}
    path.strategy = "Default"
    path.static_run_store = _MemoryStaticRunStore()
    path.resource_history = SimpleNamespace(apply=lambda resources, *_args: resources)
    path.runtime_estimator = RuntimeEstimator()
    metric_calls = []
    path.global_metrics = SimpleNamespace(on_run_submitted=metric_calls.append)
    path.scheduler_process = SimpleNamespace(
        is_alive=lambda: True,
        pid=123,
        exitcode=None,
    )
    sent_messages = []
    path._send_scheduler_message = sent_messages.append
    return path, sent_messages, metric_calls


def _run_values(marker):
    return {
        "question": {"marker": marker},
        "api_key": "env:WORKFLOW_TEST_API_KEY",
    }


def test_workflow_authoring_keeps_constants_and_encodes_required_and_default_inputs():
    recorded = _RecordingWorkflow()

    _authored_workflow.build(recorded, inputs={"endpoint": "http://model/v1"})

    assert recorded._workflow_input_contract == {
        "constants": frozenset({"endpoint"}),
        "runtime": {
            "question": {"required": True},
            "temperature": {"required": False, "default": 0.25},
        },
    }
    params = recorded.task_inputs[0]["input_params"]
    assert params["1"]["input_schema"] == "from_run"
    assert params["1"]["value"] == {"question": _ref("question")}
    assert params["2"]["input_schema"] == "from_user"
    assert params["2"]["value"] == "http://model/v1"
    assert params["3"]["input_schema"] == "from_run"
    assert params["3"]["value"] == _ref("temperature")


@pytest.mark.parametrize(
    "definition",
    [_formatted_runtime_input, _compared_runtime_input],
)
def test_workflow_authoring_rejects_runtime_inputs_used_for_dag_construction(definition):
    recorded = _RecordingWorkflow()
    with pytest.raises(TypeError, match="question"):
        definition.build(recorded)
    assert recorded.task_inputs == []


def test_legacy_fully_bound_workflow_has_no_runtime_inputs():
    recorded = _RecordingWorkflow()

    _authored_workflow.build(
        recorded,
        inputs={
            "question": "legacy",
            "endpoint": "http://model/v1",
            "temperature": 0.5,
        },
    )

    assert recorded._workflow_input_contract["runtime"] == {}
    assert all(
        param["input_schema"] == "from_user"
        for param in recorded.task_inputs[0]["input_params"].values()
    )


def test_client_run_sends_input_payload_once():
    client_workflow = _RecordingWorkflow()
    _authored_workflow.build(client_workflow, inputs={"endpoint": "http://model/v1"})

    caller_inputs = {"question": {"marker": "A"}, "temperature": 0.75}
    assert client_workflow.run(inputs=caller_inputs) == "run-1"
    assert client_workflow.client.submissions[0]["run"]["inputs"] == caller_inputs
    assert len(client_workflow.client.submissions) == 1


def test_client_build_persists_runtime_contract_with_task():
    client_workflow = _RecordingWorkflow()
    _authored_workflow.build(
        client_workflow,
        inputs={"endpoint": "http://model/v1"},
    )
    assert client_workflow.client.submissions == []
    assert client_workflow.run(inputs={"question": "Q"}) == "run-1"

    spec = client_workflow.client.submissions[0]
    assert spec["input_contract"] == {
        "constants": ["endpoint"],
        "runtime": {
            "question": {"required": True},
            "temperature": {"required": False, "default": 0.25},
        },
    }
    assert spec["nodes"][0]["task_kind"] == "cpu"
    assert spec["nodes"][0]["inputs"]["payload"]["input_schema"] == "from_run"

    normalized = dag_spec_from_payload(spec)
    core_workflow = build_dag_workflow(spec["workflow_id"], normalized)
    assert core_workflow.graph.graph["workflow_input_contract"] == spec["input_contract"]
    input_params = next(iter(core_workflow.tasks.values())).task_input["input_params"]
    assert input_params["1"]["input_schema"] == "from_run"
    assert input_params["1"]["value"] == {"question": _ref("question")}


def test_dag_spec_rejects_malformed_nested_run_input_reference():
    with pytest.raises(DagSpecError, match="malformed workflow run input reference"):
        dag_spec_from_payload({
            "nodes": [{
                "id": "task",
                "code": "def task(value):\n    return {'result': value}",
                "inputs": {
                    "value": {
                        "input_schema": "from_run",
                        "value": [
                            _ref("valid"),
                            {RUN_INPUT_REF_MARKER: False, "key": "invalid"},
                        ],
                    }
                },
                "outputs": ["result"],
            }],
            "edges": [],
        })


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        (
            {"api_key": "env:WORKFLOW_TEST_API_KEY"},
            "Missing workflow run inputs: question",
        ),
        (
            {**_run_values("A"), "extra": True},
            "Unknown workflow run inputs: extra",
        ),
        (
            {**_run_values("A"), "endpoint": "http://other/v1"},
            "Run inputs cannot override template constants: endpoint",
        ),
    ],
)
def test_invalid_run_inputs_fail_without_run_state_or_dispatch(inputs, message):
    path, sent_messages, metric_calls = _path_for(_core_workflow())

    with pytest.raises(ValueError, match=message):
        path.run_workflow("template", inputs=inputs)

    assert sent_messages == []
    assert path.static_runs == {}
    assert path.submit_workflows == {}
    assert path.async_que == {}
    assert path.static_run_store.saved == []
    assert metric_calls == []


def test_runtime_contract_mismatch_fails_without_run_state_or_dispatch():
    template = _core_workflow()
    template.graph.graph["workflow_input_contract"]["runtime"]["unused"] = {
        "required": False,
        "default": "unused",
    }
    path, sent_messages, metric_calls = _path_for(template)

    with pytest.raises(ValueError, match="Workflow run input contract mismatch: unused"):
        path.run_workflow("template", inputs=_run_values("A"))

    assert sent_messages == []
    assert path.static_runs == {}
    assert path.submit_workflows == {}
    assert path.async_que == {}
    assert path.static_run_store.saved == []
    assert metric_calls == []


def test_run_binding_snapshots_values_defaults_and_env_references(monkeypatch):
    monkeypatch.setenv("WORKFLOW_TEST_API_KEY", "plaintext-secret")
    template = _core_workflow()
    path, sent_messages, _ = _path_for(template)
    caller_inputs = _run_values("A")

    run_id = path.run_workflow("template", inputs=caller_inputs)
    caller_inputs["question"]["marker"] = "mutated"

    run = path.static_runs[run_id]
    assert run.run_inputs == {
        "question": {"marker": "A"},
        "temperature": 0.25,
        "api_key": "env:WORKFLOW_TEST_API_KEY",
    }
    bound_params = path.submit_workflows[run_id].tasks["task"].task_input["input_params"]
    assert all(param["input_schema"] != "from_run" for param in bound_params.values())
    assert bound_params["1"]["value"] == {"question": {"marker": "A"}}
    assert bound_params["3"]["value"] == 0.25
    assert bound_params["4"]["value"] == "env:WORKFLOW_TEST_API_KEY"
    assert template.tasks["task"].task_input["input_params"]["1"]["input_schema"] == "from_run"
    assert sent_messages[0]["data"]["task_input"] == path.submit_workflows[run_id].tasks["task"].task_input
    snapshot_text = json.dumps(run.snapshot())
    assert "env:WORKFLOW_TEST_API_KEY" in snapshot_text
    assert "plaintext-secret" not in snapshot_text
    snapshot = run.snapshot()
    snapshot["run_inputs"]["question"]["marker"] = "snapshot-mutated"
    assert run.run_inputs["question"] == {"marker": "A"}


def test_same_template_and_independent_workflows_are_concurrency_isolated():
    template_a = _core_workflow("template-a", "http://model-a/v1")
    template_b = _core_workflow("template-b", "http://model-b/v1")
    path, _, _ = _path_for(template_a, template_b)

    submissions = [
        ("template-a", _run_values("A")),
        ("template-a", _run_values("B")),
        ("template-b", _run_values("C")),
    ]
    with ThreadPoolExecutor(max_workers=3) as pool:
        run_ids = list(
            pool.map(
                lambda item: path.run_workflow(item[0], inputs=item[1]),
                submissions,
            )
        )

    assert len(set(run_ids)) == 3
    for run_id, (workflow_id, values) in zip(run_ids, submissions):
        marker = values["question"]["marker"]
        run = path.static_runs[run_id]
        copied = path.submit_workflows[run_id]
        params = copied.tasks["task"].task_input["input_params"]
        assert run.workflow_id == workflow_id
        assert run.run_inputs["question"] == {"marker": marker}
        assert params["1"]["value"] == {"question": {"marker": marker}}
        expected_endpoint = (
            "http://model-a/v1"
            if workflow_id == "template-a"
            else "http://model-b/v1"
        )
        assert params["2"]["value"] == expected_endpoint
    assert template_a.tasks["task"].task_input["input_params"]["1"]["input_schema"] == "from_run"
    assert template_b.tasks["task"].task_input["input_params"]["1"]["input_schema"] == "from_run"


def test_same_file_context_is_copied_for_each_run():
    template = _core_workflow()
    path, _, _ = _path_for(template)
    shared_context = {
        "enabled": True,
        "workspace_dir": "/tmp/maze-runtime-input-files",
        "nested": {"owner": "caller"},
    }

    run_a = path.run_workflow(
        "template",
        inputs=_run_values("A"),
        file_context=shared_context,
    )
    run_b = path.run_workflow(
        "template",
        inputs=_run_values("B"),
        file_context=shared_context,
    )
    context_a = path.submit_workflows[run_a].graph.graph["file_context"]
    context_b = path.submit_workflows[run_b].graph.graph["file_context"]

    context_a["nested"]["owner"] = "run-a"
    assert context_b["nested"]["owner"] == "caller"
    assert shared_context["nested"]["owner"] == "caller"
