from __future__ import annotations

import math
import os
import threading
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
HACS_ALPHA = 2.0
HACS_BETA = 5.0
HACS_COMPLETION_EMA_ALPHA = 0.2
HACS_STARVATION_SECONDS = 600.0

HACS_ALPHA_ENV = "MAZE_HACS_ALPHA"
HACS_BETA_ENV = "MAZE_HACS_BETA"
HACS_INITIAL_DCT_ENV = "MAZE_HACS_INITIAL_DCT_SECONDS"
HACS_COMPLETION_EMA_ALPHA_ENV = "MAZE_HACS_DCT_EMA_ALPHA"
HACS_STARVATION_SECONDS_ENV = "MAZE_HACS_STARVATION_SECONDS"


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


def _configured_float(
    name: str,
    default: float,
    *,
    minimum: float,
) -> float:
    raw_value = os.getenv(name)
    try:
        value = default if raw_value is None else float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value <= minimum:
        raise ValueError(f"{name} must be finite and > {minimum:g}")
    return value


def _positive_float(value: Any, name: str) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{name} must be finite and > 0")
    return normalized


def _bounded_fraction(value: Any, name: str) -> float:
    normalized = _positive_float(value, name)
    if normalized > 1:
        raise ValueError(f"{name} must be <= 1")
    return normalized


def _safe_power(base: float, exponent: float) -> float:
    return math.exp(max(-700.0, min(700.0, math.log(base) * exponent)))


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

    def priority_key(
        self,
        task: Any,
        sequence: int,
        now: float | None = None,
    ) -> Tuple[Any, ...]:
        return (sequence,)

    def enqueue_key(
        self,
        task: Any,
        sequence: int,
        now: float | None = None,
    ) -> Tuple[Any, ...]:
        return self.priority_key(task, sequence, now)

    def refresh_task_metadata(self, task: Any, now: float | None = None) -> Dict[str, Any]:
        metadata = {
            "strategy": self.name,
            "queue_name": self.queue_name(task),
        }
        setattr(task, "scheduling_metadata", metadata)
        return metadata

    def observe_workflow_completion(self, duration_seconds: float) -> bool:
        return False


class FcfsSchedulingStrategy(SchedulingStrategy):
    name = SchedulingAlgorithm.FCFS.value

    def priority_key(
        self,
        task: Any,
        sequence: int,
        now: float | None = None,
    ) -> Tuple[Any, ...]:
        return (sequence,)


