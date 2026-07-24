"""C14 event association indexes and integrity checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from ascend_maze.benchmark.validity import MetricValidity, ValidityIssue, stable_issues
from ascend_maze.benchmark.metrics import metric_required_event_types
from ascend_maze.contracts.recording import ExecutionEvent

_RUN_TERMINAL_STATES = frozenset(
    {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
)
_ATTEMPT_TERMINAL_EVENTS = frozenset(
    {"task_succeeded", "task_failed", "task_retry_wait", "cancel_cleanup"}
)
_ROUTE_TERMINAL_PREFIXES = (
    "model_route_released",
    "model_route_cancelled",
    "model_route_expired",
    "model_route_failed",
    "model_route_invalidated",
)
_WORKER_LEASE_START_EVENTS = frozenset(
    {"standby_hit", "worker_acquired", "worker_lease_acquired"}
)
_WORKER_LEASE_END_EVENTS = frozenset(
    {"worker_released", "worker_retired", "worker_lease_released"}
)

AttemptKey = tuple[str, str, int]


@dataclass(frozen=True, slots=True)
class AssociationIndexes:
    runs: frozenset[str]
    tasks: frozenset[tuple[str, str]]
    attempts: Mapping[AttemptKey, str]
    dispatches: Mapping[str, AttemptKey]
    placement_leases: Mapping[str, AttemptKey]
    worker_leases: Mapping[str, AttemptKey]
    route_leases: Mapping[str, AttemptKey]
    model_instances: Mapping[str, tuple[str | None, int | None]]
    producers: Mapping[str, tuple[str, ...]]
    event_ids: frozenset[str]

    def counts(self) -> dict[str, int]:
        return {
            "run": len(self.runs),
            "task": len(self.tasks),
            "attempt": len(self.attempts),
            "dispatch": len(self.dispatches),
            "placement_lease": len(self.placement_leases),
            "worker_lease": len(self.worker_leases),
            "route_lease": len(self.route_leases),
            "model_instance": len(self.model_instances),
            "producer": len(self.producers),
            "event": len(self.event_ids),
        }


@dataclass(frozen=True, slots=True)
class IndexValidationResult:
    indexes: AssociationIndexes
    issues: tuple[ValidityIssue, ...]


def build_indexes(
    *,
    run_ids: Iterable[str],
    events: Iterable[ExecutionEvent],
    expected_producers: Mapping[str, frozenset[str]],
) -> IndexValidationResult:
    ordered_events = tuple(events)
    runs = frozenset(run_ids)
    tasks = frozenset(
        (event.run_id, event.task_id)
        for event in ordered_events
        if event.run_id is not None
        and event.task_id is not None
        and event.event_type == "task_queued"
    )
    attempts: dict[AttemptKey, str] = {}
    dispatches: dict[str, AttemptKey] = {}
    placements: dict[str, AttemptKey] = {}
    worker_leases: dict[str, AttemptKey] = {}
    routes: dict[str, AttemptKey] = {}
    models: dict[str, tuple[str | None, int | None]] = {}
    producer_runs: dict[str, set[str]] = {}
    issues: list[ValidityIssue] = []

    for event in ordered_events:
        run_id = event.run_id
        if run_id is not None:
            producer_runs.setdefault(event.producer_id, set()).add(run_id)
        key = _attempt_key(event)
        payload = _payload(event)
        if event.event_type == "task_dispatched" and key is not None:
            dispatch_id = _payload_str(payload, "dispatch_id")
            if dispatch_id is None:
                issues.append(
                    ValidityIssue(
                        "dispatch_reference_dangling",
                        run_id=key[0],
                        subject=_attempt_subject(key),
                    )
                )
            else:
                _bind(
                    dispatches, dispatch_id, key, "dispatch_reference_dangling", issues
                )
                existing = attempts.get(key)
                if existing is not None and existing != dispatch_id:
                    issues.append(
                        ValidityIssue(
                            "dispatch_reference_dangling",
                            run_id=key[0],
                            subject=dispatch_id,
                        )
                    )
                attempts[key] = dispatch_id
            if event.lease_id is None:
                issues.append(
                    ValidityIssue(
                        "placement_lease_reference_dangling",
                        run_id=key[0],
                        subject=_attempt_subject(key),
                    )
                )
            else:
                _bind(
                    placements,
                    event.lease_id,
                    key,
                    "placement_lease_reference_dangling",
                    issues,
                )
            if event.route_lease_id is not None:
                _bind(
                    routes,
                    event.route_lease_id,
                    key,
                    "route_lease_reference_dangling",
                    issues,
                )

        worker_lease_id = _payload_str(payload, "worker_lease_id")
        if (
            worker_lease_id is not None
            and key is not None
            and event.event_type in _WORKER_LEASE_START_EVENTS
        ):
            _bind(
                worker_leases,
                worker_lease_id,
                key,
                "worker_lease_reference_dangling",
                issues,
            )
        if event.route_lease_id is not None and key is not None:
            if event.event_type in {"model_route_reserved", "model_route_active"}:
                _bind(
                    routes,
                    event.route_lease_id,
                    key,
                    "route_lease_reference_dangling",
                    issues,
                )
        defines_model = event.model_instance_id is not None and (
            (event.event_type == "task_dispatched" and event.route_lease_id is not None)
            or event.event_type in {"model_route_reserved", "model_route_active"}
            or (
                event.event_type
                in {
                    "model_instance_requested",
                    "model_instance_ready",
                    "model_instance_restarted",
                }
                and _payload_str(payload, "model_id") is not None
            )
        )
        if defines_model:
            assert event.model_instance_id is not None
            model_id = _payload_str(payload, "model_id")
            generation = _payload_int(payload, "instance_generation")
            identity = (model_id, generation)
            previous = models.get(event.model_instance_id)
            if previous is None or previous == (None, None):
                models[event.model_instance_id] = identity
            elif identity != (None, None) and previous != identity:
                issues.append(
                    ValidityIssue(
                        "model_instance_reference_dangling",
                        run_id=run_id,
                        subject=event.model_instance_id,
                    )
                )

    event_ids: set[str] = set()
    run_events: dict[str, list[ExecutionEvent]] = {run_id: [] for run_id in runs}
    for event in ordered_events:
        if event.event_id in event_ids:
            issues.append(
                ValidityIssue(
                    "event_id_duplicate",
                    run_id=event.run_id,
                    subject=event.event_id,
                )
            )
        event_ids.add(event.event_id)
        if event.run_id not in runs:
            issues.append(
                ValidityIssue(
                    "run_reference_dangling",
                    run_id=event.run_id,
                    subject=event.event_id,
                )
            )
            continue
        assert event.run_id is not None
        run_events[event.run_id].append(event)
        if event.experiment_id != event.run_id:
            issues.append(
                ValidityIssue(
                    "run_identity_mismatch",
                    run_id=event.run_id,
                    subject=event.event_id,
                )
            )
        if event.task_id is not None and (event.run_id, event.task_id) not in tasks:
            issues.append(
                ValidityIssue(
                    "task_reference_dangling",
                    run_id=event.run_id,
                    subject=event.task_id,
                )
            )
        key = _attempt_key(event)
        if (
            key is not None
            and event.event_type != "task_dispatched"
            and key not in attempts
        ):
            issues.append(
                ValidityIssue(
                    "task_attempt_reference_dangling",
                    run_id=event.run_id,
                    subject=_attempt_subject(key),
                )
            )
        payload = _payload(event)
        dispatch_id = _payload_str(payload, "dispatch_id")
        if dispatch_id is not None and event.event_type != "task_dispatched":
            if dispatches.get(dispatch_id) != key:
                issues.append(
                    ValidityIssue(
                        "dispatch_reference_dangling",
                        run_id=event.run_id,
                        subject=dispatch_id,
                    )
                )
        if event.lease_id is not None and placements.get(event.lease_id) != key:
            issues.append(
                ValidityIssue(
                    "placement_lease_reference_dangling",
                    run_id=event.run_id,
                    subject=event.lease_id,
                )
            )
        worker_lease_id = _payload_str(payload, "worker_lease_id")
        if worker_lease_id is not None and worker_leases.get(worker_lease_id) != key:
            issues.append(
                ValidityIssue(
                    "worker_lease_reference_dangling",
                    run_id=event.run_id,
                    subject=worker_lease_id,
                )
            )
        if event.route_lease_id is not None and routes.get(event.route_lease_id) != key:
            issues.append(
                ValidityIssue(
                    "route_lease_reference_dangling",
                    run_id=event.run_id,
                    subject=event.route_lease_id,
                )
            )
        if (
            event.model_instance_id is not None
            and event.model_instance_id not in models
        ):
            issues.append(
                ValidityIssue(
                    "model_instance_reference_dangling",
                    run_id=event.run_id,
                    subject=event.model_instance_id,
                )
            )

    _validate_terminals(run_events, issues)
    _validate_producers(run_events, expected_producers, issues)
    _validate_sequences(ordered_events, issues)
    _validate_intervals(ordered_events, attempts, worker_leases, routes, issues)
    indexes = AssociationIndexes(
        runs=runs,
        tasks=tasks,
        attempts=dict(sorted(attempts.items())),
        dispatches=dict(sorted(dispatches.items())),
        placement_leases=dict(sorted(placements.items())),
        worker_leases=dict(sorted(worker_leases.items())),
        route_leases=dict(sorted(routes.items())),
        model_instances=dict(sorted(models.items())),
        producers={
            producer: tuple(sorted(owned_runs))
            for producer, owned_runs in sorted(producer_runs.items())
        },
        event_ids=frozenset(event_ids),
    )
    return IndexValidationResult(indexes, stable_issues(issues))


def metric_validity(
    metric_names: Iterable[str],
    *,
    run_ids: Iterable[str],
    events: Iterable[ExecutionEvent],
    trial_integrity_valid: bool,
    formal_inputs_valid: bool = True,
) -> tuple[MetricValidity, ...]:
    event_tuple = tuple(events)
    direct_metrics = {
        metric_name
        for event in event_tuple
        if event.event_type == "microbenchmark_sample"
        for payload in (_payload(event),)
        for metric_name in (payload.get("metric_name"),)
        if isinstance(metric_name, str)
        and metric_name
        and isinstance(payload.get("value"), (int, float))
        and not isinstance(payload.get("value"), bool)
    }
    by_run: dict[str, tuple[ExecutionEvent, ...]] = {
        run_id: tuple(event for event in event_tuple if event.run_id == run_id)
        for run_id in run_ids
    }
    results: list[MetricValidity] = []
    for metric in sorted(set(metric_names)):
        reasons: set[str] = set()
        if not trial_integrity_valid:
            reasons.add("metric_required_fact_missing")
        if metric in direct_metrics:
            pass
        elif metric == "dct_ms":
            if any(not _has_valid_dct(events) for events in by_run.values()):
                reasons.add("metric_required_fact_missing")
        elif metric == "throughput_success_per_s":
            if not formal_inputs_valid or any(
                not _has_terminal_facts(events) for events in by_run.values()
            ):
                reasons.add("metric_required_fact_missing")
        else:
            required = metric_required_event_types(metric)
            if required is None:
                reasons.add("metric_dependency_unknown")
            elif not required and not formal_inputs_valid:
                reasons.add("metric_required_fact_missing")
            elif required and not any(
                event.event_type in required for event in event_tuple
            ):
                reasons.add("metric_required_fact_missing")
        results.append(MetricValidity(metric, not reasons, tuple(reasons)))
    return tuple(results)


def _validate_terminals(
    run_events: Mapping[str, list[ExecutionEvent]], issues: list[ValidityIssue]
) -> None:
    for run_id, events in run_events.items():
        terminals = [event for event in events if event.event_type == "run_terminal"]
        if not terminals:
            issues.append(ValidityIssue("terminal_event_missing", run_id=run_id))
            continue
        if len(terminals) != 1:
            issues.append(ValidityIssue("terminal_event_conflict", run_id=run_id))
            continue
        status = _payload_str(_payload(terminals[0]), "status")
        if status not in _RUN_TERMINAL_STATES:
            issues.append(
                ValidityIssue(
                    "terminal_event_conflict",
                    run_id=run_id,
                    subject=status or "missing_status",
                )
            )


def _validate_producers(
    run_events: Mapping[str, list[ExecutionEvent]],
    expected: Mapping[str, frozenset[str]],
    issues: list[ValidityIssue],
) -> None:
    for run_id, producer_ids in expected.items():
        seen = {event.producer_id for event in run_events.get(run_id, [])}
        for producer_id in sorted(producer_ids - seen):
            issues.append(
                ValidityIssue("producer_missing", run_id=run_id, subject=producer_id)
            )
        for producer_id in sorted(seen - producer_ids):
            issues.append(
                ValidityIssue("producer_unexpected", run_id=run_id, subject=producer_id)
            )


def _validate_sequences(
    events: tuple[ExecutionEvent, ...], issues: list[ValidityIssue]
) -> None:
    by_producer: dict[str, list[ExecutionEvent]] = {}
    for event in events:
        by_producer.setdefault(event.producer_id, []).append(event)
    for producer_id, producer_events in by_producer.items():
        raw = [event.producer_sequence for event in producer_events]
        ordered = sorted(raw)
        if len(ordered) != len(set(ordered)):
            issues.append(
                ValidityIssue("producer_sequence_duplicate", subject=producer_id)
            )
        unique = sorted(set(ordered))
        if unique and unique != list(range(unique[0], unique[-1] + 1)):
            issues.append(ValidityIssue("producer_sequence_gap", subject=producer_id))
        by_sequence = sorted(
            producer_events, key=lambda event: (event.producer_sequence, event.event_id)
        )
        if any(
            later.monotonic_time_ms < earlier.monotonic_time_ms
            for earlier, later in zip(by_sequence, by_sequence[1:])
        ):
            issues.append(
                ValidityIssue("producer_sequence_reversal", subject=producer_id)
            )


def _validate_intervals(
    events: tuple[ExecutionEvent, ...],
    attempts: Mapping[AttemptKey, str],
    worker_leases: Mapping[str, AttemptKey],
    routes: Mapping[str, AttemptKey],
    issues: list[ValidityIssue],
) -> None:
    starts: dict[AttemptKey, int] = {}
    attempt_ends: dict[AttemptKey, list[int]] = {}
    route_ends: dict[str, list[int]] = {}
    worker_starts: dict[str, int] = {}
    worker_ends: dict[str, list[int]] = {}
    for event in events:
        key = _attempt_key(event)
        if key is not None and event.event_type == "task_dispatched":
            starts[key] = min(
                starts.get(key, event.monotonic_time_ms), event.monotonic_time_ms
            )
        if key is not None and event.event_type in _ATTEMPT_TERMINAL_EVENTS:
            attempt_ends.setdefault(key, []).append(event.monotonic_time_ms)
        if event.route_lease_id is not None and event.event_type.startswith(
            _ROUTE_TERMINAL_PREFIXES
        ):
            route_ends.setdefault(event.route_lease_id, []).append(
                event.monotonic_time_ms
            )
        worker_id = _payload_str(_payload(event), "worker_lease_id")
        if worker_id is not None and event.event_type in _WORKER_LEASE_START_EVENTS:
            worker_starts[worker_id] = min(
                worker_starts.get(worker_id, event.monotonic_time_ms),
                event.monotonic_time_ms,
            )
        if worker_id is not None and event.event_type in _WORKER_LEASE_END_EVENTS:
            worker_ends.setdefault(worker_id, []).append(event.monotonic_time_ms)
    for key in attempts:
        start = starts.get(key)
        if start is not None and not attempt_ends.get(key):
            issues.append(
                ValidityIssue(
                    "task_attempt_interval_open",
                    run_id=key[0],
                    subject=_attempt_subject(key),
                )
            )
            issues.append(
                ValidityIssue(
                    "placement_lease_interval_open",
                    run_id=key[0],
                    subject=_attempt_subject(key),
                )
            )
        if start is not None and any(end < start for end in attempt_ends.get(key, [])):
            issues.append(
                ValidityIssue(
                    "task_attempt_interval_inverted",
                    run_id=key[0],
                    subject=_attempt_subject(key),
                )
            )
        if start is not None and any(
            event.monotonic_time_ms < start
            for event in events
            if event.lease_id is not None
            and _attempt_key(event) == key
            and event.event_type in _ATTEMPT_TERMINAL_EVENTS
        ):
            issues.append(
                ValidityIssue(
                    "placement_lease_interval_inverted",
                    run_id=key[0],
                    subject=_attempt_subject(key),
                )
            )
    for route_id, key in routes.items():
        start = starts.get(key)
        if start is not None and not route_ends.get(route_id):
            issues.append(
                ValidityIssue(
                    "route_lease_interval_open",
                    run_id=key[0],
                    subject=route_id,
                )
            )
        if start is not None and any(
            end < start for end in route_ends.get(route_id, [])
        ):
            issues.append(
                ValidityIssue(
                    "route_lease_interval_inverted",
                    run_id=key[0],
                    subject=route_id,
                )
            )
    for worker_id, key in worker_leases.items():
        start = worker_starts.get(worker_id)
        if start is not None and not worker_ends.get(worker_id):
            issues.append(
                ValidityIssue(
                    "worker_lease_interval_open",
                    run_id=key[0],
                    subject=worker_id,
                )
            )
        if start is not None and any(
            end < start for end in worker_ends.get(worker_id, [])
        ):
            issues.append(
                ValidityIssue(
                    "worker_lease_interval_inverted",
                    run_id=key[0],
                    subject=worker_id,
                )
            )


def _has_valid_dct(events: tuple[ExecutionEvent, ...]) -> bool:
    submitted = [event for event in events if event.event_type == "run_submitted"]
    terminal = [event for event in events if event.event_type == "run_terminal"]
    return (
        len(submitted) == 1
        and len(terminal) == 1
        and submitted[0].producer_id == terminal[0].producer_id
        and submitted[0].monotonic_time_ms <= terminal[0].monotonic_time_ms
    )


def _has_terminal_facts(events: tuple[ExecutionEvent, ...]) -> bool:
    terminal = [event for event in events if event.event_type == "run_terminal"]
    return len(terminal) == 1 and _payload_str(_payload(terminal[0]), "status") in (
        _RUN_TERMINAL_STATES
    )


def _attempt_key(event: ExecutionEvent) -> AttemptKey | None:
    if event.run_id is None or event.task_id is None or event.attempt is None:
        return None
    return (event.run_id, event.task_id, event.attempt)


def _attempt_subject(key: AttemptKey) -> str:
    return f"{key[1]}:{key[2]}"


def _bind(
    target: dict[str, AttemptKey],
    identity: str,
    key: AttemptKey,
    reason_code: str,
    issues: list[ValidityIssue],
) -> None:
    previous = target.get(identity)
    if previous is not None and previous != key:
        issues.append(ValidityIssue(reason_code, run_id=key[0], subject=identity))
        return
    target[identity] = key


def _payload(event: ExecutionEvent) -> Mapping[str, object]:
    return {
        str(key): value
        for key, value in event.payload.items_tuple()
        if isinstance(key, str)
    }


def _payload_str(payload: Mapping[str, object], name: str) -> str | None:
    value = payload.get(name)
    return value if isinstance(value, str) and value else None


def _payload_int(payload: Mapping[str, object], name: str) -> int | None:
    value = payload.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None
