from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict

import httpx


DEFAULT_PREDICTOR_URL = os.environ.get("MAZE_PREDICTOR_URL", "http://127.0.0.1:8001/predict")
DEFAULT_PREDICTOR_TIMEOUT_SECONDS = 0.25
DEFAULT_PREDICTOR_BACKOFF_SECONDS = 5.0


@dataclass(frozen=True)
class PredictionResult:
    duration_seconds: float
    source: str = "malearn"


class MaLearnPredictionClient:
    def __init__(
        self,
        url: str = DEFAULT_PREDICTOR_URL,
        timeout_seconds: float = DEFAULT_PREDICTOR_TIMEOUT_SECONDS,
        backoff_seconds: float = DEFAULT_PREDICTOR_BACKOFF_SECONDS,
    ):
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.backoff_seconds = backoff_seconds
        self._retry_after = 0.0

    def predict_duration(self, task_name: str, features: Dict[str, Any] | None) -> PredictionResult | None:
        now = time.time()
        if now < self._retry_after:
            return None

        try:
            response = httpx.post(
                self.url,
                json={"task_name": task_name, "features": features or {}},
                timeout=httpx.Timeout(self.timeout_seconds),
            )
            response.raise_for_status()
            payload = response.json()
            duration = float(payload["predict_time"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            self._retry_after = time.time() + self.backoff_seconds
            return None

        if duration <= 0:
            return None

        return PredictionResult(
            duration_seconds=duration,
            source=str(payload.get("prediction_source") or "malearn"),
        )
