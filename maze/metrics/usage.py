"""The compact resource usage record stored on each task."""

from copy import deepcopy
import math


def _number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if math.isfinite(value) and value >= 0 else None
    return None


def resource_usage(metrics: dict | None, duration_seconds=None) -> dict:
    metrics = metrics or {}
    if isinstance(metrics.get("resource_usage"), dict):
        return deepcopy(metrics["resource_usage"])
    model = metrics.get("model_instance") or {}
    source = model if model.get("measurement_phase") == "task_execution" else metrics
    duration = _number(duration_seconds)
    cpu_time = _number(source.get("cpu_time_seconds"))
    average = (
        cpu_time / duration
        if cpu_time is not None and duration
        else _number(source.get("cpu_average_cores"))
    )
    gpu_peak = _number(source.get("gpu_memory_peak_observed_bytes"))
    if (
        gpu_peak is None
        and source is metrics
        and source.get("memory_scope") != "model_instance"
    ):
        peaks = [
            _number(source.get(key))
            for key in (
                "gpu_memory_peak_allocated_bytes",
                "gpu_memory_peak_reserved_bytes",
                "peak_cuda_allocated_bytes",
                "peak_cuda_reserved_bytes",
            )
        ]
        gpu_peak = max((value for value in peaks if value is not None), default=None)
    return {
        "duration_seconds": duration,
        "cpu": {
            "time": cpu_time,
            "average_cores": average,
            "peak_cores": _number(source.get("cpu_peak_cores")),
        },
        "memory": {
            "process_rss_peak_bytes": _number(source.get("process_rss_peak_bytes"))
        },
        "gpu": {
            "memory_peak": gpu_peak,
            "utilization_peak": _number(source.get("gpu_utilization_peak_percent")),
        },
        "disk": {
            "read_bytes": _number(source.get("process_io_read_bytes")),
            "write_bytes": _number(source.get("process_io_write_bytes")),
        },
    }
