import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CATALOG_ROOT = PROJECT_ROOT / "system_catalog"
RESOURCE_WORKFLOW = CATALOG_ROOT / "workflows" / "resource_mix_demo.json"
RESOURCE_TASKS = CATALOG_ROOT / "tasks"
BACKEND_ROOT = PROJECT_ROOT / "web" / "maze_playground" / "backend"


def _request_json(base_url, path, *, method="GET", payload=None, query=None):
    if query:
        path = f"{path}?{urlencode(query)}"
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        f"{base_url}{path}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _request_error_json(base_url, path, *, method="GET", payload=None, query=None):
    try:
        _request_json(base_url, path, method=method, payload=payload, query=query)
    except HTTPError as error:
        with error:
            return error.code, json.loads(error.read().decode("utf-8"))
    pytest.fail(f"Expected HTTP error for {method} {path}")


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def playground_backend(tmp_path):
    node = shutil.which("node")
    if not node:
        sibling = Path(sys.executable).with_name("node")
        node = str(sibling) if sibling.exists() else None
    if not node or not (BACKEND_ROOT / "node_modules" / "express").exists():
        pytest.skip("Playground backend dependencies are not installed")

    port = _free_port()
    workspace_root = tmp_path / "workspaces"
    catalog_root = tmp_path / "system_catalog"
    shutil.copytree(CATALOG_ROOT, catalog_root)
    env = os.environ.copy()
    env.update({
        "PORT": str(port),
        "MAZE_WORKSPACES_DIR": str(workspace_root),
        "MAZE_SYSTEM_CATALOG_DIR": str(catalog_root),
        "PYTHON_BIN": sys.executable,
    })
    process = subprocess.Popen(
        [node, "src/server.js"],
        cwd=BACKEND_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"Playground backend exited with code {process.returncode}")
        try:
            _request_json(base_url, "/health")
            break
        except OSError:
            time.sleep(0.1)
    else:
        process.terminate()
        pytest.fail("Playground backend did not become ready")

    try:
        yield f"{base_url}/api", workspace_root
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _create_workspace(api_base, workspace_id):
    return _request_json(
        api_base,
        "/workspaces",
        method="POST",
        payload={"workspaceId": workspace_id, "name": workspace_id, "mode": "session"},
    )


def _load_resource_workflow(api_base, workspace_id):
    return _request_json(
        api_base,
        "/system-catalog/workflows/load",
        method="POST",
        payload={"workspaceId": workspace_id, "sourceId": RESOURCE_WORKFLOW.name},
    )


def test_resource_mix_bundle_matches_catalog_tasks_and_uses_cpu_gpu_queues_only():
    payload = json.loads(RESOURCE_WORKFLOW.read_text(encoding="utf-8"))
    workflow = payload["workflow"]
    definitions = {item["relativePath"]: item for item in payload["includedTasks"]}
    task_files = sorted(RESOURCE_TASKS.glob("resource_mix_*.py"))

    assert len(workflow["nodes"]) == 9
    assert len(workflow["edges"]) == 22
    assert len(definitions) == len(task_files) == 9
    assert "IO" not in workflow["name"].split()
    assert "I/O" not in payload["description"]
    assert "I/O" not in workflow["description"]
    assert all(str(tag).upper() not in {"IO", "I/O"} for tag in payload["tags"])
    assert all(str(tag).upper() not in {"IO", "I/O"} for tag in workflow["tags"])

    serialized_bundle = json.dumps(payload, ensure_ascii=False)
    assert "routing" + "_plan" not in serialized_bundle
    assert "Routing" + " Plan" not in serialized_bundle

    for task_file in task_files:
        relative_path = f"tasks/{task_file.name}"
        definition = definitions[relative_path]
        assert definition["code"] == task_file.read_text(encoding="utf-8")

    node_kinds = Counter()
    for node in workflow["nodes"]:
        task_path = node["data"]["taskPath"]
        definition = definitions[task_path]
        assert node["data"]["task_kind"] == definition["task_kind"]
        assert node["data"]["resources"] == definition["resources"]
        assert definition["resources"]["io_num"] == 0
        assert "I/O" not in node["data"]["label"]
        assert "I/O" not in definition["displayName"]
        node_kinds[definition["task_kind"]] += 1

    assert node_kinds == {"cpu": 8, "gpu": 1}
    file_operation_ids = {
        "node-load-text",
        "node-write-section-files",
        "node-final-report",
    }
    assert all(
        node["data"]["label"].startswith("CPU File:")
        for node in workflow["nodes"]
        if node["id"] in file_operation_ids
    )


