"""C14 metric catalog and clock-domain-safe fact extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from ascend_maze.contracts.recording import ExecutionEvent


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    name: str
    unit: str
    description: str
    higher_is_better: bool
    scope: str


@dataclass(frozen=True, slots=True)
class RunFact:
    run_id: str | None
    phase: str
    offered_at_ms: int | None
    issued_at_ms: int | None
    admitted_at_ms: int | None
    terminal_at_ms: int | None
    terminal_status: str | None
    scheduled_at_ms: int | None
    scheduled_offset_ms: int | None
    arrival_lateness_ms: int | None


@dataclass(frozen=True, slots=True)
class MetricSample:
    metric_name: str
    value: float
    run_id: str | None = None
    node_id: str | None = None
    device_id: str | None = None
    producer_id: str | None = None


@dataclass(frozen=True, slots=True)
class MetricExtraction:
    definition: MetricDefinition
    samples: tuple[MetricSample, ...]
    reason_codes: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return bool(self.samples) and not self.reason_codes


def _definition(
    name: str,
    unit: str,
    description: str,
    *,
    higher_is_better: bool = False,
    scope: str = "event",
) -> MetricDefinition:
    return MetricDefinition(name, unit, description, higher_is_better, scope)


_DEFINITIONS = (
    _definition("dct_ms", "ms", "Controller commit-to-terminal duration"),
    _definition(
        "throughput_success_per_s",
        "run/s",
        "Measurement-window successful terminal throughput",
        higher_is_better=True,
        scope="trial",
    ),
    _definition(
        "steady_state_throughput_success_per_s",
        "run/s",
        "Measurement-window successful terminal throughput",
        higher_is_better=True,
        scope="trial",
    ),
    _definition(
        "throughput_terminal_per_s",
        "run/s",
        "Measurement-window terminal throughput",
        higher_is_better=True,
        scope="trial",
    ),
    _definition(
        "offered_load_per_s",
        "run/s",
        "Measurement-window offered load",
        higher_is_better=True,
        scope="trial",
    ),
    _definition("arrival_lateness_ms", "ms", "Arrival issue lateness"),
    _definition("queue_ms", "ms", "Attempt queue-to-dispatch duration"),
    _definition("scheduler_score_ms", "ms", "Scheduler score computation duration"),
    _definition("scheduler_policy_select_ms", "ms", "Scheduler policy selection duration"),
    _definition("scheduler_placement_ms", "ms", "Scheduler placement duration"),
    _definition("scheduler_total_ms", "ms", "Scheduler score, policy and placement duration"),
    _definition(
        "scheduling_order_match",
        "ratio",
        "Whether read clients preserved the reference scheduling order",
        higher_is_better=True,
        scope="trial",
    ),
    _definition("data_binding_ms", "ms", "Input binding duration"),
    _definition("object_store_get_ms", "ms", "Object Store read duration"),
    _definition("object_store_put_ms", "ms", "Object Store write duration"),
    _definition("data_transfer_ms", "ms", "Cross-node data transfer duration"),
    _definition("data_transfer_bytes", "byte", "Cross-node transferred bytes"),
    _definition("data_publish_ms", "ms", "Output publication duration"),
    _definition("worker_acquire_ms", "ms", "Worker acquisition duration"),
    _definition("worker_cold_start_ms", "ms", "Worker cold-start duration"),
    _definition(
        "worker_standby_hit_rate",
        "ratio",
        "Fraction of Worker acquisitions served by Standby",
        higher_is_better=True,
        scope="trial",
    ),
    _definition("worker_binding_ms", "ms", "Worker device binding duration"),
    _definition("worker_code_load_ms", "ms", "Worker code loading duration"),
    _definition("worker_user_function_ms", "ms", "User function duration"),
    _definition("worker_cleanup_ms", "ms", "Worker cleanup duration"),
    _definition("model_cold_start_ms", "ms", "Model requested-to-ready duration"),
    _definition("model_ready_route_ms", "ms", "Route reservation-to-active duration"),
    _definition("inference_client_overhead_ms", "ms", "Inference client overhead"),
    _definition("ttft_ms", "ms", "Inference time to first token"),
    _definition("tpot_ms", "ms/token", "Inference time per output token"),
    _definition(
        "inference_token_throughput_per_s",
        "token/s",
        "Per-request output token throughput",
        higher_is_better=True,
    ),
    _definition("inference_queue_ms", "ms", "Inference request queue duration"),
    _definition("inference_engine_queue_depth", "request", "Inference engine queue depth"),
    _definition("inference_batch_size", "request", "Inference batch size"),
    _definition(
        "inference_prefix_cache_hit_rate",
        "ratio",
        "Inference prefix cache hit fraction",
        higher_is_better=True,
        scope="trial",
    ),
    _definition("device_hbm_free_mb", "MiB", "Per-device observed free HBM", higher_is_better=True),
    _definition("device_hbm_used_mb", "MiB", "Per-Attempt peak process HBM"),
    _definition("device_utilization_pct", "percent", "Per-device utilization", higher_is_better=True),
    _definition("device_power_w", "W", "Per-device power"),
    _definition("host_rss_mb", "MiB", "Per-Attempt peak host RSS"),
    _definition("io_bytes", "byte", "Recorded I/O bytes"),
    _definition("active_lease_count", "lease", "Active Lease concurrency"),
    _definition("fault_detection_ms", "ms", "Fault detection duration"),
    _definition("fault_cleanup_ms", "ms", "Fault cleanup barrier duration"),
    _definition("fault_backoff_ms", "ms", "Retry backoff duration"),
    _definition("fault_recovery_ms", "ms", "Fault decision-to-recovery duration"),
    _definition("resource_recovery_ms", "ms", "Resource return-to-baseline duration"),
    _definition("recorder_emit_ms", "ms", "Recorder emit duration"),
    _definition("recorder_flush_ms", "ms", "Recorder flush duration"),
    _definition("watch_client_count", "client", "Concurrent watch clients"),
    _definition("query_client_count", "client", "Concurrent query clients"),
    _definition("recorder_drop_count", "event", "Dropped recording facts"),
    _definition("recorder_writer_error_count", "error", "Recorder writer errors"),
    _definition("success_rate", "ratio", "Measurement cohort success fraction", higher_is_better=True, scope="trial"),
    _definition("oom_count", "attempt", "NPU OOM count", scope="trial"),
    _definition("retry_count", "attempt", "Retry decision count", scope="trial"),
    _definition("timeout_count", "run", "Timed-out measurement Runs", scope="trial"),
    _definition("cancellation_count", "run", "Cancelled measurement Runs", scope="trial"),
    _definition(
        "incomplete_recording_rate",
        "ratio",
        "Incomplete recording fraction",
        scope="trial",
    ),
)

METRIC_CATALOG: Mapping[str, MetricDefinition] = {
    definition.name: definition for definition in _DEFINITIONS
}
CORRECTNESS_GUARD_METRICS = frozenset(
    {
        "cancellation_count",
        "incomplete_recording_rate",
        "oom_count",
        "retry_count",
        "success_rate",
        "timeout_count",
    }
)


_PAYLOAD_METRICS: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "data_binding_ms": (("data_binding", "task_result", "task_failed"), ("data_binding_ms", "binding_duration_ms")),
    "object_store_get_ms": (("object_store_get", "data_get"), ("object_store_get_ms", "get_duration_ms")),
    "object_store_put_ms": (("object_store_put", "data_put"), ("object_store_put_ms", "put_duration_ms")),
    "data_transfer_ms": (("data_transfer",), ("data_transfer_ms", "transfer_duration_ms")),
    "data_transfer_bytes": (("data_transfer",), ("data_transfer_bytes", "size_bytes")),
    "data_publish_ms": (("data_publish", "task_result"), ("data_publish_ms", "publish_duration_ms")),
    "worker_binding_ms": (("worker_started", "task_result", "task_failed"), ("worker_binding_ms", "binding_duration_ms")),
    "worker_code_load_ms": (("worker_started", "task_result", "task_failed"), ("worker_code_load_ms", "code_load_ms")),
    "worker_user_function_ms": (("task_result", "task_failed"), ("worker_user_function_ms", "user_function_ms")),
    "worker_cleanup_ms": (("worker_released", "worker_retired"), ("worker_cleanup_ms", "cleanup_duration_ms")),
    "inference_client_overhead_ms": (("inference_request",), ("client_overhead_ms",)),
    "inference_queue_ms": (("inference_request",), ("queue_duration_ms", "inference_queue_ms")),
    "inference_batch_size": (("inference_request",), ("batch_size",)),
    "device_power_w": (("device_resource_sample",), ("power_w",)),
    "io_bytes": (("node_resource_sample", "task_result"), ("io_bytes", "bytes")),
    "fault_detection_ms": (("error_normalized", "recovery_decision"), ("detection_duration_ms", "fault_detection_ms")),
    "recorder_emit_ms": (("recorder_emit",), ("emit_duration_ms", "duration_ms")),
    "watch_client_count": (("control_client_sample",), ("watch_client_count",)),
    "query_client_count": (("control_client_sample",), ("query_client_count",)),
}


def metric_definition(name: str) -> MetricDefinition | None:
    return METRIC_CATALOG.get(name)


def metric_required_event_types(name: str) -> frozenset[str] | None:
    if name not in METRIC_CATALOG:
        return None
    if name == "dct_ms":
        return frozenset({"run_submitted", "run_terminal"})
    if name in {
        "throughput_success_per_s",
        "steady_state_throughput_success_per_s",
        "throughput_terminal_per_s",
        "offered_load_per_s",
        "arrival_lateness_ms",
        "success_rate",
        "timeout_count",
        "cancellation_count",
        "incomplete_recording_rate",
        "oom_count",
        "retry_count",
        "recorder_flush_ms",
        "recorder_drop_count",
        "recorder_writer_error_count",
        "resource_recovery_ms",
    }:
        return frozenset()
    if name == "queue_ms":
        return frozenset({"task_queued", "task_dispatched"})
    if name.startswith("scheduler_"):
        return frozenset({"scheduling_decision"})
    if name.startswith("data_") or name.startswith("object_store_"):
        return frozenset(
            {
                "data_binding",
                "data_get",
                "data_publish",
                "data_put",
                "data_transfer",
                "object_store_get",
                "object_store_put",
                "task_failed",
                "task_result",
            }
        )
    if name.startswith("worker_"):
        return frozenset(
            {
                "standby_hit",
                "task_failed",
                "task_result",
                "worker_acquired",
                "worker_lease_acquired",
                "worker_released",
                "worker_retired",
                "worker_started",
            }
        )
    if name.startswith("model_"):
        return frozenset(
            {
                "model_instance_ready",
                "model_instance_requested",
                "model_route_active",
                "model_route_reserved",
            }
        )
    if name.startswith("inference_") or name in {"ttft_ms", "tpot_ms"}:
        return frozenset({"inference_request", "attempt_inference_summary"})
    if name.startswith("device_") or name in {"host_rss_mb", "active_lease_count", "io_bytes"}:
        return frozenset(
            {"device_resource_sample", "node_resource_sample", "task_failed", "task_result"}
        )
    if name.startswith("fault_"):
        return frozenset(
            {"error_normalized", "recovery_decision", "recovery_succeeded"}
        )
    if name in {"recorder_emit_ms", "watch_client_count", "query_client_count"}:
        return frozenset({"control_client_sample", "recorder_emit"})
    return frozenset()


def extract_metric(
    name: str,
    *,
    events: Sequence[ExecutionEvent],
    runs: Sequence[RunFact],
    measurement_duration_ms: int,
    recording_complete: bool,
    flush_results: Sequence[Mapping[str, object]] = (),
    resource_after: Mapping[str, object] | None = None,
) -> MetricExtraction:
    definition = metric_definition(name)
    if definition is None:
        return MetricExtraction(
            _definition(name, "unknown", "Unknown metric"),
            (),
            ("metric_dependency_unknown",),
        )
    measured_runs = tuple(run for run in runs if run.phase == "measurement")
    run_ids = {run.run_id for run in measured_runs if run.run_id is not None}
    selected_events = tuple(event for event in events if event.run_id in run_ids)
    direct = _direct_microbenchmark_samples(name, selected_events)
    if direct:
        return MetricExtraction(definition, direct)

    if name == "dct_ms":
        samples = _dct_samples(selected_events)
    elif name in {
        "throughput_success_per_s",
        "steady_state_throughput_success_per_s",
        "throughput_terminal_per_s",
        "offered_load_per_s",
    }:
        samples = _throughput_sample(name, measured_runs, measurement_duration_ms)
    elif name == "arrival_lateness_ms":
        samples = tuple(
            MetricSample(name, float(run.arrival_lateness_ms), run.run_id)
            for run in measured_runs
            if run.arrival_lateness_ms is not None
        )
    elif name == "queue_ms":
        samples = _queue_samples(selected_events)
    elif name.startswith("scheduler_"):
        samples = _scheduler_samples(name, selected_events)
    elif name in _PAYLOAD_METRICS:
        samples = _payload_samples(name, selected_events, *_PAYLOAD_METRICS[name])
    elif name in {"worker_acquire_ms", "worker_cold_start_ms"}:
        field = "worker_acquire_ms" if name == "worker_acquire_ms" else "cold_start_ms"
        samples = _payload_samples(
            name,
            selected_events,
            ("worker_acquired", "standby_hit", "worker_lease_acquired"),
            (field,),
        )
    elif name == "worker_standby_hit_rate":
        acquisitions = tuple(
            event
            for event in selected_events
            if event.event_type in {"worker_acquired", "standby_hit", "worker_lease_acquired"}
        )
        hits = sum(
            event.event_type == "standby_hit"
            or _payload(event).get("source") == "standby"
            for event in acquisitions
        )
        samples = () if not acquisitions else (MetricSample(name, hits / len(acquisitions)),)
    elif name == "model_cold_start_ms":
        samples = _paired_identity_duration(
            name, selected_events, "model_instance_requested", "model_instance_ready", "model_instance_id"
        )
    elif name == "model_ready_route_ms":
        samples = _paired_identity_duration(
            name, selected_events, "model_route_reserved", "model_route_active", "route_lease_id"
        )
    elif name in {
        "ttft_ms",
        "tpot_ms",
        "inference_token_throughput_per_s",
        "inference_engine_queue_depth",
        "inference_prefix_cache_hit_rate",
    }:
        samples = _inference_samples(name, selected_events)
    elif name in {
        "device_hbm_free_mb",
        "device_hbm_used_mb",
        "device_utilization_pct",
        "host_rss_mb",
        "active_lease_count",
    }:
        samples = _resource_samples(name, selected_events)
    elif name == "fault_cleanup_ms":
        samples = _payload_samples(name, selected_events, ("recovery_decision",), ("cleanup_duration_ms",))
    elif name == "fault_backoff_ms":
        samples = _backoff_samples(selected_events)
    elif name == "fault_recovery_ms":
        samples = _recovery_samples(selected_events)
    elif name in {"recorder_flush_ms", "recorder_drop_count", "recorder_writer_error_count"}:
        samples = _recorder_samples(name, flush_results)
    elif name == "resource_recovery_ms":
        samples = _resource_recovery_samples(resource_after)
    elif name in {
        "success_rate",
        "oom_count",
        "retry_count",
        "timeout_count",
        "cancellation_count",
        "incomplete_recording_rate",
    }:
        samples = _guard_samples(name, measured_runs, selected_events, recording_complete)
    else:
        samples = ()
    reasons = () if samples else ("metric_required_fact_missing",)
    return MetricExtraction(definition, samples, reasons)


def _direct_microbenchmark_samples(
    metric_name: str, events: Sequence[ExecutionEvent]
) -> tuple[MetricSample, ...]:
    samples: list[MetricSample] = []
    for event in events:
        if (
            event.event_type != "microbenchmark_sample"
            or event.payload.get("metric_name") != metric_name
        ):
            continue
        value = event.payload.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        samples.append(
            MetricSample(
                metric_name=metric_name,
                value=float(value),
                run_id=event.run_id,
                node_id=event.node_id,
                device_id=event.device_id,
                producer_id=event.producer_id,
            )
        )
    return tuple(samples)


def _dct_samples(events: Sequence[ExecutionEvent]) -> tuple[MetricSample, ...]:
    by_run: dict[str, list[ExecutionEvent]] = {}
    for event in events:
        if event.run_id is not None:
            by_run.setdefault(event.run_id, []).append(event)
    samples: list[MetricSample] = []
    for run_id, run_events in sorted(by_run.items()):
        starts = [event for event in run_events if event.event_type == "run_submitted"]
        ends = [event for event in run_events if event.event_type == "run_terminal"]
        if len(starts) != 1 or len(ends) != 1:
            continue
        start, end = starts[0], ends[0]
        if start.producer_id != end.producer_id or end.monotonic_time_ms < start.monotonic_time_ms:
            continue
        samples.append(
            MetricSample(
                "dct_ms",
                float(end.monotonic_time_ms - start.monotonic_time_ms),
                run_id,
                producer_id=start.producer_id,
            )
        )
    return tuple(samples)


def _measurement_window(
    runs: Sequence[RunFact], configured_duration_ms: int
) -> tuple[int, int] | None:
    if configured_duration_ms > 0:
        bases = [
            run.scheduled_at_ms - run.scheduled_offset_ms
            for run in runs
            if run.scheduled_at_ms is not None and run.scheduled_offset_ms is not None
        ]
        if bases:
            start = min(bases)
            return start, start + configured_duration_ms
    starts = [run.offered_at_ms for run in runs if run.offered_at_ms is not None]
    ends = [run.terminal_at_ms for run in runs if run.terminal_at_ms is not None]
    if not starts or not ends or max(ends) <= min(starts):
        return None
    return min(starts), max(ends)


def _throughput_sample(
    name: str, runs: Sequence[RunFact], configured_duration_ms: int
) -> tuple[MetricSample, ...]:
    window = _measurement_window(runs, configured_duration_ms)
    if window is None:
        return ()
    start, end = window
    duration_s = (end - start) / 1000.0
    if duration_s <= 0:
        return ()
    if name == "offered_load_per_s":
        if configured_duration_ms > 0:
            count = sum(
                run.scheduled_offset_ms is not None
                and 0 <= run.scheduled_offset_ms < configured_duration_ms
                for run in runs
            )
        else:
            count = sum(
                run.offered_at_ms is not None and start <= run.offered_at_ms <= end
                for run in runs
            )
    elif name == "throughput_terminal_per_s":
        count = sum(
            run.terminal_at_ms is not None and start <= run.terminal_at_ms <= end
            for run in runs
        )
    else:
        count = sum(
            run.terminal_status == "succeeded"
            and run.terminal_at_ms is not None
            and start <= run.terminal_at_ms <= end
            for run in runs
        )
    return (MetricSample(name, count / duration_s),)


def _queue_samples(events: Sequence[ExecutionEvent]) -> tuple[MetricSample, ...]:
    queues: dict[tuple[str, str, str], list[ExecutionEvent]] = {}
    dispatches: dict[tuple[str, str, str], list[ExecutionEvent]] = {}
    for event in events:
        if event.run_id is None or event.task_id is None:
            continue
        key = (event.run_id, event.task_id, event.producer_id)
        if event.event_type == "task_queued":
            queues.setdefault(key, []).append(event)
        elif event.event_type == "task_dispatched":
            dispatches.setdefault(key, []).append(event)
    samples: list[MetricSample] = []
    for key in sorted(set(queues).intersection(dispatches)):
        starts = sorted(queues[key], key=lambda item: item.producer_sequence)
        ends = sorted(dispatches[key], key=lambda item: item.producer_sequence)
        for start, end in zip(starts, ends):
            if end.producer_sequence <= start.producer_sequence or end.monotonic_time_ms < start.monotonic_time_ms:
                continue
            samples.append(
                MetricSample(
                    "queue_ms",
                    float(end.monotonic_time_ms - start.monotonic_time_ms),
                    key[0],
                    producer_id=key[2],
                )
            )
    return tuple(samples)


def _scheduler_samples(name: str, events: Sequence[ExecutionEvent]) -> tuple[MetricSample, ...]:
    field_by_name = {
        "scheduler_score_ms": "score_compute_ms",
        "scheduler_policy_select_ms": "policy_select_ms",
        "scheduler_placement_ms": "placement_ms",
    }
    samples: list[MetricSample] = []
    for event in events:
        if event.event_type != "scheduling_decision":
            continue
        payload = _payload(event)
        if name == "scheduler_total_ms":
            values = [_number(payload.get(field)) for field in ("score_compute_ms", "policy_select_ms", "placement_ms")]
            value = None if any(item is None for item in values) else sum(item for item in values if item is not None)
        else:
            value = _number(payload.get(field_by_name[name]))
        if value is not None:
            samples.append(_event_sample(name, value, event))
    return tuple(samples)


def _payload_samples(
    name: str,
    events: Sequence[ExecutionEvent],
    event_types: tuple[str, ...],
    fields: tuple[str, ...],
) -> tuple[MetricSample, ...]:
    samples: list[MetricSample] = []
    for event in events:
        if event.event_type not in event_types:
            continue
        payload = _payload(event)
        value = next((_number(payload.get(field)) for field in fields if _number(payload.get(field)) is not None), None)
        if value is None and event.duration_ms is not None:
            value = float(event.duration_ms)
        if value is not None:
            samples.append(_event_sample(name, value, event))
    return tuple(samples)


def _paired_identity_duration(
    name: str,
    events: Sequence[ExecutionEvent],
    start_type: str,
    end_type: str,
    identity_field: str,
) -> tuple[MetricSample, ...]:
    starts: dict[tuple[str, str], ExecutionEvent] = {}
    samples: list[MetricSample] = []
    for event in sorted(events, key=lambda item: (item.producer_id, item.producer_sequence, item.event_id)):
        identity = getattr(event, identity_field)
        if identity is None:
            continue
        key = (event.producer_id, identity)
        if event.event_type == start_type:
            starts[key] = event
        elif event.event_type == end_type and key in starts:
            start = starts[key]
            if event.monotonic_time_ms >= start.monotonic_time_ms:
                samples.append(
                    MetricSample(
                        name,
                        float(event.monotonic_time_ms - start.monotonic_time_ms),
                        event.run_id,
                        event.node_id,
                        event.device_id,
                        event.producer_id,
                    )
                )
    return tuple(samples)


def _inference_samples(name: str, events: Sequence[ExecutionEvent]) -> tuple[MetricSample, ...]:
    requests = tuple(event for event in events if event.event_type == "inference_request")
    if name == "inference_prefix_cache_hit_rate":
        values = [_payload(event).get("prefix_cache_hit") for event in requests]
        known = [value for value in values if isinstance(value, bool)]
        return () if not known else (MetricSample(name, sum(known) / len(known)),)
    samples: list[MetricSample] = []
    for event in requests:
        payload = _payload(event)
        duration = _number(payload.get("duration_ms"))
        output_tokens = _number(payload.get("output_tokens"))
        ttft = _number(payload.get("ttft_ms"))
        if name == "ttft_ms":
            value = ttft
        elif name == "inference_engine_queue_depth":
            value = _number(payload.get("engine_queue_depth"))
        elif name == "inference_token_throughput_per_s":
            value = None if duration is None or duration <= 0 or output_tokens is None else output_tokens * 1000.0 / duration
        else:
            value = (
                None
                if duration is None or ttft is None or output_tokens is None or output_tokens < 2
                else max(0.0, duration - ttft) / (output_tokens - 1)
            )
        if value is not None:
            samples.append(_event_sample(name, value, event))
    return tuple(samples)


def _resource_samples(name: str, events: Sequence[ExecutionEvent]) -> tuple[MetricSample, ...]:
    fields = {
        "device_hbm_free_mb": ("device_resource_sample", "observed_free_hbm_mb"),
        "device_hbm_used_mb": ("task_result", "peak_npu_process_hbm_mb"),
        "device_utilization_pct": ("device_resource_sample", "utilization"),
        "host_rss_mb": ("task_result", "peak_host_rss_mb"),
        "active_lease_count": ("*", "active_lease_count"),
    }
    event_type, field = fields[name]
    samples: list[MetricSample] = []
    for event in events:
        if event_type != "*" and event.event_type not in {event_type, "task_failed"}:
            continue
        value = _number(_payload(event).get(field))
        if value is not None:
            samples.append(_event_sample(name, value, event))
    return tuple(samples)


def _backoff_samples(events: Sequence[ExecutionEvent]) -> tuple[MetricSample, ...]:
    samples: list[MetricSample] = []
    for event in events:
        if event.event_type != "recovery_decision":
            continue
        eligible = _number(_payload(event).get("eligible_at_ms"))
        if eligible is not None and eligible >= event.monotonic_time_ms:
            samples.append(_event_sample("fault_backoff_ms", eligible - event.monotonic_time_ms, event))
    return tuple(samples)


def _recovery_samples(events: Sequence[ExecutionEvent]) -> tuple[MetricSample, ...]:
    decisions: dict[tuple[str, str], ExecutionEvent] = {}
    samples: list[MetricSample] = []
    for event in sorted(events, key=lambda item: (item.producer_id, item.producer_sequence, item.event_id)):
        decision_id = _payload(event).get("decision_id")
        if not isinstance(decision_id, str):
            continue
        key = (event.producer_id, decision_id)
        if event.event_type == "recovery_decision":
            decisions[key] = event
        elif event.event_type == "recovery_succeeded" and key in decisions:
            start = decisions[key]
            if event.monotonic_time_ms >= start.monotonic_time_ms:
                samples.append(_event_sample("fault_recovery_ms", event.monotonic_time_ms - start.monotonic_time_ms, event))
    return tuple(samples)


def _guard_samples(
    name: str,
    runs: Sequence[RunFact],
    events: Sequence[ExecutionEvent],
    recording_complete: bool,
) -> tuple[MetricSample, ...]:
    if name == "success_rate":
        terminal = [run for run in runs if run.terminal_status is not None]
        return () if not terminal else (MetricSample(name, sum(run.terminal_status == "succeeded" for run in terminal) / len(terminal)),)
    if name == "timeout_count":
        value = sum(run.terminal_status == "timed_out" for run in runs)
    elif name == "cancellation_count":
        value = sum(run.terminal_status in {"cancelled", "interrupted"} for run in runs)
    elif name == "oom_count":
        value = sum(
            event.event_type == "error_normalized" and _payload(event).get("error_code") == "npu_oom"
            for event in events
        )
    elif name == "retry_count":
        value = sum(
            event.event_type == "recovery_decision" and _payload(event).get("action") == "retry"
            for event in events
        )
    else:
        value = 0 if recording_complete else 1
    return (MetricSample(name, float(value)),)


def _recorder_samples(
    name: str, results: Sequence[Mapping[str, object]]
) -> tuple[MetricSample, ...]:
    samples: list[MetricSample] = []
    for result in results:
        payload = result.get("payload")
        if not isinstance(payload, Mapping):
            continue
        run_id = result.get("run_id")
        effective_run_id = run_id if isinstance(run_id, str) else None
        if name == "recorder_flush_ms":
            value = _number(payload.get("flush_duration_ms"))
        elif name == "recorder_drop_count":
            control = _number(payload.get("dropped_control_event_count"))
            telemetry = _number(payload.get("dropped_telemetry_count"))
            value = None if control is None or telemetry is None else control + telemetry
        else:
            errors = payload.get("writer_errors")
            value = float(len(errors)) if isinstance(errors, (tuple, list)) else None
        if value is not None:
            samples.append(MetricSample(name, value, effective_run_id))
    return tuple(samples)


def _resource_recovery_samples(
    resource_after: Mapping[str, object] | None,
) -> tuple[MetricSample, ...]:
    if resource_after is None:
        return ()
    recovery = resource_after.get("recovery")
    if not isinstance(recovery, Mapping):
        return ()
    details = recovery.get("details")
    if not isinstance(details, Mapping):
        return ()
    value = _number(details.get("duration_ms"))
    return () if value is None else (MetricSample("resource_recovery_ms", value),)


def _event_sample(name: str, value: float, event: ExecutionEvent) -> MetricSample:
    return MetricSample(name, value, event.run_id, event.node_id, event.device_id, event.producer_id)


def _payload(event: ExecutionEvent) -> Mapping[str, object]:
    return {
        str(key): value
        for key, value in event.payload.items_tuple()
        if isinstance(key, str)
    }


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
