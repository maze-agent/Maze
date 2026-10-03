"""Pure memory decisions shared by submission, deployment and OOM recovery."""

from __future__ import annotations

import ast
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any

MIB = 1024 * 1024
PEAK_MARGIN = 1.15
OOM_GROWTH = 1.25
GPU_PEAK_KEYS = (
    "gpu_memory_peak_reserved_bytes",
    "gpu_memory_peak_allocated_bytes",
    "peak_cuda_reserved_bytes",
    "peak_cuda_allocated_bytes",
    "gpu_memory_observed_bytes",
)


def positive_int(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def model_memory_mib(anchor: dict | None) -> int:
    anchor = anchor or {}
    estimates = [
        positive_int(anchor.get(k))
        for k in (
            "gpu_mem",
            "estimated_gpu_mem_mb",
            "estimated_gpu_memory_mb",
            "gpu_memory_mb",
        )
    ]
    weight = positive_int(
        anchor.get("estimated_weight_memory_bytes") or anchor.get("weight_bytes")
    )
    return max(*estimates, math.ceil(weight * 1.2 / MIB))


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ResourceContext:
    scope: str
    identity: str
    backend: str = ""
    dtype: str = ""
    features: tuple[tuple[str, int], ...] = ()

    @property
    def key(self) -> str:
        return _fingerprint(asdict(self))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> ResourceContext:
        return cls(
            str(value["scope"]),
            str(value["identity"]),
            str(value.get("backend") or ""),
            str(value.get("dtype") or ""),
            tuple((str(k), int(v)) for k, v in value.get("features", ())),
        )


def _code_dtype(code: str) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ""
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in {"dtype", "torch_dtype"}:
            if isinstance(node.value, ast.Attribute):
                return node.value.attr
            if isinstance(node.value, ast.Constant):
                return str(node.value.value)
    return ""


def context_for_task(
    *,
    code_str: str | None = None,
    code_ser: str | None = None,
    task_input: dict | None = None,
    model_anchor: dict | None = None,
) -> ResourceContext:
    anchor = model_anchor or {}
    model = anchor.get("local_model") or anchor.get("model")
    features = {}
    text_size = 0
    dtype = _code_dtype(code_str or "")
    for param in (task_input or {}).get("input_params", {}).values():
        if param.get("input_schema") != "from_user":
            continue
        name, value = str(param.get("key", "")), param.get("value")
        if name in {"dtype", "torch_dtype", "precision"} and isinstance(value, str):
            dtype = value
        if isinstance(value, str) and any(
            k in name for k in ("text", "prompt", "question", "context")
        ):
            text_size += len(value)
        elif name in {"max_tokens", "max_new_tokens", "batch_size", "sequence_length"}:
            n = positive_int(value)
            if n:
                features[name] = 1 << (n - 1).bit_length()
    if text_size:
        features["text_chars"] = 1 << (text_size - 1).bit_length()
    if model:
        return ResourceContext(
            "model_instance",
            _fingerprint(
                (anchor.get("checkpoint_fingerprint") or model, anchor.get("revision"))
            ),
            str(anchor.get("backend") or anchor.get("engine") or "vllm"),
            str(anchor.get("dtype") or anchor.get("torch_dtype") or ""),
            tuple(sorted(features.items())),
        )
    return ResourceContext(
        "task",
        _fingerprint((code_str or "", code_ser or "")),
        dtype=dtype,
        features=tuple(sorted(features.items())),
    )


@dataclass(frozen=True)
class ResourceDecision:
    declared: dict
    effective: dict
    context: ResourceContext
    sources: tuple[str, ...]
    sample_count: int = 0

    def to_dict(self) -> dict:
        return {**asdict(self), "context": self.context.to_dict()}


class ResourceLimitExceeded(ValueError):
    pass


def observed_peak_bytes(metrics: dict | None) -> int | None:
    metrics = metrics or {}
    values = [positive_int(metrics[k]) for k in GPU_PEAK_KEYS if k in metrics]
    return max(values) if values else None


def recommend_resources(
    context: ResourceContext,
    declared: dict,
    history: dict | None = None,
    *,
    model_anchor: dict | None = None,
    failure: dict | None = None,
) -> ResourceDecision:
    # Import at the public boundary; workflow normalization itself stays policy-free.
    from maze.core.workflow.resources import normalize_resources

    effective = normalize_resources(declared)
    original = dict(effective)
    history = history or {}
    sources = ["declared"]
    amount = positive_int(effective.get("gpu_mem"))
    estimate = model_memory_mib(model_anchor)
    if estimate > amount:
        amount = estimate
        sources.append("model_weights")
    peak = positive_int(history.get("success_peak_bytes"))
    observed = math.ceil(peak * PEAK_MARGIN / MIB)
    lower_bound = positive_int(history.get("oom_lower_bound_mib"))
    if observed > amount:
        amount = observed
        sources.append("observed_peak")
    if lower_bound > amount:
        amount = lower_bound
        sources.append("oom_history")
    if failure is not None:
        peak = positive_int(failure.get("peak_bytes"))
        allocated = positive_int(failure.get("allocated_bytes")) or peak
        needed = positive_int(failure.get("attempted_allocation_bytes"))
        amount = max(
            amount,
            math.ceil((allocated + needed) * PEAK_MARGIN / MIB),
            math.ceil(positive_int(effective.get("gpu_mem")) * OOM_GROWTH),
            positive_int(effective.get("gpu_mem")) + 256,
        )
        sources.append("oom_lower_bound")
    limit = positive_int(effective.get("gpu_mem_limit"))
    if limit and amount > limit:
        raise ResourceLimitExceeded(
            f"Estimated GPU memory {amount} MiB exceeds gpu_mem_limit={limit} MiB"
        )
    effective["gpu_mem"] = amount
    return ResourceDecision(
        original,
        effective,
        context,
        tuple(sources),
        positive_int(history.get("success_samples")),
    )
