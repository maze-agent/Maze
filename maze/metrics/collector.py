"""Per-task metrics collector.

Uses ``contextvars`` so it works correctly with both threads and asyncio
tasks. Each Maze task runs inside ``_metrics_scope()`` (set up by the ray
remote wrapper), so user code calling ``report(...)`` ends up writing to
the right collector instance.

Outside of a task scope, ``report()`` is a no-op (no exception).
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Dict, Optional


_current_collector: contextvars.ContextVar[Optional["_Collector"]] = contextvars.ContextVar(
    "maze_metrics_collector", default=None
)


class _Collector:
    """Thread-safe-ish per-task accumulator.

    Within a single task, only one thread writes here. We keep the data
    plain for cheap merging.
    """

    __slots__ = ("data",)

    def __init__(self) -> None:
        self.data: Dict[str, Any] = {}

    def report(self, **kwargs: Any) -> None:
        """Accumulate metrics. Numeric keys add up; non-numeric overwrite.

        Special handling:
          - ``model``: also bumps ``by_model[<model>]`` aggregates
            (tokens_in / tokens_out / cost_usd / calls).
        """
        model = kwargs.get("model")
        for key, value in kwargs.items():
            if value is None:
                continue
            if key == "by_model":
                continue  # see below for nested merge
            if isinstance(value, (int, float)) and isinstance(self.data.get(key), (int, float)):
                self.data[key] = self.data[key] + value
            elif isinstance(value, (int, float)) and key not in self.data:
                self.data[key] = value
            else:
                self.data[key] = value

        # Aggregate by model
        if isinstance(model, str) and model:
            bucket = self.data.setdefault("by_model", {}).setdefault(
                model, {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "calls": 0}
            )
            for sub_key in ("tokens_in", "tokens_out", "cost_usd"):
                v = kwargs.get(sub_key)
                if isinstance(v, (int, float)):
                    bucket[sub_key] = bucket.get(sub_key, 0) + v
            bucket["calls"] = bucket.get("calls", 0) + 1

        # Merge a passed nested by_model dict
        nested = kwargs.get("by_model")
        if isinstance(nested, dict):
            outer = self.data.setdefault("by_model", {})
            for model_name, m in nested.items():
                if not isinstance(m, dict):
                    continue
                target = outer.setdefault(model_name, {})
                for k, v in m.items():
                    if isinstance(v, (int, float)) and isinstance(target.get(k), (int, float)):
                        target[k] = target[k] + v
                    elif isinstance(v, (int, float)) and k not in target:
                        target[k] = v
                    else:
                        target[k] = v


def report(**kwargs: Any) -> None:
    """Public API: record metrics for the current task.

    No-op when called outside of a task context (e.g. in unit tests or
    from the user's driver script).
    """
    c = _current_collector.get()
    if c is None:
        return
    c.report(**kwargs)


# --- Framework internals (not part of the public API) ---


@contextmanager
def _metrics_scope():
    """Context manager used by the ray remote wrapper.

    Yields the underlying collector so the caller can flush its data
    after the user function returns.
    """
    collector = _Collector()
    token = _current_collector.set(collector)
    try:
        yield collector
    finally:
        _current_collector.reset(token)


def _drain(collector: _Collector) -> Dict[str, Any]:
    """Return a fresh dict copy of the collector's data and clear it."""
    out = collector.data
    collector.data = {}
    return out
