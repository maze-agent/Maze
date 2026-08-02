import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest
import requests
import uvicorn
from starlette.requests import Request

from maze.core.files.artifact_store import LocalCASArtifactStore
from maze.core.files.lineage import (
    TASK_RESULT_ENVELOPE,
    ArtifactError,
    run_task_with_file_context,
)
from maze.core.path.path import MaPath


@pytest.fixture
def artifact_http_server(monkeypatch, tmp_path):
    import maze.core.server as core_server

    store = LocalCASArtifactStore(tmp_path / "core-cas")
    monkeypatch.setattr(core_server, "artifact_store", store)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    server = uvicorn.Server(
        uvicorn.Config(
            core_server.app,
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_level="error",
            lifespan="off",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and thread.is_alive() and time.time() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        yield core_server, store, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_private_cas_http_capability_persists_without_secret_leak(
    artifact_http_server,
):
    _, store, base_url = artifact_http_server
    capability = store.create_capability()
    private_data = b"private-gaia-input"
    private_sha = _sha(private_data)
    store.put_bytes(
        private_sha,
        private_data,
        private=True,
        capability=capability,
    )

    anonymous_paths = [
        f"/artifacts/sha256/{private_sha}",
        f"/artifacts/sha256/{private_sha}/metadata",
    ]
    responses = [requests.get(f"{base_url}{path}", timeout=5) for path in anonymous_paths]
    responses.append(requests.head(f"{base_url}{anonymous_paths[0]}", timeout=5))
    assert [response.status_code for response in responses] == [404, 404, 404]

    wrong = requests.get(
        f"{base_url}{anonymous_paths[0]}",
        headers={"Authorization": "Bearer wrong-capability"},
        timeout=5,
    )
    assert wrong.status_code == 404
    assert "wrong-capability" not in wrong.text

    headers = {"Authorization": f"Bearer {capability}"}
    authorized = requests.get(f"{base_url}{anonymous_paths[0]}", headers=headers, timeout=5)
    metadata = requests.get(f"{base_url}{anonymous_paths[1]}", headers=headers, timeout=5)
    assert authorized.content == private_data
    assert metadata.json()["artifact"]["private"] is True

    derived_data = b"private-derived-output"
    derived_sha = _sha(derived_data)
    uploaded = requests.put(
        f"{base_url}/artifacts/sha256/{derived_sha}",
        headers=headers,
        data=derived_data,
        timeout=5,
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["private"] is True
    assert capability not in uploaded.text + metadata.text + authorized.text

    restarted_store = LocalCASArtifactStore(store.root)
    assert restarted_store.is_private(private_sha)
    assert restarted_store.is_private(derived_sha)
    restarted_store.require_read(private_sha, capability)
    with pytest.raises(PermissionError):
        restarted_store.require_read(private_sha)

    public_data = b"ordinary-public-artifact"
    public_sha = _sha(public_data)
    public_upload = requests.put(
        f"{base_url}/artifacts/sha256/{public_sha}", data=public_data, timeout=5
    )
    assert public_upload.status_code == 200
    assert requests.get(
        f"{base_url}/artifacts/sha256/{public_sha}", timeout=5
    ).content == public_data

    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.access_db_path.stat().st_mode) == 0o600
    assert capability.encode() not in store.access_db_path.read_bytes()


def test_artifact_worker_uses_remote_http_and_cleans_its_local_root(
    artifact_http_server,
    monkeypatch,
    tmp_path,
):
    _, store, base_url = artifact_http_server
    head_workspace = tmp_path / "head-only-workspace"
    source = head_workspace / "files" / "question.txt"
    source.parent.mkdir(parents=True)
    source.write_text("private-question", encoding="utf-8")
    worker_root = tmp_path / "remote-worker-local-root"
    monkeypatch.setenv("MAZE_WORKER_ARTIFACT_TMP_DIR", str(worker_root))

    context = object.__new__(MaPath)._prepare_initial_artifacts(
        {
            "enabled": True,
            "private": True,
            "workspace_dir": str(head_workspace),
            "artifact_store": {
                "type": "head_http",
                "base_url": base_url,
                "root": str(store.root),
                "private": True,
            },
        },
        "run-private",
    )
    assert "workspace_dir" not in context
    capability = context["artifact_store"]["capability"]
    observed_work_dirs = []

    def successful_task(_):
        observed_work_dirs.append(Path.cwd())
        assert Path("question.txt").read_text(encoding="utf-8") == "private-question"
        Path("answer.txt").write_text("private-answer", encoding="utf-8")
        return {"answer": "ok"}

    result = run_task_with_file_context(
        successful_task,
        {},
        {
            **context,
            "task_id": "reason",
            "attempt": 1,
            "dispatch_id": "remote-dispatch",
            "lease_id": "remote-lease",
        },
    )
    assert result[TASK_RESULT_ENVELOPE] is True
    artifact = result["file_manifest"]["files"][0]
    assert artifact["private"] is True
    assert requests.get(
        f"{base_url}/artifacts/sha256/{artifact['sha256']}", timeout=5
    ).status_code == 404
    assert requests.get(
        f"{base_url}/artifacts/sha256/{artifact['sha256']}",
        headers={"Authorization": f"Bearer {capability}"},
        timeout=5,
    ).content == b"private-answer"

    def failed_task(_):
        observed_work_dirs.append(Path.cwd())
        Path("failure.txt").write_text("failure-output", encoding="utf-8")
        raise RuntimeError("expected task failure")

    failed = run_task_with_file_context(
        failed_task,
        {},
        {
            **context,
            "task_id": "failure",
            "attempt": 1,
            "dispatch_id": "failure-dispatch",
            "lease_id": "failure-lease",
        },
    )
    assert failed["__maze_task_error_envelope__"] is True
    assert failed["error"]["error_type"] == "user_code"
    assert capability not in json.dumps(failed)
    assert all(worker_root in work_dir.parents for work_dir in observed_work_dirs)
    assert all(not work_dir.exists() for work_dir in observed_work_dirs)
    assert list(worker_root.iterdir()) == []
    assert not (head_workspace / "runs").exists()


def test_artifact_worker_cleanup_failure_is_explicit(monkeypatch, tmp_path):
    import maze.core.files.lineage as lineage

    worker_root = tmp_path / "worker-root"
    monkeypatch.setenv("MAZE_WORKER_ARTIFACT_TMP_DIR", str(worker_root))
    real_rmtree = lineage.shutil.rmtree

    def fail_cleanup(path, *args, **kwargs):
        if worker_root in Path(path).parents:
            raise OSError("injected cleanup failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(lineage.shutil, "rmtree", fail_cleanup)
    with pytest.raises(ArtifactError, match="Failed to clean Worker artifact"):
        run_task_with_file_context(
            lambda _: {},
            {},
            {
                "enabled": True,
                "run_id": "run-cleanup",
                "task_id": "task-cleanup",
                "attempt": 1,
                "dispatch_id": "dispatch-cleanup",
                "lease_id": "lease-cleanup",
                "initial_files": [],
                "parent_file_manifests": [],
                "artifact_store": {"base_url": "http://unused.invalid"},
            },
        )
    for child in worker_root.iterdir():
        real_rmtree(child)


def test_core_public_dtos_redact_artifact_capabilities(
    artifact_http_server,
    monkeypatch,
):
    core_server, _, base_url = artifact_http_server
    capability = "dto-secret-capability"
    leaked = {
        "artifact_store": {"capability": capability, "type": "head_http"},
        "nested": [{"artifact_capability": capability}],
    }

    async def fake_run(_run_id):
        return {"run_id": "run-1", **leaked}

    async def fake_task(_run_id, _task_id):
        return {"task_id": "task-1", **leaked}

    async def fake_artifacts(_run_id):
        return [{"sha256": "a" * 64, **leaked}]

    monkeypatch.setattr(core_server.mapath, "get_run_snapshot", fake_run)
    monkeypatch.setattr(core_server.mapath, "get_run_task", fake_task)
    monkeypatch.setattr(core_server.mapath, "get_run_artifacts", fake_artifacts)
    urls = [
        "/runs/run-1",
        "/runs/run-1/tasks/task-1",
        "/runs/run-1/artifacts",
    ]
    payloads = [requests.get(f"{base_url}{url}", timeout=5).text for url in urls]
    assert all(capability not in payload for payload in payloads)
    assert all("capability" not in payload.lower() for payload in payloads)


@pytest.mark.asyncio
async def test_multinode_loopback_requires_reachable_advertised_url(monkeypatch):
    import maze.core.server as core_server

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("127.0.0.1", 8000),
            "client": ("127.0.0.1", 50000),
            "path": "/run_workflow",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"127.0.0.1:8000")],
        }
    )
    context = {
        "enabled": True,
        "workspace_dir": "/tmp/maze-artifact-transport-test",
        "artifact_store": {"type": "head_http", "base_url": "http://localhost:8000"},
    }

    async def remote_cluster(timeout=2.0):
        return {
            "head_node_ip": "10.0.0.1",
            "nodes": [
                {"role": "head", "node_ip": "10.0.0.1", "alive": True},
                {"role": "worker", "node_ip": "10.0.0.2", "alive": True},
            ],
        }

    monkeypatch.setattr(core_server.mapath, "get_cluster_resources", remote_cluster)
    monkeypatch.setenv("MAZE_ARTIFACT_ADVERTISED_URL", "http://artifact-head.example:9000")
    prepared = await core_server._worker_reachable_file_context(request, context)
    assert prepared["artifact_store"]["base_url"] == "http://artifact-head.example:9000"

    monkeypatch.delenv("MAZE_ARTIFACT_ADVERTISED_URL")
    prepared = await core_server._worker_reachable_file_context(request, context)
    assert prepared["artifact_store"]["base_url"] == "http://10.0.0.1:8000"

    async def invalid_cluster(timeout=2.0):
        return {
            "head_node_ip": "127.0.0.1",
            "nodes": [
                {"role": "head", "node_ip": "127.0.0.1", "alive": True},
                {"role": "worker", "node_ip": "10.0.0.2", "alive": True},
            ],
        }

    monkeypatch.setattr(core_server.mapath, "get_cluster_resources", invalid_cluster)
    with pytest.raises(ValueError, match="Multi-node artifact transport"):
        await core_server._worker_reachable_file_context(request, context)


