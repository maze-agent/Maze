import base64
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import cloudpickle
import pytest

from maze import get_task_metadata


CATALOG_TASKS_DIR = Path(__file__).resolve().parents[1] / "system_catalog" / "tasks"


def _load_catalog_task(file_name, function_name):
    path = CATALOG_TASKS_DIR / file_name
    spec = importlib.util.spec_from_file_location(f"test_catalog_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, getattr(module, function_name)


def _load_serialized_task(task_func):
    metadata = get_task_metadata(task_func)
    return cloudpickle.loads(base64.b64decode(metadata.code_ser))


@pytest.mark.parametrize(
    ("file_name", "function_name"),
    [
        ("distributed_gpu_probe.py", "distributed_gpu_probe"),
    ],
)
def test_catalog_builtin_metadata_matches_parser(file_name, function_name):
    from web.maze_playground.backend import maze_bridge

    source = (CATALOG_TASKS_DIR / file_name).read_text(encoding="utf-8")
    module, task_func = _load_catalog_task(file_name, function_name)
    metadata = get_task_metadata(task_func)
    parsed = maze_bridge.parse_custom_function(source)

    decorated = [
        value for value in vars(module).values() if hasattr(value, "_maze_task_metadata")
    ]
    assert decorated == [task_func]
    assert source.startswith("from maze import task")
    assert metadata.code_str.lstrip().startswith(f"def {function_name}")
    assert metadata.code_ser
    assert parsed["functionName"] == function_name
    assert parsed["inputs"] == [
        {"name": name, "dataType": metadata.data_types.get(name, "str")}
        for name in metadata.inputs
    ]
    assert parsed["outputs"] == [
        {"name": name, "dataType": metadata.data_types.get(name, "str")}
        for name in metadata.outputs
    ]
    assert parsed["resources"] == metadata.resources
    assert parsed["taskKind"] == metadata.task_kind
    assert parsed["codeStr"].lstrip().startswith(f"def {function_name}")
    assert parsed["codeSer"]


def test_custom_parser_returns_decorator_entrypoint_metadata():
    from web.maze_playground.backend import maze_bridge

    source = '''from maze import task

def helper(value):
    return value + 1

@task(
    task_kind="io",
    resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 1},
    max_retries=2,
    retry_backoff_seconds=0.5,
    retry_on=["ValueError"],
    timeout_seconds=9,
)
def custom_task(value: int = 1):
    return {"result": helper(value)}
'''

    payload = maze_bridge.parse_custom_function(source)

    assert payload["functionName"] == "custom_task"
    assert payload["taskKind"] == "io"
    assert payload["resources"] == {"cpu_num": 1, "gpu_mem": 0, "io_num": 1}
    assert payload["inputs"] == [{"name": "value", "dataType": "int"}]
    assert payload["outputs"] == [{"name": "result", "dataType": "any"}]
    assert payload["modelAnchor"] is None
    assert payload["maxRetries"] == 2
    assert payload["retryBackoffSeconds"] == 0.5
    assert payload["retryOn"] == ["ValueError"]
    assert payload["timeoutSeconds"] == 9
    assert payload["codeStr"].lstrip().startswith("def custom_task")
    assert "def helper" not in payload["codeStr"]
    assert _load_serialized_task_from_payload(payload)({"value": 4}) == {"result": 5}


def _load_serialized_task_from_payload(payload):
    return cloudpickle.loads(base64.b64decode(payload["codeSer"]))


def test_catalog_serialized_tasks_are_callable(monkeypatch):
    _, distributed_gpu_probe = _load_catalog_task(
        "distributed_gpu_probe.py", "distributed_gpu_probe"
    )

    fake_ray = SimpleNamespace(
        get_runtime_context=lambda: SimpleNamespace(get_node_id=lambda: "node-1"),
        util=SimpleNamespace(get_node_ip_address=lambda: "127.0.0.1"),
    )
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    probe_result = _load_serialized_task(distributed_gpu_probe)({
        "probe_id": 7,
        "sleep_seconds": 0,
    })
    assert probe_result["placement"]["probe_id"] == 7
    assert probe_result["placement"]["ray_node_id"] == "node-1"
    assert probe_result["placement"]["sleep_seconds"] == 0
