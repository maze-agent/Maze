"""Runtime patches loaded only by selected vLLM-Ascend service processes.

This module is intentionally placed in a dedicated directory and injected via
``PYTHONPATH`` only when the model launch option explicitly enables it.  It is
not imported by normal Ascend-Maze control-plane processes.
"""

from __future__ import annotations

import os


def _install_qwen25vl_unique_consecutive_workaround() -> None:
    if os.environ.get("ASCEND_MAZE_QWEN25VL_CPU_UNIQUE_CONSECUTIVE") != "1":
        return

    import torch

    original = torch.unique_consecutive
    if getattr(original, "_ascend_maze_qwen25vl_cpu_workaround", False):
        return

    max_items = int(
        os.environ.get(
            "ASCEND_MAZE_QWEN25VL_CPU_UNIQUE_CONSECUTIVE_MAX_ITEMS",
            "131072",
        )
    )

    def patched_unique_consecutive(input, *args, **kwargs):  # type: ignore[no-untyped-def]
        device = getattr(getattr(input, "device", None), "type", None)
        dtype = getattr(input, "dtype", None)
        should_offload = (
            device == "npu"
            and dtype in {torch.int16, torch.int32, torch.int64}
            and getattr(input, "ndim", 0) <= 1
            and input.numel() <= max_items
            and kwargs.get("dim") is None
        )
        if should_offload:
            return original(input.detach().cpu(), *args, **kwargs)
        return original(input, *args, **kwargs)

    patched_unique_consecutive._ascend_maze_qwen25vl_cpu_workaround = True  # type: ignore[attr-defined]
    torch.unique_consecutive = patched_unique_consecutive


_install_qwen25vl_unique_consecutive_workaround()
