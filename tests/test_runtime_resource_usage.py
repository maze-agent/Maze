import time
from types import SimpleNamespace

import psutil
import pytest

from maze.metrics.resources import ProcessResourceSampler
from maze.core.scheduler.runner import execute_code_task_in_worker
from maze.core.scheduler.llm_instance import LlmInstanceManager


def test_sampler_measures_current_attempt_instead_of_process_lifetime(tmp_path):
    with ProcessResourceSampler(interval=0.01) as sample:
        memory = bytearray(12 * 1024 * 1024)
        end = time.monotonic() + 0.1
        while time.monotonic() < end:
            sum(range(5000))
        with (tmp_path / "output.bin").open("wb") as f:
            f.write(memory)
            f.flush()
    metrics = sample.metrics
    assert metrics["cpu_time_seconds"] > 0
    assert metrics["cpu_average_cores"] > 0
    assert metrics["process_rss_peak_bytes"] >= len(memory)
    assert metrics["process_io_write_bytes"] >= len(memory)
    assert metrics["process_resource_samples"] >= 2
    with ProcessResourceSampler() as idle:
        time.sleep(0.06)
    assert idle.metrics["cpu_time_seconds"] < metrics["cpu_time_seconds"]
    assert idle.metrics["process_io_write_bytes"] < metrics["process_io_write_bytes"]


def test_unavailable_os_counters_do_not_become_measured_zero(monkeypatch):
    def unavailable():
        raise psutil.AccessDenied()

    sampler = ProcessResourceSampler()
    monkeypatch.setattr(
        sampler,
        "process",
        SimpleNamespace(
            memory_info=unavailable, cpu_times=unavailable, io_counters=unavailable
        ),
    )
    with sampler:
        pass
    assert "cpu_time_seconds" not in sampler.metrics
    assert "process_rss_peak_bytes" not in sampler.metrics
    assert "process_io_read_bytes" not in sampler.metrics


@pytest.mark.parametrize("fail", [False, True])
def test_worker_reports_resource_measurements_for_success_and_failure(fail):
    code = "def run():\n    " + (
        'raise ValueError("expected")' if fail else 'return {"ok": True}'
    )
    envelope = execute_code_task_in_worker(code_str=code, task_input_data={})
    assert envelope["metrics"]["process_rss_peak_bytes"] > 0
    assert envelope["metrics"]["cpu_time_seconds"] >= 0
    assert "model_instance" not in envelope["metrics"]
    if fail:
        assert envelope["error"]["error_type"] == "user_code"
    else:
        assert envelope["result"] == {"ok": True}


def test_model_measurements_preserve_shared_scope_and_generation():
    manager = LlmInstanceManager()
    manager.register_instance(
        "instance",
        "whisper",
        "127.0.0.1",
        "node",
        0,
        8001,
        {"gpu_mem": 8192},
        backend="transformers",
        generation_id="first",
    )
    measured = {
        "memory_scope": "model_instance",
        "gpu_memory_observed_bytes": 3500 * 1024**2,
        "observed_at": 1000,
        "measurement_phase": "runtime",
    }
    manager.id_to_resource_detail["instance"]["memory_metrics"] = measured
    route = {"instance_id": "instance", "generation_id": "first"}
    result = manager.resource_metrics_for_route(route)
    assert result["gpu_memory_observed_bytes"] == measured["gpu_memory_observed_bytes"]
    assert result["gpu_id"] == 0
    assert result["memory_scope"] == "model_instance"
    result["gpu_memory_observed_bytes"] = 0
    assert manager.resource_metrics_for_route(route)["gpu_memory_observed_bytes"] > 0
    assert (
        manager.resource_metrics_for_route({**route, "generation_id": "second"}) == {}
    )


@pytest.mark.parametrize("dynamic", [False, True])
def test_snapshots_keep_failed_attempt_metrics_and_clear_them_on_new_start(dynamic):
    from maze.core.path.path import MaPath
    from maze.core.workflow.dynamic import DynamicRun
    from maze.core.workflow.task import CodeTask

    core = MaPath.__new__(MaPath)
    core._append_scheduler_event_once = lambda *a, **k: None
    observed = {"cpu_time_seconds": 0.25, "process_rss_peak_bytes": 1024}
    event = {"type": "task_exception", "data": {"task_id": "task", "metrics": observed}}
    if dynamic:
        run = DynamicRun("run")
        task = CodeTask("run", "task", "task")
        task.save_task(
            {"input_params": {}},
            {"output_params": {}},
            "def run(): return {}",
            None,
            {"cpu_num": 1},
        )
        run.tasks["task"] = task
        core.get_dynamic_run = lambda identifier: run
        core._dynamic_run_snapshot = lambda identifier: run.snapshot()
        core.dynamic_run_store = SimpleNamespace(save_run=lambda snapshot: None)
        record = core._persist_dynamic_scheduler_event
        snapshot = lambda: run.snapshot()["task_nodes"]["task"]
    else:
        from maze.core.workflow.static_run import StaticRun
        from maze.core.workflow.workflow import Workflow

        workflow = Workflow("workflow")
        task = CodeTask("workflow", "task", "task")
        task.save_task(
            {"input_params": {}},
            {"output_params": {}},
            "def run(): return {}",
            None,
            {"cpu_num": 1},
        )
        workflow.add_task("task", task)
        run = StaticRun("run", "workflow", workflow)
        core.static_runs = {"run": run}
        core.static_run_store = None
        core._persist_static_run = lambda identifier: None
        record = core._record_static_event
        snapshot = lambda: run.task_nodes["task"]
    record("run", event)
    assert snapshot()["metrics"] == observed
    observed["cpu_time_seconds"] = 999
    assert snapshot()["metrics"]["cpu_time_seconds"] == 0.25
    record("run", {"type": "start_task", "data": {"task_id": "task"}})
    assert not snapshot().get("metrics")


def test_reused_worker_resets_gpu_peaks_even_for_a_cpu_caller(monkeypatch):
    import sys

    peaks = {"allocated": 1024, "reserved": 2048}

    def reset():
        peaks.update(allocated=0, reserved=0)

    cuda = SimpleNamespace(
        is_initialized=lambda: True,
        reset_peak_memory_stats=reset,
        max_memory_allocated=lambda: peaks["allocated"],
        max_memory_reserved=lambda: peaks["reserved"],
    )
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=cuda))
    envelope = execute_code_task_in_worker(
        code_str="def run(): return {}", task_input_data={}
    )
    assert envelope["metrics"]["gpu_memory_peak_allocated_bytes"] == 0
    assert envelope["metrics"]["gpu_memory_peak_reserved_bytes"] == 0
