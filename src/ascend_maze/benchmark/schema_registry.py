"""Access to frozen C14 version-1 JSON Schema documents."""

from __future__ import annotations

import importlib.resources
import json
from typing import Mapping, cast

from ascend_maze.benchmark.canonical import canonical_json_digest
from ascend_maze.core.errors import ExperimentValidationError

SCHEMA_FILES = (
    "artifact_manifest.v1.schema.json",
    "baseline_cleanup_result.v1.schema.json",
    "baseline_description.v1.schema.json",
    "baseline_prepare_result.v1.schema.json",
    "baseline_trial_request.v1.schema.json",
    "baseline_trial_result.v1.schema.json",
    "controller_config_overrides.v1.schema.json",
    "experiment_spec.v1.schema.json",
    "raw_files.v1.schema.json",
    "report.v1.schema.json",
    "run_manifest.v1.schema.json",
    "study_manifest.v1.schema.json",
    "study_plan.v1.schema.json",
    "study_validation.v1.schema.json",
    "trace_schedule.v1.schema.json",
    "trial_manifest.v1.schema.json",
    "trial_state.v1.schema.json",
    "trial_validity.v1.schema.json",
    "workload_dataset.v1.schema.json",
)


def load_schema(name: str) -> Mapping[str, object]:
    if name not in SCHEMA_FILES:
        raise ExperimentValidationError(f"unknown benchmark schema: {name}")
    resource = importlib.resources.files("ascend_maze.benchmark.schemas").joinpath(name)
    try:
        payload = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentValidationError(
            f"cannot load benchmark schema: {name}"
        ) from exc
    if not isinstance(payload, dict):
        raise ExperimentValidationError(f"benchmark schema is not an object: {name}")
    return cast(Mapping[str, object], payload)


def schema_digests() -> tuple[tuple[str, str], ...]:
    return tuple(
        (name, canonical_json_digest(load_schema(name))) for name in SCHEMA_FILES
    )
