from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Tuple

from maze.core.scheduler.strategy import DEFAULT_PREDICTED_DURATION_SECONDS


PREDICTION_SOURCE_DEFAULT = "task_kind_default"
PREDICTION_SOURCE_TASK_KIND_EMA = "task_kind_ema"
PREDICTION_SOURCE_TASK_CODE_EMA = "task_code_ema"


def _normalize_task_kind(task_kind: Any) -> str:
    value = str(task_kind or "cpu").strip().lower()
    return value if value in DEFAULT_PREDICTED_DURATION_SECONDS else "cpu"


def _stable_normalize(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {
            "__bytes_sha256": hashlib.sha256(value).hexdigest(),
            "length": len(value),
        }
    if isinstance(value, dict):
        return {
            str(key): _stable_normalize(value[key])
            for key in sorted(value.keys(), key=lambda item: str(item))
        }
    if isinstance(value, (list, tuple)):
        return [_stable_normalize(item) for item in value]
    if isinstance(value, set):
        normalized = [_stable_normalize(item) for item in value]
        return sorted(normalized, key=lambda item: repr(item))
    return repr(value)


def _stable_hash(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(
        _stable_normalize(payload),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def code_hash_for_task(task: Any) -> str | None:
    code_str = getattr(task, "code_str", None)
    code_ser = getattr(task, "code_ser", None)
    task_input = getattr(task, "task_input", None)

    payload = {
        "code_str": code_str,
        "code_ser": code_ser,
        "task_input": task_input,
    }
    if task_input is None and hasattr(task, "args"):
        payload["args"] = getattr(task, "args", None)
        payload["kwargs"] = getattr(task, "kwargs", None)

    if all(value is None for value in payload.values()):
        return None
    return _stable_hash(payload)


@dataclass
class EMAProfile:
    ema_duration: float = 0.0
    sample_count: int = 0
    last_observed_at: float = 0.0

    def update(self, duration_seconds: float, alpha: float, now: float | None = None) -> None:
        if self.sample_count == 0:
            self.ema_duration = duration_seconds
        else:
            self.ema_duration = alpha * duration_seconds + (1.0 - alpha) * self.ema_duration
        self.sample_count += 1
        self.last_observed_at = now or time.time()

    @property
    def confidence(self) -> float:
        return min(1.0, self.sample_count / 10.0)


@dataclass(frozen=True)
class RuntimePrediction:
    predicted_duration: float
    prediction_source: str
    confidence: float
    sample_count: int
    task_kind: str
    code_hash: str | None = None


@dataclass(frozen=True)
class RuntimeObservation:
    task_kind: str
    code_hash: str | None
    duration_seconds: float
    success: bool


class RuntimeEstimator:
    def __init__(
        self,
        *,
        defaults: Dict[str, float] | None = None,
        alpha: float = 0.2,
    ):
        self.defaults = dict(defaults or DEFAULT_PREDICTED_DURATION_SECONDS)
        self.alpha = float(alpha)
        self.kind_profiles: Dict[str, EMAProfile] = {}
        self.code_profiles: Dict[Tuple[str, str], EMAProfile] = {}

    def predict(self, task: Any) -> RuntimePrediction:
        task_kind = _normalize_task_kind(getattr(task, "task_kind", None))
        code_hash = code_hash_for_task(task)

        if code_hash is not None:
            code_profile = self.code_profiles.get((task_kind, code_hash))
            if code_profile is not None and code_profile.sample_count > 0:
                return RuntimePrediction(
                    predicted_duration=code_profile.ema_duration,
                    prediction_source=PREDICTION_SOURCE_TASK_CODE_EMA,
                    confidence=code_profile.confidence,
                    sample_count=code_profile.sample_count,
                    task_kind=task_kind,
                    code_hash=code_hash,
                )

        kind_profile = self.kind_profiles.get(task_kind)
        if kind_profile is not None and kind_profile.sample_count > 0:
            return RuntimePrediction(
                predicted_duration=kind_profile.ema_duration,
                prediction_source=PREDICTION_SOURCE_TASK_KIND_EMA,
                confidence=kind_profile.confidence,
                sample_count=kind_profile.sample_count,
                task_kind=task_kind,
                code_hash=code_hash,
            )

        return RuntimePrediction(
            predicted_duration=float(self.defaults.get(task_kind, self.defaults["cpu"])),
            prediction_source=PREDICTION_SOURCE_DEFAULT,
            confidence=0.0,
            sample_count=0,
            task_kind=task_kind,
            code_hash=code_hash,
        )

    def observe(self, observation: RuntimeObservation) -> None:
        if not observation.success:
            return
        duration_seconds = float(observation.duration_seconds)
        if duration_seconds <= 0:
            return

        task_kind = _normalize_task_kind(observation.task_kind)
        kind_profile = self.kind_profiles.setdefault(task_kind, EMAProfile())
        kind_profile.update(duration_seconds, self.alpha)

        if observation.code_hash:
            code_profile = self.code_profiles.setdefault((task_kind, observation.code_hash), EMAProfile())
            code_profile.update(duration_seconds, self.alpha)

    def observe_task(self, task: Any, duration_seconds: float, *, success: bool = True) -> None:
        self.observe(
            RuntimeObservation(
                task_kind=_normalize_task_kind(getattr(task, "task_kind", None)),
                code_hash=code_hash_for_task(task),
                duration_seconds=duration_seconds,
                success=success,
            )
        )