class HacsSchedulingStrategy(SchedulingStrategy):
    name = SchedulingAlgorithm.HACS.value

    def __init__(
        self,
        *,
        alpha: float = HACS_ALPHA,
        beta: float = HACS_BETA,
        initial_avg_completion_seconds: float = DEFAULT_AVG_COMPLETION_SECONDS,
        completion_ema_alpha: float = HACS_COMPLETION_EMA_ALPHA,
        starvation_seconds: float = HACS_STARVATION_SECONDS,
    ):
        self.alpha = _positive_float(alpha, "HACS alpha")
        self.beta = _positive_float(beta, "HACS beta")
        if self.beta <= 1:
            raise ValueError("HACS beta must be > 1")
        self.completion_ema_alpha = _bounded_fraction(
            completion_ema_alpha,
            "HACS DCT EMA alpha",
        )
        self.starvation_seconds = _positive_float(
            starvation_seconds,
            "HACS starvation seconds",
        )
        self._avg_completion_seconds = _positive_float(
            initial_avg_completion_seconds,
            "HACS initial DCT seconds",
        )
        self._completion_sample_count = 0
        self._completion_lock = threading.Lock()

    @property
    def avg_completion_seconds(self) -> float:
        with self._completion_lock:
            return self._avg_completion_seconds

    @property
    def completion_sample_count(self) -> int:
        with self._completion_lock:
            return self._completion_sample_count

    def _completion_snapshot(self) -> tuple[float, int]:
        with self._completion_lock:
            return self._avg_completion_seconds, self._completion_sample_count

    def observe_workflow_completion(self, duration_seconds: float) -> bool:
        try:
            duration = float(duration_seconds)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(duration) or duration <= 0:
            return False
        with self._completion_lock:
            if self._completion_sample_count == 0:
                self._avg_completion_seconds = duration
            else:
                weight = self.completion_ema_alpha
                self._avg_completion_seconds = (
                    weight * duration
                    + (1.0 - weight) * self._avg_completion_seconds
                )
            self._completion_sample_count += 1
        return True

    def priority_key(
        self,
        task: Any,
        sequence: int,
        now: float | None = None,
    ) -> Tuple[Any, ...]:
        metadata = self.refresh_task_metadata(task, now)
        if metadata["starved"]:
            return (0, sequence)
        # List ordering is ascending; larger HACS score means earlier.
        return (1, -metadata["hacs_score"], sequence)

    def refresh_task_metadata(self, task: Any, now: float | None = None) -> Dict[str, Any]:
        now = time.time() if now is None else float(now)
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
        queue_entered_time = _float_value(
            getattr(task, "queue_entered_time", None),
            _float_value(getattr(task, "created_time", None), now),
        )
        queue_wait_time = max(0.0, now - queue_entered_time)
        avg_completion, completion_sample_count = self._completion_snapshot()
        avg_completion = max(avg_completion, EPSILON)

        n_desc = max(0.0, _float_value(context.get("n_desc"), 0.0))
        n_anc = max(0.0, _float_value(context.get("n_anc"), 0.0))
        remaining_value_tasks = max(
            0.0,
            _float_value(context.get("remaining_value_tasks"), 0.0),
        )

        if mode == "static":
            topological_weight = math.log2(2.0 + 2.0 * n_desc)
            phi = (
                queue_wait_time / (self.alpha * avg_completion)
            ) - remaining_value_tasks
            value_multiplier = _safe_power(self.beta, phi)
            hacs_score = (topological_weight * value_multiplier) / predicted_duration
        else:
            topological_weight = n_anc + (
                queue_wait_time / (self.alpha * avg_completion)
            )
            value_multiplier = 1.0
            phi = 0.0
            hacs_score = topological_weight / predicted_duration

        starved = queue_wait_time >= self.starvation_seconds

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
            "queue_wait_time": queue_wait_time,
            "workflow_wait_time": workflow_wait_time,
            "remaining_value_tasks": remaining_value_tasks,
            "hacs_score": hacs_score,
            "starved": starved,
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
                "queue_wait_time": queue_wait_time,
                "workflow_wait_time": workflow_wait_time,
                "remaining_value_tasks": remaining_value_tasks,
                "avg_completion_seconds": avg_completion,
                "completion_sample_count": completion_sample_count,
                "alpha": self.alpha,
                "beta": self.beta,
                "phi": phi,
                "value_multiplier": value_multiplier,
                "starvation_seconds": self.starvation_seconds,
                "starved": starved,
                "score": hacs_score,
            },
        }
        setattr(task, "scheduling_metadata", metadata)
        return metadata


def create_scheduling_strategy(strategy: str | SchedulingAlgorithm | None) -> SchedulingStrategy:
    algorithm = normalize_scheduling_algorithm(strategy)
    if algorithm is SchedulingAlgorithm.HACS:
        return HacsSchedulingStrategy(
            alpha=_configured_float(
                HACS_ALPHA_ENV,
                HACS_ALPHA,
                minimum=0.0,
            ),
            beta=_configured_float(
                HACS_BETA_ENV,
                HACS_BETA,
                minimum=1.0,
            ),
            initial_avg_completion_seconds=_configured_float(
                HACS_INITIAL_DCT_ENV,
                DEFAULT_AVG_COMPLETION_SECONDS,
                minimum=0.0,
            ),
            completion_ema_alpha=_configured_float(
                HACS_COMPLETION_EMA_ALPHA_ENV,
                HACS_COMPLETION_EMA_ALPHA,
                minimum=0.0,
            ),
            starvation_seconds=_configured_float(
                HACS_STARVATION_SECONDS_ENV,
                HACS_STARVATION_SECONDS,
                minimum=0.0,
            ),
        )
    return FcfsSchedulingStrategy()
