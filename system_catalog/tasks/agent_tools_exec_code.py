from maze import task


@task(
    data_types={
        "path": "str",
        "code": "str",
        "timeout_seconds": "int",
        "workspace_dir": "str",
        "backend": "str",
        "input_paths": "list",
        "cpu": "int",
        "cpu_mem": "int",
        "gpu": "int",
        "gpu_mem": "int",
        "target_node_id": "str",
    },
    resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 0},
)
def exec_code(
    path: str = "",
    code: str = "",
    timeout_seconds: int = 20,
    workspace_dir: str = "",
    backend: str = "workspace_sandbox",
    input_paths: list | str | None = None,
    cpu: int = 1,
    cpu_mem: int = 128,
    gpu: int = 0,
    gpu_mem: int = 0,
    target_node_id: str = "",
):
    """Run a Python file under workspace/files, optionally writing code first."""
    from maze.client.maze.agent_exec import run_agent_exec_code

    result = run_agent_exec_code(
        path=path,
        code=code,
        timeout_seconds=timeout_seconds,
        workspace_dir=workspace_dir,
        backend=backend,
        input_paths=input_paths,
    )
    metadata = dict(result.get("metadata", {}) or {})
    metadata["resource_request"] = {
        "cpu": cpu,
        "cpu_mem": cpu_mem,
        "gpu": gpu,
        "gpu_mem": gpu_mem,
        "target_node_id": target_node_id,
    }
    return {
        "path": result.get("path", str(path or "")),
        "backend": result.get("backend", backend),
        "returncode": result.get("returncode"),
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "error": result.get("error"),
        "timed_out": result.get("timed_out", False),
        "stdout_truncated": result.get("stdout_truncated", False),
        "stderr_truncated": result.get("stderr_truncated", False),
        "generated_files": result.get("generated_files", []),
        "metadata": metadata,
    }
