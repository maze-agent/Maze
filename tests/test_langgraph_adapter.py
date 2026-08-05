import base64
import copy

import cloudpickle
import pytest

from maze.client.langgraph import client as client_module


class _Core:
    instances = []

    def __init__(self, server_url):
        self.server_url = server_url
        self.submissions = []
        self.runs = {}
        self.__class__.instances.append(self)

    def submit_workflow(self, spec, *, artifact_mode):
        spec = copy.deepcopy(spec)
        self.submissions.append((spec, artifact_mode))
        run_id = f"run-{len(self.submissions)}"
        node = spec["nodes"][0]
        execute = cloudpickle.loads(base64.b64decode(node["code_ser"]))
        inputs = {
            "callable": node["inputs"]["callable"]["value"],
            **spec["run"]["inputs"],
        }
        try:
            result = execute(inputs)
            self.runs[run_id] = {
                "status": "succeeded",
                "result_summary": result,
            }
        except Exception as exc:
            self.runs[run_id] = {
                "status": "failed",
                "error_summary": {"message": str(exc)},
            }
        return {"workflow_id": spec["workflow_id"], "run_id": run_id}

    def wait_run(self, run_id):
        return self.runs[run_id]


@pytest.fixture
def core(monkeypatch):
    _Core.instances = []
    monkeypatch.setattr(client_module, "MaClient", _Core)
    client = client_module.LanggraphClient("http://maze.test/")
    return client, _Core.instances[0]


def test_langgraph_task_reuses_one_standard_dag_with_per_run_inputs(core):
    client, fake_core = core

    @client.task(resources={"cpu_num": 2}, task_kind="cpu")
    def combine(value, *, count=1):
        return (value, count)

    assert combine("first", count=2) == ("first", 2)
    assert combine("second", count=3) == ("second", 3)

    first, second = [submission[0] for submission in fake_core.submissions]
    assert fake_core.server_url == "http://maze.test"
    assert first["workflow_id"] == second["workflow_id"] == combine._workflow_id
    assert first["nodes"] == second["nodes"]
    assert first["nodes"][0]["resources"] == {
        "cpu_num": 2,
        "gpu_mem": 0,
        "io_num": 0,
    }
    assert first["input_contract"]["runtime"] == {
        "args": {"required": True},
        "kwargs": {"required": True},
    }
    assert first["run"]["inputs"] != second["run"]["inputs"]
    assert all(artifact_mode is False for _, artifact_mode in fake_core.submissions)


def test_langgraph_task_failure_is_a_failed_core_run(core):
    client, _fake_core = core

    @client.task
    def fail():
        raise ValueError("broken node")

    with pytest.raises(RuntimeError, match="broken node"):
        fail()


def test_langgraph_gpu_validation_remains_local(core):
    client, fake_core = core

    with pytest.raises(ValueError, match="must declare resources.gpu_mem"):
        client.task(resources={"gpu_mem": 0}, task_kind="gpu")

    assert fake_core.submissions == []