def _run_node_script(script: str, env: dict[str, str], cwd: Path) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Playground transport test")
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=cwd,
        env={**os.environ, **env, "MAZE_PLAYGROUND_NO_LISTEN": "1"},
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_playground_private_staging_symlink_and_gc_boundaries(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    workspaces = tmp_path / "workspaces"
    staging_root = tmp_path / "private-staging"
    artifact_root = tmp_path / "cas"
    active_blob = artifact_root / "blobs" / "aa" / "bb" / ("a" * 64)
    active_blob.parent.mkdir(parents=True)
    active_blob.write_bytes(b"active-initial-file")
    script = r"""
      import fs from 'fs/promises';
      import path from 'path';
      const hooks = (await import('./web/maze_playground/backend/src/server.js')).__artifactSecurityTestHooks;
      const context = await hooks.ensureManagedGaiaWorkspaceContext('private-test');
      const staged = await hooks.stageGaiaExecutionFile(
        context,
        'run-1',
        {name: 'input.txt', content: Buffer.from('private-input')},
      );
      const rootMode = (await fs.stat(process.env.MAZE_GAIA_STAGING_ROOT)).mode & 0o777;
      const runMode = (await fs.stat(staged.workspaceDir)).mode & 0o777;
      const filesMode = (await fs.stat(path.join(staged.workspaceDir, 'files'))).mode & 0o777;
      const fileMode = (await fs.stat(path.join(staged.workspaceDir, 'files', 'input.txt'))).mode & 0o777;
      const outside = path.join(process.env.MAZE_GAIA_STAGING_ROOT, '..', 'outside-target');
      await fs.mkdir(outside, {recursive: true});
      await fs.writeFile(path.join(outside, 'marker'), 'keep');
      await fs.rm(staged.workspaceDir, {recursive: true});
      await fs.symlink(outside, staged.workspaceDir, 'dir');
      let exchangeRejected = false;
      try { await staged.clearInput(); } catch (error) { exchangeRejected = error.code === 'GAIA_PATH_UNSAFE'; }
      await fs.unlink(staged.workspaceDir);
      let gcStatus = null;
      try { await hooks.cleanupWorkspaceArtifacts(context.workspaceDir, {dryRun: false}); }
      catch (error) { gcStatus = error.status; }
      console.log(JSON.stringify({
        rootMode, runMode, filesMode, fileMode, exchangeRejected, gcStatus,
        outsideIntact: Boolean(await fs.stat(path.join(outside, 'marker')).catch(() => null)),
        stagingOutsideWorkspace: !staged.workspaceDir.startsWith(context.workspaceDir + path.sep),
      }));
    """
    result = _run_node_script(
        script,
        {
            "MAZE_WORKSPACES_DIR": str(workspaces),
            "MAZE_GAIA_STAGING_ROOT": str(staging_root),
            "MAZE_ARTIFACT_STORE_DIR": str(artifact_root),
        },
        repo_root,
    )
    assert result == {
        "rootMode": 0o700,
        "runMode": 0o700,
        "filesMode": 0o700,
        "fileMode": 0o600,
        "exchangeRejected": True,
        "gcStatus": 403,
        "outsideIntact": True,
        "stagingOutsideWorkspace": True,
    }
    assert active_blob.read_bytes() == b"active-initial-file"

    external = tmp_path / "preexisting-symlink-target"
    external.mkdir()
    symlink_root = tmp_path / "symlink-staging-root"
    symlink_root.symlink_to(external, target_is_directory=True)
    symlink_script = r"""
      const hooks = (await import('./web/maze_playground/backend/src/server.js')).__artifactSecurityTestHooks;
      let rejected = false;
      try { await hooks.requirePrivateGaiaStagingRoot(); }
      catch (error) { rejected = error.code === 'GAIA_PATH_UNSAFE'; }
      console.log(JSON.stringify({rejected}));
    """
    assert _run_node_script(
        symlink_script,
        {"MAZE_GAIA_STAGING_ROOT": str(symlink_root)},
        repo_root,
    ) == {"rejected": True}