def test_resource_mix_quality_gate_requires_cuda_for_pass():
    from system_catalog.tasks.resource_mix_quality_gate import resource_mix_quality_gate

    common = {
        "scorecard": {"cpu_signal": 1, "artifact_signal": 1},
        "findings": [],
        "graph_stats": {"node_count": 1},
    }
    fallback = resource_mix_quality_gate(
        **common,
        embedding_summary={"checksum": "fallback", "accelerator": "cpu-fallback"},
    )
    assert fallback["quality_gate"]["decision"] == "fail"
    assert any("GPU queue task must execute on CUDA" in note for note in fallback["quality_notes"])

    cuda = resource_mix_quality_gate(
        **common,
        embedding_summary={"checksum": "cuda", "accelerator": "cuda"},
    )
    assert cuda["quality_gate"]["decision"] == "pass"


def test_system_catalog_loads_bundle_idempotently_into_fresh_workspace(playground_backend):
    api_base, _ = playground_backend
    catalog = _request_json(api_base, "/system-catalog")["catalog"]
    assert [item["id"] for item in catalog["workflows"]] == [RESOURCE_WORKFLOW.name]
    assert all(item["id"].endswith(".py") for item in catalog["tasks"])
    assert all("__pycache__" not in item["id"] for item in catalog["tasks"])

    workspace = _create_workspace(api_base, "fresh-system-workflow")
    first = _load_resource_workflow(api_base, workspace["workspaceId"])
    assert first["workflow"]["name"] == "Resource Mix CPU GPU Artifact Demo"
    assert len(first["workflow"]["nodes"]) == 9
    assert len(first["workflow"]["edges"]) == 22
    assert len(first["importedTaskDefinitions"]["imported"]) == 9
    assert first["importedTaskDefinitions"]["skipped"] == []
    assert first["importedTaskDefinitions"]["remapped"] == []

    tasks = _request_json(
        api_base,
        "/workspace-tasks",
        query={"workspaceDir": first["workspaceDir"]},
    )
    assert len(tasks["tasks"]) == 9
    assert tasks.get("errors", []) == []
    assert all(task["resources"]["io_num"] == 0 for task in tasks["tasks"])

    second = _load_resource_workflow(api_base, workspace["workspaceId"])
    assert second["importedTaskDefinitions"]["imported"] == []
    assert len(second["importedTaskDefinitions"]["skipped"]) == 9
    assert second["importedTaskDefinitions"]["remapped"] == []
    assert second["workspaceManifestVersion"] == first["workspaceManifestVersion"]


def test_system_catalog_reuses_existing_conflict_remap(playground_backend):
    api_base, _ = playground_backend
    workspace = _create_workspace(api_base, "conflicting-system-workflow")
    workspace_dir = Path(workspace["workspaceDir"])
    conflict_path = workspace_dir / "tasks" / "resource_mix_load_text.py"
    conflict_path.write_text(
        "from maze import task\n\n"
        "@task(task_kind=\"cpu\", resources={\"cpu_num\": 1, \"gpu_mem\": 0, \"io_num\": 0})\n"
        "def resource_mix_load_text(input_path: str = \"questions.txt\"):\n"
        "    return {\"corpus\": \"workspace override\"}\n",
        encoding="utf-8",
    )

    first = _load_resource_workflow(api_base, workspace["workspaceId"])
    assert len(first["importedTaskDefinitions"]["imported"]) == 9
    assert len(first["importedTaskDefinitions"]["remapped"]) == 1
    remap = first["importedTaskDefinitions"]["remapped"][0]
    assert remap["from"] == "tasks/resource_mix_load_text.py"
    mapped_node = next(node for node in first["workflow"]["nodes"] if node["id"] == "node-load-text")
    assert mapped_node["data"]["taskPath"] == remap["to"]

    second = _load_resource_workflow(api_base, workspace["workspaceId"])
    assert second["importedTaskDefinitions"]["imported"] == []
    assert len(second["importedTaskDefinitions"]["skipped"]) == 9
    assert second["importedTaskDefinitions"]["remapped"] == [
        {"from": remap["from"], "to": remap["to"], "reason": "conflict-reused"}
    ]
    assert second["workspaceManifestVersion"] == first["workspaceManifestVersion"]
    imported_files = list((workspace_dir / "tasks" / "imported").rglob("resource_mix_load_text*.py"))
    assert len(imported_files) == 1


