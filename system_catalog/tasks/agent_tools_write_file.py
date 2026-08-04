from maze import task


@task(
    data_types={
        "path": "str",
        "content": "str",
        "append": "bool",
        "workspace_dir": "str",
    },
    resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 0},
)
def write_file(path: str, content: str, append: bool = False, workspace_dir: str = ""):
    """Write text content to a file under workspace/files."""
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
            permission="write",
        )
        append_flag = append
        if isinstance(append_flag, str):
            append_flag = append_flag.strip().lower() in {"1", "true", "yes", "y", "on"}
        else:
            append_flag = bool(append_flag)

        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        mode = "a" if append_flag else "w"
        text = str(content or "")
        try:
            max_bytes = int(os.environ.get("MAZE_AGENT_WRITE_MAX_BYTES", 200000))
        except (TypeError, ValueError):
            max_bytes = 200000
        max_bytes = min(max(max_bytes, 1), 5_000_000)
        text_bytes = text.encode("utf-8")
        if len(text_bytes) > max_bytes:
            raise ValueError(
                f"content is too large for write_file ({len(text_bytes)} > {max_bytes} bytes)"
            )
        with open(full_path, mode, encoding="utf-8") as handle:
            handle.write(text)

        return {
            "path": normalized,
            "bytes": len(text_bytes),
            "appended": append_flag,
            "error": None,
            "metadata": {
                "permission": decision.to_dict() if decision is not None else None,
            },
        }
    except Exception as exc:
        return {
            "path": str(path or ""),
            "bytes": 0,
            "appended": False,
            "error": str(exc),
            "metadata": permission_error_payload(exc),
        }
