"""Deterministic C14 statistics without optional numeric dependencies."""

from __future__ import annotations

from dataclasses import dataclass
import math
import random
from statistics import fmean
from typing import Callable, Iterable, Sequence


STATISTICS_ALGORITHM = "c14_statistics_v1"
BOOTSTRAP_ALGORITHM = "paired_median_mt19937_v1"


def type7_quantile(values: Iterable[float], probability: float) -> float:
    """Return the Hyndman-Fan type-7 sample quantile."""

    ordered = sorted(_finite_values(values))
    if not ordered:
        raise ValueError("quantile requires at least one finite value")
    if not 0.0 <= probability <= 1.0 or not math.isfinite(probability):
        raise ValueError("quantile probability must be within [0, 1]")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def median(values: Iterable[float]) -> float:
    return type7_quantile(values, 0.5)


@dataclass(frozen=True, slots=True)
class DistributionSummary:
    sample_count: int
    mean: float | None
    standard_deviation: float | None
    median: float | None
    mad: float | None
    minimum: float | None
    maximum: float | None
    p50: float | None
    p95: float | None
    p99: float | None

    def canonical_payload(self) -> dict[str, int | float | None]:
        return {
            "sample_count": self.sample_count,
            "mean": self.mean,
            "standard_deviation": self.standard_deviation,
            "median": self.median,
            "mad": self.mad,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "p50": self.p50,
            "p95": self.p95,
            "p99": self.p99,
        }


def summarize_distribution(values: Iterable[float]) -> DistributionSummary:
    ordered = sorted(_finite_values(values))
    count = len(ordered)
    if count == 0:
        return DistributionSummary(0, None, None, None, None, None, None, None, None, None)
    average = fmean(ordered)
    center = type7_quantile(ordered, 0.5)
    variance = (
        None
        if count < 2
        else sum((value - average) ** 2 for value in ordered) / (count - 1)
    )
    return DistributionSummary(
        sample_count=count,
        mean=average,
        standard_deviation=None if variance is None else math.sqrt(variance),
        median=center,
        mad=type7_quantile((abs(value - center) for value in ordered), 0.5),
        minimum=ordered[0],
        maximum=ordered[-1],
        p50=center,
        p95=type7_quantile(ordered, 0.95),
        p99=type7_quantile(ordered, 0.99),
    )


@dataclass(frozen=True, slots=True)
class ConfidenceInterval:
    confidence_level: float
    lower: float
    upper: float
    sidedness: str
    bootstrap_samples: int

    def canonical_payload(self) -> dict[str, int | float | str]:
        return {
            "confidence_level": self.confidence_level,
            "lower": self.lower,
            "upper": self.upper,
            "sidedness": self.sidedness,
            "bootstrap_samples": self.bootstrap_samples,
        }


def deterministic_bootstrap_interval(
    values: Sequence[float],
    *,
    seed: int,
    samples: int = 10_000,
    confidence_level: float = 0.95,
    statistic: Callable[[Iterable[float]], float] = median,
    one_sided_upper: bool = False,
) -> ConfidenceInterval:
    population = tuple(_finite_values(values))
    if not population:
        raise ValueError("bootstrap requires at least one finite value")
    if samples < 1:
        raise ValueError("bootstrap samples must be positive")
    if not 0.0 < confidence_level < 1.0:
        raise ValueError("confidence level must be within (0, 1)")
    generator = random.Random(seed)
    count = len(population)
    estimates = [
        statistic(population[generator.randrange(count)] for _ in range(count))
        for _ in range(samples)
    ]
    if one_sided_upper:
        return ConfidenceInterval(
            confidence_level=confidence_level,
            lower=min(estimates),
            upper=type7_quantile(estimates, confidence_level),
            sidedness="upper",
            bootstrap_samples=samples,
        )
    tail = (1.0 - confidence_level) / 2.0
    return ConfidenceInterval(
        confidence_level=confidence_level,
        lower=type7_quantile(estimates, tail),
        upper=type7_quantile(estimates, 1.0 - tail),
        sidedness="two_sided",
        bootstrap_samples=samples,
    )


def relative_effect_percent(
    baseline: float, candidate: float, *, higher_is_better: bool
) -> float | None:
    """Return benefit percentage, where positive always favors the candidate."""

    if baseline == 0:
        return None
    raw = (candidate - baseline) / abs(baseline) * 100.0
    return raw if higher_is_better else -raw


def degradation_percent(
    baseline: float, candidate: float, *, higher_is_better: bool
) -> float | None:
    benefit = relative_effect_percent(
        baseline, candidate, higher_is_better=higher_is_better
    )
    return None if benefit is None else -benefit


def budget_decision(
    *, point_estimate: float | None, upper_bound: float | None, limit: float
) -> str:
    if point_estimate is None or upper_bound is None:
        return "insufficient_sample"
    if math.isclose(upper_bound, limit, rel_tol=0.0, abs_tol=1e-12):
        return "borderline"
    return "pass" if upper_bound < limit else "fail"


def _finite_values(values: Iterable[float]) -> tuple[float, ...]:
    normalized: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("statistics require numeric values")
        converted = float(value)
        if not math.isfinite(converted):
            raise ValueError("statistics require finite values")
        normalized.append(converted)
    return tuple(normalized)
