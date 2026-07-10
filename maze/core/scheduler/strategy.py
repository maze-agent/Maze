from __future__ import annotations

import math
import time
from enum import Enum
from typing import Any, Dict, Tuple


EPSILON = 1e-3
DEFAULT_PREDICTED_DURATION_SECONDS = {
    "gpu": 60.0,
    "cpu": 30.0,
    "io": 10.0,
}
DEFAULT_AVG_COMPLETION_SECONDS = 60.0
HACS_ALPHA = 1.0
HACS_BETA = 1.1


class SchedulingAlgorithm(str, Enum):
    FCFS = "FCFS"
    HACS = "HACS"


def normalize_scheduling_algorithm(strategy: str | SchedulingAlgorithm | None) -> SchedulingAlgorithm:
    if strategy is None:
        return SchedulingAlgorithm.FCFS
    if isinstance(strategy, SchedulingAlgorithm):
        return strategy
    normalized = str(strategy).strip().upper()
    try:
        return SchedulingAlgorithm(normalized)
    except ValueError as exc:
        supported = ", ".join(item.value for item in SchedulingAlgorithm)
        raise ValueError(f"unsupported scheduling algorithm: {strategy!r}; supported: {supported}") from exc


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _task_kind(task: Any) -> str:
    value = str(getattr(task, "task_kind", None) or "cpu").strip().lower()
    return value if value in DEFAULT_PREDICTED_DURATION_SECONDS else "cpu"


def _context(task: Any) -> Dict[str, Any]:
    context = getattr(task, "scheduling_context", None)
    return context if isinstance(context, dict) else {}


class SchedulingStrategy:
    """Small strategy interface used by the scheduler queue manager."""

    name = SchedulingAlgorithm.FCFS.value

    def queue_name(self, task: Any) -> str:
        return _task_kind(task)

    def enqueue_key(self, task: Any, sequence: int, now: float | None = None) -> Tuple[Any, ...]:
        return (sequence,)

    def refresh_task_metadata(self, task: Any, now: float | None = None) -> Dict[str, Any]:
        metadata = {
            "strategy": self.name,
            "queue_name": self.queue_name(task),
        }
        setattr(task, "scheduling_metadata", metadata)
        return metadata


class FcfsSchedulingStrategy(SchedulingStrategy):
    name = SchedulingAlgorithm.FCFS.value

    def enqueue_key(self, task: Any, sequence: int, now: float | None = None) -> Tuple[Any, ...]:
        return (sequence,)


class HacsSchedulingStrategy(SchedulingStrategy):
    name = SchedulingAlgorithm.HACS.value

    def enqueue_key(self, task: Any, sequence: int, now: float | None = None) -> Tuple[Any, ...]:
        context = _context(task)
        enqueue_now = _float_value(context.get("workflow_submitted_time"), now or time.time())
        metadata = self.refresh_task_metadata(task, enqueue_now)
        # heap/list ordering is ascending; larger HACS score means earlier.
        return (-metadata["hacs_score"], sequence)

    def refresh_task_metadata(self, task: Any, now: float | None = None) -> Dict[str, Any]:
        now = now or time.time()
        context = _context(task)
        kind = _task_kind(task)
        mode = str(context.get("mode") or "dynamic").lower()
        if mode not in {"static", "dynamic"}:
            mode = "dynamic"

        predicted_duration = _float_value(
            context.get("predicted_duration"),
            DEFAULT_PREDICTED_DURATION_SECONDS[kind],
        )
        if predicted_duration <= 0:
            predicted_duration = DEFAULT_PREDICTED_DURATION_SECONDS[kind]
        predicted_duration = max(predicted_duration, EPSILON)

        submitted_time = _float_value(
            context.get("workflow_submitted_time"),
            _float_value(getattr(task, "created_time", None), now),
        )
        workflow_wait_time = max(0.0, now - submitted_time)
        avg_completion = max(
            _float_value(context.get("avg_completion_seconds"), DEFAULT_AVG_COMPLETION_SECONDS),
            EPSILON,
        )

        n_desc = max(0.0, _float_value(context.get("n_desc"), 0.0))
        n_anc = max(0.0, _float_value(context.get("n_anc"), 0.0))
        remaining_value_tasks = max(0.0, _float_value(context.get("remaining_value_tasks"), 0.0))

        if mode == "static":
            topological_weight = math.log2(2.0 + 2.0 * n_desc)
            phi = (workflow_wait_time / (HACS_ALPHA * avg_completion)) - remaining_value_tasks
            phi = max(-20.0, min(20.0, phi))
            value_multiplier = HACS_BETA ** phi
            hacs_score = (topological_weight * value_multiplier) / predicted_duration
        else:
            topological_weight = n_anc + (workflow_wait_time / (HACS_ALPHA * avg_completion))
            value_multiplier = 1.0
            phi = 0.0
            hacs_score = topological_weight / predicted_duration

        metadata = {
            "strategy": self.name,
            "queue_name": kind,
            "mode": mode,
            "predicted_duration": predicted_duration,
            "prediction_source": context.get("prediction_source") or "task_kind_default",
            "prediction_confidence": _float_value(context.get("prediction_confidence"), 0.0),
            "prediction_sample_count": int(_float_value(context.get("prediction_sample_count"), 0.0)),
            "code_hash": context.get("code_hash"),
            "topological_weight": topological_weight,
            "workflow_wait_time": workflow_wait_time,
            "remaining_value_tasks": remaining_value_tasks,
            "hacs_score": hacs_score,
            "hacs_breakdown": {
                "mode": mode,
                "task_kind": kind,
                "predicted_duration": predicted_duration,
                "prediction_source": context.get("prediction_source") or "task_kind_default",
                "prediction_confidence": _float_value(context.get("prediction_confidence"), 0.0),
                "prediction_sample_count": int(_float_value(context.get("prediction_sample_count"), 0.0)),
                "code_hash": context.get("code_hash"),
                "n_desc": n_desc,
                "n_anc": n_anc,
                "topological_weight": topological_weight,
                "workflow_wait_time": workflow_wait_time,
                "remaining_value_tasks": remaining_value_tasks,
                "avg_completion_seconds": avg_completion,
                "alpha": HACS_ALPHA,
                "beta": HACS_BETA,
                "phi": phi,
                "value_multiplier": value_multiplier,
                "score": hacs_score,
            },
        }
        setattr(task, "scheduling_metadata", metadata)
        return metadata


def create_scheduling_strategy(strategy: str | SchedulingAlgorithm | None) -> SchedulingStrategy:
    algorithm = normalize_scheduling_algorithm(strategy)
    if algorithm is SchedulingAlgorithm.HACS:
        return HacsSchedulingStrategy()
    return FcfsSchedulingStrategy()
