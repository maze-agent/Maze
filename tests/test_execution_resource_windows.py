import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from maze.metrics.model_resources import (
    ModelProcessReader,
    ModelResourceMonitor,
    RemoteModelObservation,
)


def monitor_fixture():
    clock = [10.0]
    values = {
        "cpu_time_seconds": 100.0,
        "process_rss_bytes": 200,
        "process_io_read_bytes": 1000,
        "process_io_write_bytes": 2000,
        "gpu_memory_observed_bytes": 100,
        "gpu_utilization_percent": 10,
    }
    monitor = ModelResourceMonitor(reader=lambda: dict(values), clock=lambda: clock[0])
    return monitor, clock, values


def test_execution_window_keeps_peaks_and_counter_deltas_and_ends_at_task_finish():
    monitor, clock, values = monitor_fixture()
    monitor.begin("first")
    clock[0] = 11
    values.update(
        cpu_time_seconds=102,
        process_rss_bytes=500,
        gpu_memory_observed_bytes=1000,
        gpu_utilization_percent=80,
    )
    monitor.current()
    clock[0] = 12
    values.update(
        cpu_time_seconds=103,
        process_rss_bytes=250,
        gpu_memory_observed_bytes=200,
        gpu_utilization_percent=20,
        process_io_read_bytes=1300,
        process_io_write_bytes=2500,
    )
    result = monitor.end("first")
    assert result["started_at"] == 10 and result["finished_at"] == 12
    assert result["duration_seconds"] == 2
    assert result["cpu_time_seconds"] == 3
    assert result["cpu_peak_cores"] == 2
    assert result["process_rss_peak_bytes"] == 500
    assert result["gpu_memory_peak_observed_bytes"] == 1000
    assert result["gpu_utilization_peak_percent"] == 80
    assert result["gpu_utilization_scope"] == "device"
    assert result["process_io_read_bytes"] == 300
    assert result["process_io_write_bytes"] == 500
    assert result["measurement_phase"] == "task_execution"
    values["gpu_memory_observed_bytes"] = 9999
    clock[0] = 20
    monitor.current()
    assert result["gpu_memory_peak_observed_bytes"] == 1000
    assert monitor.end("first") == {}


def test_a_new_task_does_not_inherit_model_lifetime_peaks():
    monitor, clock, values = monitor_fixture()
    monitor.begin("first")
    values["gpu_memory_observed_bytes"] = 9000
    clock[0] += 1
    monitor.end("first")
    values["gpu_memory_observed_bytes"] = 100
    clock[0] += 1
    monitor.begin("second")
    clock[0] += 1
    values.update(gpu_memory_observed_bytes=300, cpu_time_seconds=101)
    result = monitor.end("second")
    assert result["gpu_memory_peak_observed_bytes"] == 300
    assert result["cpu_time_seconds"] == 1


def test_shared_requests_keep_separate_windows_and_record_overlap():
    monitor, clock, values = monitor_fixture()
    monitor.begin("first")
    clock[0] += 0.5
    monitor.begin("second")
    values["gpu_memory_observed_bytes"] = 500
    clock[0] += 0.5
    first = monitor.end("first")
    values["gpu_memory_observed_bytes"] = 900
    clock[0] += 0.5
    second = monitor.end("second")
    assert first["gpu_memory_peak_observed_bytes"] == 500
    assert second["gpu_memory_peak_observed_bytes"] == 900
    assert first["max_concurrent_observations"] == 2
    assert second["max_concurrent_observations"] == 2
    assert first["finished_at"] < second["finished_at"]


def test_sampling_stops_after_the_last_task_window():
    calls = []
    monitor = ModelResourceMonitor(
        interval=0.01, reader=lambda: (calls.append(1) or {})
    ).start()
    try:
        monitor.begin("task")
        time.sleep(0.06)
        result = monitor.end("task")
        assert result["sample_count"] >= 3
        count = len(calls)
        time.sleep(0.05)
        assert len(calls) == count
    finally:
        monitor.stop()
    assert not monitor._thread.is_alive()


