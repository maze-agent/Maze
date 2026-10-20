from maze.metrics.usage import resource_usage
from maze.core.scheduler.runner import execute_code_task_in_worker


def test_record_has_only_the_requested_resource_fields():
    usage = resource_usage(
        {
            "cpu_time_seconds": 1,
            "cpu_peak_cores": 2,
            "process_rss_peak_bytes": 100,
            "process_io_read_bytes": 10,
            "process_io_write_bytes": 20,
        },
        4,
    )
    assert set(usage) == {"duration_seconds", "cpu", "memory", "gpu", "disk"}
    assert set(usage["cpu"]) == {"time", "average_cores", "peak_cores"}
    assert set(usage["memory"]) == {"process_rss_peak_bytes"}
    assert set(usage["gpu"]) == {"memory_peak", "utilization_peak"}
    assert set(usage["disk"]) == {"read_bytes", "write_bytes"}
    assert usage["cpu"]["average_cores"] == 0.25


def test_model_task_uses_its_execution_window_as_one_consistent_source():
    usage = resource_usage(
        {
            "cpu_time_seconds": 99,
            "cpu_peak_cores": 99,
            "process_rss_peak_bytes": 99,
            "model_instance": {
                "measurement_phase": "task_execution",
                "cpu_time_seconds": 1,
                "cpu_peak_cores": 1.5,
                "process_rss_peak_bytes": 200,
                "gpu_memory_peak_observed_bytes": 400,
                "gpu_utilization_peak_percent": 80,
                "process_io_read_bytes": 10,
                "process_io_write_bytes": 20,
            },
        },
        2,
    )
    assert usage["cpu"] == {"time": 1, "average_cores": 0.5, "peak_cores": 1.5}
    assert usage["memory"]["process_rss_peak_bytes"] == 200
    assert usage["gpu"] == {"memory_peak": 400, "utilization_peak": 80}
    assert usage["disk"] == {"read_bytes": 10, "write_bytes": 20}


def test_warmup_snapshots_are_not_promoted_to_task_gpu_peaks():
    usage = resource_usage(
        {
            "cpu_time_seconds": 0.1,
            "model_instance": {
                "measurement_phase": "warmup",
                "cpu_time_seconds": 999,
                "gpu_memory_observed_bytes": 12345,
            },
        },
        1,
    )
    assert usage["cpu"]["time"] == 0.1
    assert usage["gpu"]["memory_peak"] is None


def test_zero_is_preserved_and_missing_measurements_stay_unavailable():
    usage = resource_usage(
        {
            "cpu_time_seconds": 0,
            "gpu_memory_peak_reserved_bytes": 0,
            "process_io_read_bytes": 0,
        },
        1,
    )
    assert usage["cpu"]["time"] == usage["cpu"]["average_cores"] == 0
    assert usage["gpu"]["memory_peak"] == 0
    assert usage["disk"]["read_bytes"] == 0
    assert usage["memory"]["process_rss_peak_bytes"] is None
    assert usage["gpu"]["utilization_peak"] is None


def test_worker_and_static_snapshot_save_the_compact_record():
    from maze.core.workflow.static_run import StaticRun
    from maze.core.workflow.workflow import Workflow
    from maze.core.workflow.task import CodeTask

    envelope = execute_code_task_in_worker(
        code_str='def run(): return {"ok": True}', task_input_data={}
    )
    measured = envelope["metrics"]["resource_usage"]
    assert measured["duration_seconds"] >= 0
    assert measured["memory"]["process_rss_peak_bytes"] > 0
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
    run.mark_task_finished("task", result={"ok": True}, metrics=envelope["metrics"])
    assert run.snapshot()["task_nodes"]["task"]["resource_usage"] == measured
