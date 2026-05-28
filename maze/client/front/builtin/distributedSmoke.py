"""
Built-in distributed smoke tasks for checking multi-node scheduling.
"""

from maze.client.front.decorator import task


@task(
    data_types={
        "probe_id": "int",
        "sleep_seconds": "int",
        "placement": "dict",
    },
    resources={"cpu": 1, "cpu_mem": 128, "gpu": 1, "gpu_mem": 0},
)
def distributed_gpu_probe(probe_id: int = 0, sleep_seconds: int = 1):
    """Report the worker machine, Ray node, and GPU visibility for a distributed smoke task."""
    import os
    import platform
    import socket
    import time

    placement = {
        "probe_id": int(probe_id or 0),
        "hostname": socket.gethostname(),
        "platform_node": platform.node(),
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
        "pid": os.getpid(),
    }

    try:
        import ray

        placement["ray_node_id"] = ray.get_runtime_context().get_node_id()
        placement["ray_node_ip"] = ray.util.get_node_ip_address()
    except Exception as exc:
        placement["ray_error"] = str(exc)

    delay = max(0, min(int(sleep_seconds or 0), 300))
    placement["sleep_seconds"] = delay
    if delay:
        time.sleep(delay)

    return {"placement": placement}