def test_missing_gpu_data_is_not_reported_as_zero_and_cancelled_windows_are_bounded():
    monitor = ModelResourceMonitor(reader=lambda: {}, max_windows=1)
    monitor.begin("first")
    with pytest.raises(RuntimeError, match="capacity"):
        monitor.begin("second")
    monitor.discard("first")
    with pytest.raises(RuntimeError, match="already ended"):
        monitor.begin("first")
    monitor.begin("second")
    result = monitor.end("second")
    assert "gpu_memory_peak_observed_bytes" not in result
    assert "gpu_utilization_peak_percent" not in result
    assert "cpu_peak_cores" not in result


def test_model_reader_counts_its_process_tree_and_excludes_unrelated_gpu_processes(
    monkeypatch,
):
    import sys

    counters = {1: 100.0, 2: 50.0}

    def process(pid):
        return SimpleNamespace(
            pid=pid,
            create_time=lambda: 0,
            children=lambda recursive: [process(2)] if pid == 1 else [],
            oneshot=nullcontext,
            memory_info=lambda: SimpleNamespace(rss=100 * pid),
            cpu_times=lambda: SimpleNamespace(user=counters[pid], system=0),
            io_counters=lambda: SimpleNamespace(read_bytes=0, write_bytes=0),
        )

    nvml = SimpleNamespace(
        nvmlInit=lambda: None,
        nvmlShutdown=lambda: None,
        NVMLError=RuntimeError,
        nvmlDeviceGetHandleByIndex=lambda index: index,
        nvmlDeviceGetComputeRunningProcesses=lambda handle: [
            SimpleNamespace(pid=1, usedGpuMemory=1000),
            SimpleNamespace(pid=2, usedGpuMemory=2000),
            SimpleNamespace(pid=3, usedGpuMemory=999999),
        ],
        nvmlDeviceGetUtilizationRates=lambda handle: SimpleNamespace(gpu=75),
    )
    monkeypatch.setitem(sys.modules, "pynvml", nvml)
    monkeypatch.setattr("maze.metrics.model_resources.psutil.Process", process)
    reader = ModelProcessReader(1, 0)
    first = reader()
    assert first["process_rss_bytes"] == 300
    assert first["gpu_memory_observed_bytes"] == 3000
    assert first["gpu_utilization_percent"] == 75
    counters.update({1: 101, 2: 50.5})
    assert reader()["cpu_time_seconds"] == 1.5
    reader.close()


@pytest.mark.parametrize("failure", [False, True])
def test_remote_observation_ends_on_both_success_and_task_error(monkeypatch, failure):
    import ray

    calls = []

    def method(name):
        return SimpleNamespace(
            remote=lambda token: (calls.append((name, token)) or name)
        )

    actor = SimpleNamespace(
        begin_resource_observation=method("begin"),
        end_resource_observation=method("end"),
    )
    monkeypatch.setattr(
        ray,
        "get",
        lambda ref, **k: (
            {"gpu_memory_peak_observed_bytes": 123} if ref == "end" else {}
        ),
    )
    observation = RemoteModelObservation(
        {"actor": actor, "observation_id": "attempt", "model": "whisper"}
    )
    try:
        with observation:
            if failure:
                raise ValueError("task failure")
    except ValueError:
        assert failure
    assert calls == [("begin", "attempt"), ("end", "attempt")]
    assert observation.metrics["gpu_memory_peak_observed_bytes"] == 123
    assert observation.metrics["model"] == "whisper"
    assert "actor" not in observation.metrics


def test_observer_failure_does_not_fail_task_code(monkeypatch):
    import ray

    actor = SimpleNamespace(
        begin_resource_observation=SimpleNamespace(remote=lambda token: None)
    )

    def unavailable(*args, **kwargs):
        raise RuntimeError("observer unavailable")

    monkeypatch.setattr(ray, "get", unavailable)
    with RemoteModelObservation(
        {"actor": actor, "observation_id": "attempt"}
    ) as observation:
        result = 42
    assert result == 42
    assert observation.metrics == {}