def test_concurrent_system_workflow_loads_materialize_once(playground_backend):
    api_base, _ = playground_backend
    workspace = _create_workspace(api_base, "concurrent-system-workflow")
    workspace_dir = Path(workspace["workspaceDir"])
    initial_manifest_version = workspace["manifest"]["manifest_version"]
    conflict_path = workspace_dir / "tasks" / "resource_mix_load_text.py"
    user_code = "# workspace-owned task\ndef resource_mix_load_text():\n    return 'keep me'\n"
    conflict_path.write_text(user_code, encoding="utf-8")

    barrier = Barrier(2)

    def load_after_barrier():
        barrier.wait(timeout=10)
        return _load_resource_workflow(api_base, workspace["workspaceId"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: load_after_barrier(), range(2)))

    assert sorted(len(item["importedTaskDefinitions"]["imported"]) for item in responses) == [0, 9]
    assert {
        item["workspaceManifestVersion"] for item in responses
    } == {initial_manifest_version + 1}
    assert sorted(
        item["importedTaskDefinitions"]["remapped"][0]["reason"] for item in responses
    ) == ["conflict", "conflict-reused"]

    mapped_paths = {
        next(node for node in item["workflow"]["nodes"] if node["id"] == "node-load-text")["data"]["taskPath"]
        for item in responses
    }
    assert len(mapped_paths) == 1
    mapped_path = mapped_paths.pop()
    assert mapped_path.startswith("tasks/imported/")
    assert conflict_path.read_text(encoding="utf-8") == user_code

    bundle = json.loads(RESOURCE_WORKFLOW.read_text(encoding="utf-8"))
    bundled_code = next(
        definition["code"]
        for definition in bundle["includedTasks"]
        if definition["relativePath"] == "tasks/resource_mix_load_text.py"
    )
    assert (workspace_dir / mapped_path).read_text(encoding="utf-8") == bundled_code
    assert len(list((workspace_dir / "tasks" / "imported").rglob("resource_mix_load_text*.py"))) == 1

    current = _request_json(api_base, f"/workspaces/{workspace['workspaceId']}")
    assert current["workspaceManifestVersion"] == initial_manifest_version + 1


def test_system_workflow_load_error_contract(playground_backend):
    api_base, workspace_root = playground_backend
    workspace = _create_workspace(api_base, "system-workflow-errors")
    catalog_workflows = workspace_root.parent / "system_catalog" / "workflows"
    (catalog_workflows / "malformed.json").write_text("{not-json", encoding="utf-8")
    (catalog_workflows / "invalid-workflow.json").write_text(
        json.dumps({"workflow": {"name": "Invalid", "nodes": []}}),
        encoding="utf-8",
    )

    cases = [
        ({"workspaceId": workspace["workspaceId"]}, 400),
        ({"workspaceId": workspace["workspaceId"], "sourceId": "../tasks/resource_mix_load_text.py"}, 400),
        ({"workspaceId": workspace["workspaceId"], "sourceId": "malformed.json"}, 400),
        ({"workspaceId": workspace["workspaceId"], "sourceId": "invalid-workflow.json"}, 400),
        ({"workspaceId": workspace["workspaceId"], "sourceId": "missing.json"}, 404),
    ]
    for payload, expected_status in cases:
        status, response = _request_error_json(
            api_base,
            "/system-catalog/workflows/load",
            method="POST",
            payload=payload,
        )
        assert status == expected_status
        assert response["error"]

    loaded = _load_resource_workflow(api_base, workspace["workspaceId"])
    assert loaded["success"] is True
    assert len(loaded["importedTaskDefinitions"]["imported"]) == 9


def test_missing_workspace_workflow_returns_not_found(playground_backend):
    api_base, _ = playground_backend
    workspace = _create_workspace(api_base, "missing-workspace-workflow")

    status, response = _request_error_json(
        api_base,
        "/workspace-workflows/load",
        method="POST",
        payload={
            "workspaceId": workspace["workspaceId"],
            "relativePath": "workflows/.drafts/current.workflow.json",
        },
    )

    assert status == 404
    assert response["error"]

    loaded = _load_resource_workflow(api_base, workspace["workspaceId"])
    assert loaded["success"] is True
    assert len(loaded["importedTaskDefinitions"]["imported"]) == 9


def test_system_workflow_load_queue_continues_after_server_error(playground_backend):
    api_base, _ = playground_backend
    workspace = _create_workspace(api_base, "system-workflow-queue-error")
    blocking_path = Path(workspace["workspaceDir"]) / "tasks" / "resource_mix_load_text.py"
    blocking_path.mkdir()

    status, response = _request_error_json(
        api_base,
        "/system-catalog/workflows/load",
        method="POST",
        payload={
            "workspaceId": workspace["workspaceId"],
            "sourceId": RESOURCE_WORKFLOW.name,
        },
    )
    assert status == 500
    assert response["error"]

    blocking_path.rmdir()
    loaded = _load_resource_workflow(api_base, workspace["workspaceId"])
    definitions = loaded["importedTaskDefinitions"]
    assert loaded["success"] is True
    assert len(definitions["imported"]) + len(definitions["skipped"]) == 9
