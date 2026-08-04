from maze import task


@task(
    data_types={"path": "str", "max_bytes": "int", "workspace_dir": "str"},
    resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 0},
)
def read_file(path: str, max_bytes: int = 20000, workspace_dir: str = ""):
    """Read text content from a file under workspace/files."""
    import os

    from maze.client.maze.agent_permissions import permission_error_payload
    from maze.client.maze.agent_sandbox import build_workspace_sandbox
    from maze.client.maze.agent_sandbox import resolve_workspace_file

    try:
        sandbox = build_workspace_sandbox(workspace_dir)
        full_path, normalized, decision = resolve_workspace_file(
            path,
            sandbox.files_dir,
            policy=sandbox.policy,
            permission="read",
        )
        limit = int(max_bytes or 20000)
        try:
            env_limit = int(os.environ.get("MAZE_AGENT_READ_MAX_BYTES", 200000))
        except (TypeError, ValueError):
            env_limit = 200000
        env_limit = min(max(env_limit, 1), 5_000_000)
        limit = min(max(limit, 1), env_limit)
        with open(full_path, "rb") as handle:
            raw = handle.read(limit + 1)
        truncated = len(raw) > limit
        content = raw[:limit].decode("utf-8", errors="replace")
        return {
            "path": normalized,
            "content": content,
            "bytes": len(raw[:limit]),
            "truncated": truncated,
            "error": None,
            "metadata": {
                "permission": decision.to_dict() if decision is not None else None,
            },
        }
    except Exception as exc:
        return {
            "path": str(path or ""),
            "content": "",
            "bytes": 0,
            "truncated": False,
            "error": str(exc),
            "metadata": permission_error_payload(exc),
        }
