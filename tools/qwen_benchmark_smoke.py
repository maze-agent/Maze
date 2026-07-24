#!/usr/bin/env python3
"""Small-sample real-Qwen smoke runner for migrated benchmark workflows.

This manual tool can start local vLLM-Ascend services and requires Ascend
hardware. It validates the system path, not benchmark accuracy:

    migrated GAIA / OpenAGI / tau-bench Workflow
      -> InMemoryRuntimeClient
      -> Ascend-Maze scheduling / C11 route lease
      -> vLLM-Ascend OpenAI-compatible service
      -> local Qwen model

The runner rewrites workflow model anchors by family so that large logical
models from the original Maze workflows, such as ``qwen3-32b`` and
``deepseek-r1-32b``, route to one local smoke model.  Text workflows use local
Qwen3-4B; visual workflows default to local Qwen2.5-VL-3B-Instruct because
this is the Qwen VL family implemented by the current vLLM-Ascend stack.
Current visual workflow ports pass OpenAI-style text+image content parts to
``ascend_maze.inference.chat()`` and are marked as ``true_multimodal``.

Typical plan-only usage from the repository root:

    PYTHONPATH="$PWD/src:$PWD:${PYTHONPATH:-}" \
      python tools/qwen_benchmark_smoke.py --plan-only --samples-per-workflow 1

Typical hardware usage:

    PYTHONPATH="$PWD/src:$PWD:${PYTHONPATH:-}" \
      /home/user2/workplace/miniconda3/envs/ascend-maze/bin/python \
      tools/qwen_benchmark_smoke.py \
        --samples-per-workflow 1 \
        --output-dir experiments/qwen_benchmark_smoke/first_real_qwen
"""

from __future__ import annotations

import argparse
import ast
import asyncio
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass, replace
from enum import Enum
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
DEFAULT_DATA_ROOT = REPO_ROOT / "data"
DEFAULT_TEXT_MODEL_PATH = Path(
    "/home/user2/workplace/model_weight/model_from_hf/Qwen3-4B"
)
DEFAULT_VISION_MODEL_PATH = Path(
    "/home/user2/workplace/model_weight/model_from_hf/Qwen2.5-VL-3B-Instruct"
)
DEFAULT_CONDA_PYTHON = Path(
    "/home/user2/workplace/miniconda3/envs/ascend-maze/bin/python"
)
DEFAULT_MODULES = (
    "torch",
    "torch_npu",
    "vllm",
    "vllm_ascend",
    "transformers",
    "ray",
    "httpx",
    "prometheus_client",
    "acl",
)
VLLM_MODULES = DEFAULT_MODULES
TRANSFORMERS_LOCAL_MODULES = (
    "torch",
    "torch_npu",
    "transformers",
    "PIL",
    "acl",
)
TEXT_MODEL_ID = "qwen3-4b-smoke"
VISION_MODEL_ID = "qwen2_5-vl-3b-smoke"

WORKFLOW_MODULES: dict[tuple[str, str], str] = {
    ("gaia", "file"): "workflows.gaia.file",
    ("gaia", "reason"): "workflows.gaia.reason",
    ("gaia", "speech"): "workflows.gaia.speech",
    ("gaia", "vision"): "workflows.gaia.vision",
    ("openagi", "document_qa"): "workflows.openagi.document_qa",
    (
        "openagi",
        "image_captioning_complex",
    ): "workflows.openagi.image_captioning_complex",
    (
        "openagi",
        "multimodal_vqa_complex",
    ): "workflows.openagi.multimodal_vqa_complex",
    (
        "openagi",
        "text_processing_multilingual",
    ): "workflows.openagi.text_processing_multilingual",
    ("tbench", "airline_book"): "workflows.tbench.airline_book",
    ("tbench", "airline_cancel"): "workflows.tbench.airline_cancel",
    ("tbench", "retail_cancel"): "workflows.tbench.retail_cancel",
    ("tbench", "retail_cancel_modify"): "workflows.tbench.retail_cancel_modify",
    ("tbench", "retail_modify"): "workflows.tbench.retail_modify",
    ("tbench", "retail_return"): "workflows.tbench.retail_return",
}

VISION_WORKFLOWS = frozenset(
    {
        ("gaia", "vision"),
        ("openagi", "image_captioning_complex"),
        ("openagi", "multimodal_vqa_complex"),
    }
)

TBENCH_QUESTION_FILES = {
    "airline_book": "airline_book_ins.py",
    "airline_cancel": "airline_cancel_ins.py",
    "retail_cancel": "cancel_ins.py",
    "retail_cancel_modify": "can_modify_ins.py",
    "retail_modify": "modify_ins.py",
    "retail_return": "return_ins.py",
}


class SmokePreflightError(RuntimeError):
    """Environment is not ready for this hardware smoke test."""


@dataclass(frozen=True, slots=True)
class SampleSpec:
    dataset: str
    workflow: str
    family: str
    dag_id: str
    query_index: int
    inputs: dict[str, object]
    source_files: tuple[str, ...]
    expected_answer: str
    vision_mode: str | None = None

    @property
    def sample_id(self) -> str:
        return f"{self.dataset}.{self.workflow}.{self.dag_id}"

    def manifest(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "dataset": self.dataset,
            "workflow": self.workflow,
            "family": self.family,
            "dag_id": self.dag_id,
            "query_index": self.query_index,
            "source_files": self.source_files,
            "expected_answer": self.expected_answer,
            "vision_mode": self.vision_mode,
        }


@dataclass(frozen=True, slots=True)
class DiscoveryFailure:
    dataset: str
    workflow: str
    dag_id: str
    query_index: int
    phase: str
    error: str


def _install_repo_path() -> None:
    for path in (str(SRC_ROOT), str(REPO_ROOT)):
        if path not in sys.path:
            sys.path.insert(0, path)
    existing = os.environ.get("PYTHONPATH", "")
    parts = [str(SRC_ROOT), str(REPO_ROOT)]
    if existing:
        parts.append(existing)
    os.environ["PYTHONPATH"] = os.pathsep.join(parts)


def _default_python() -> Path:
    if "ASCEND_MAZE_PYTHON" in os.environ:
        return Path(os.environ["ASCEND_MAZE_PYTHON"]).expanduser()
    if DEFAULT_CONDA_PYTHON.is_file():
        return DEFAULT_CONDA_PYTHON
    return Path(sys.executable)


def _jsonable(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return {
            "__bytes__": {
                "size": len(value),
                "sha256": hashlib.sha256(value).hexdigest(),
            }
        }
    if isinstance(value, bytearray):
        return _jsonable(bytes(value))
    if isinstance(value, memoryview):
        return _jsonable(value.tobytes())
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if hasattr(value, "items_tuple"):
        return {
            str(key): _jsonable(item)
            for key, item in value.items_tuple()  # type: ignore[attr-defined]
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1_000))


def _prepend_env_paths(name: str, paths: Iterable[str]) -> None:
    new_paths = tuple(str(item) for item in paths if str(item))
    if not new_paths:
        return
    existing = tuple(item for item in os.environ.get(name, "").split(os.pathsep) if item)
    merged: list[str] = []
    for item in (*new_paths, *existing):
        if item not in merged:
            merged.append(item)
    os.environ[name] = os.pathsep.join(merged)


def _data_store_metrics_snapshot(controller: object) -> dict[str, object] | None:
    data_store = getattr(controller, "data_store", None)
    snapshot = getattr(data_store, "metrics_snapshot", None)
    if callable(snapshot):
        return dict(snapshot())
    stats = getattr(data_store, "stats", None)
    if callable(stats):
        return dict(stats())
    return None


def _data_store_metrics_delta(
    before: Mapping[str, object] | None,
    after: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if before is None or after is None:
        return None
    delta: dict[str, object] = {}
    for key, value in after.items():
        old = before.get(key)
        if isinstance(value, bool) or isinstance(old, bool):
            continue
        if isinstance(value, int) and isinstance(old, int):
            delta[key] = value - old
        elif isinstance(value, (int, float)) and isinstance(old, (int, float)):
            delta[key] = round(float(value) - float(old), 3)
    return delta


def _transformers_local_records(
    inference: object,
    inference_records: object,
) -> list[dict[str, object]]:
    if not isinstance(inference_records, list):
        return []
    route_ids = {
        item.get("route_lease_id")
        for item in inference_records
        if isinstance(item, Mapping) and isinstance(item.get("route_lease_id"), str)
    }
    if not route_ids:
        return []
    catalog = getattr(inference, "catalog", None)
    adapters = getattr(catalog, "adapters", None)
    if not callable(adapters):
        return []
    records: list[dict[str, object]] = []
    for adapter in adapters():
        snapshot = getattr(adapter, "invocation_records", None)
        if not callable(snapshot):
            continue
        for item in snapshot():
            if not isinstance(item, Mapping):
                continue
            if item.get("route_lease_id") in route_ids:
                records.append(dict(item))
    return records


def _task_timing_records(
    controller: object,
    run_id: str,
    task_id_by_name: Mapping[str, str],
) -> list[dict[str, object]]:
    runtime = getattr(controller, "runtime", None)
    snapshot = getattr(runtime, "task_timing_records", None)
    if not callable(snapshot):
        return []
    task_name_by_id = {task_id: task_name for task_name, task_id in task_id_by_name.items()}
    records: list[dict[str, object]] = []
    for item in snapshot(run_id):
        if not isinstance(item, Mapping):
            continue
        record = dict(item)
        task_id = record.get("task_id")
        if isinstance(task_id, str):
            record["task_name"] = task_name_by_id.get(task_id)
        records.append(record)
    return records


def _task_timing_summary(
    records: list[dict[str, object]],
) -> dict[str, int]:
    fields = (
        "task_total_ms",
        "dispatch_prepare_ms",
        "worker_startup_ms",
        "dispatch_wait_ms",
        "input_fetch_ms",
        "callable_execute_ms",
        "chat_request_ms",
        "output_put_ms",
        "task_runtime_overhead_ms",
        "callable_minus_chat_ms",
    )
    summary: dict[str, int] = {"task_count": len(records)}
    for field in fields:
        total = 0
        for record in records:
            value = record.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                total += value
        summary[field] = total
    return summary


def _run_event_records(
    controller: object,
    run_id: str,
    task_id_by_name: Mapping[str, str],
) -> list[dict[str, object]]:
    recorder = getattr(controller, "recorder", None)
    events = getattr(recorder, "events", None)
    if not callable(events):
        return []
    task_name_by_id = {
        task_id: task_name for task_name, task_id in task_id_by_name.items()
    }
    records: list[dict[str, object]] = []
    for event in events(run_id):
        payload = _jsonable(event)
        if not isinstance(payload, Mapping):
            continue
        record = dict(payload)
        task_id = record.get("task_id")
        if isinstance(task_id, str):
            record["task_name"] = task_name_by_id.get(task_id)
        records.append(record)
    return records


def _model_request_ms(records: object) -> int:
    if not isinstance(records, list):
        return 0
    total = 0
    for item in records:
        if isinstance(item, Mapping):
            duration = item.get("duration_ms")
            if isinstance(duration, int) and not isinstance(duration, bool):
                total += duration
    return total


def emit(name: str, value: object) -> None:
    if isinstance(value, str):
        print(f"{name} {value}", flush=True)
    else:
        print(
            f"{name} "
            + json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True),
            flush=True,
        )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def _append_jsonl(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True))
        handle.write("\n")


def _module_version(module_name: str) -> str:
    module = importlib.import_module(module_name)
    version = getattr(module, "__version__", None)
    if version is not None:
        return str(version)
    try:
        from importlib import metadata

        return metadata.version(module_name.replace("_", "-"))
    except Exception:
        return "unknown"


def check_current_python_modules(
    modules: tuple[str, ...] = DEFAULT_MODULES,
) -> dict[str, dict[str, str]]:
    results: dict[str, dict[str, str]] = {}
    missing: list[str] = []
    for name in modules:
        try:
            version = _module_version(name)
        except Exception as exc:
            results[name] = {
                "status": "missing_or_import_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
            missing.append(name)
        else:
            results[name] = {"status": "ok", "version": version}
    if missing:
        raise SmokePreflightError(
            "required Python modules are unavailable: " + ", ".join(missing)
        )
    return results


def check_service_python_modules(
    python_executable: Path,
    modules: tuple[str, ...] = DEFAULT_MODULES,
) -> dict[str, dict[str, str]]:
    code = """
import importlib
import json
import sys

modules = sys.argv[1:]
results = {}
for name in modules:
    try:
        module = importlib.import_module(name)
        version = getattr(module, "__version__", "unknown")
    except Exception as exc:
        results[name] = {
            "status": "missing_or_import_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    else:
        results[name] = {"status": "ok", "version": str(version)}
print("MODULE_CHECK_JSON " + json.dumps(results, sort_keys=True))
"""
    completed = subprocess.run(
        [str(python_executable), "-c", code, *modules],
        cwd=str(REPO_ROOT),
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        check=False,
    )
    payload: dict[str, dict[str, str]] | None = None
    for line in completed.stdout.splitlines():
        if line.startswith("MODULE_CHECK_JSON "):
            payload = json.loads(line.removeprefix("MODULE_CHECK_JSON "))
    if completed.returncode != 0 or payload is None:
        raise SmokePreflightError(
            "service Python module check failed: "
            f"returncode={completed.returncode}, stderr={completed.stderr[-1000:]}"
        )
    missing = [
        name
        for name, result in payload.items()
        if result.get("status") != "ok"
    ]
    if missing:
        raise SmokePreflightError(
            "service Python modules are unavailable: " + ", ".join(sorted(missing))
        )
    return payload


def validate_model_artifact(model_path: Path) -> dict[str, object]:
    """Return a small artifact manifest or raise for incomplete local weights."""
    if not model_path.is_dir():
        raise SmokePreflightError(f"model path does not exist: {model_path}")
    config_path = model_path / "config.json"
    if not config_path.is_file():
        raise SmokePreflightError(f"model config is missing: {config_path}")

    weight_files = sorted(
        path.name
        for pattern in ("*.safetensors", "*.bin", "*.pt")
        for path in model_path.glob(pattern)
        if path.is_file()
    )
    weight_indexes = sorted(
        path.name
        for pattern in ("*.safetensors.index.json", "*.bin.index.json")
        for path in model_path.glob(pattern)
        if path.is_file()
    )
    if not weight_files and not weight_indexes:
        raise SmokePreflightError(
            f"model weights are missing under: {model_path}"
        )

    return {
        "path": str(model_path),
        "config_json": str(config_path),
        "weight_file_count": len(weight_files),
        "weight_index_count": len(weight_indexes),
        "sample_weight_files": weight_files[:5],
        "sample_weight_indexes": weight_indexes[:5],
    }


def _git_revision() -> str:
    try:
        value = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "0" * 40
    return value or "0" * 40


def _tail_logs(log_dir: Path, lines: int = 120) -> dict[str, str]:
    result: dict[str, str] = {}
    if not log_dir.exists():
        return result
    for path in sorted(log_dir.glob("*.log")):
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            result[str(path)] = f"<cannot read: {exc}>"
        else:
            result[str(path)] = "\n".join(content[-lines:])
    return result


def _residual_vllm_processes(
    model_paths: Iterable[Path],
    ports: tuple[int, ...],
    *,
    owned_process_group_ids: Iterable[int] | None = None,
) -> list[str]:
    completed = subprocess.run(
        ["ps", "-eo", "pid,ppid,pgid,stat,cmd"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    resolved_model_paths = tuple(str(path) for path in model_paths)
    owned_groups = (
        None
        if owned_process_group_ids is None
        else frozenset(int(item) for item in owned_process_group_ids)
    )
    residual: list[str] = []
    for line in completed.stdout.splitlines():
        if "grep" in line or "rg " in line:
            continue
        columns = line.strip().split(maxsplit=4)
        if len(columns) < 5:
            continue
        try:
            pid = int(columns[0])
            process_group_id = int(columns[2])
        except ValueError:
            continue
        is_owned = owned_groups is None or (
            pid in owned_groups or process_group_id in owned_groups
        )
        if is_owned and "vllm.entrypoints.openai.api_server" in line and (
            any(model_path in line for model_path in resolved_model_paths)
            or any(f"--port {port}" in line for port in ports)
        ):
            residual.append(line.strip())
    return residual


def _device_summary(device_adapter: Any) -> list[dict[str, object]]:
    return [
        {
            "physical_device_id": device.physical_device_id,
            "chip_type": device.chip_type,
            "health": device.health,
            "used_hbm_mb": device.used_hbm_mb,
            "total_hbm_mb": device.total_hbm_mb,
            "processes": [
                {"pid": process.pid, "hbm_mb": process.hbm_mb}
                for process in device.processes
            ],
        }
        for device in device_adapter.devices()
    ]


def _processes_on_device(
    devices: list[dict[str, object]],
    device_id: str,
) -> list[dict[str, int]]:
    for device in devices:
        if device["physical_device_id"] == device_id:
            return list(device["processes"])  # type: ignore[arg-type]
    return []


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSONL: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: record must be an object")
            records.append(value)
    return records


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _read_bytes(path: Path, *, max_inline_file_bytes: int) -> bytes:
    size = path.stat().st_size
    if size > max_inline_file_bytes:
        raise ValueError(
            f"supplementary file exceeds max_inline_file_bytes: "
            f"{path} size={size} limit={max_inline_file_bytes}"
        )
    return path.read_bytes()


def _gaia_file_smoke_summary(*, path: Path, content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    return (
        "GAIA file smoke summary; full file content is intentionally not "
        "inlined for this system-path run.\n"
        f"file_name: {path.name}\n"
        f"extension: {path.suffix.lower()}\n"
        f"size_bytes: {len(content)}\n"
        f"sha256: {digest}\n"
        f"source_path: {path}\n"
        "scope: validate Ascend-Maze -> vLLM-Ascend -> workflow execution; "
        "not GAIA file-answer accuracy."
    )


def _safe_answer(path: Path) -> str:
    if not path.exists():
        return ""
    return _read_text(path).strip()


def _workflow_family(dataset: str, workflow: str) -> str:
    return "vision" if (dataset, workflow) in VISION_WORKFLOWS else "text"


def _workflow_selected(
    dataset: str,
    workflow: str,
    selected: set[str],
) -> bool:
    if not selected:
        return True
    return (
        dataset in selected
        or workflow in selected
        or f"{dataset}.{workflow}" in selected
    )


def _family_selected(family: str, selected: set[str]) -> bool:
    return not selected or family in selected


def discover_samples(
    *,
    data_root: Path,
    datasets: set[str],
    workflows: set[str],
    families: set[str],
    samples_per_workflow: int,
    sample_offset: int,
    max_inline_file_bytes: int,
    tbench_smoke_overrides: bool = True,
    gaia_file_smoke_summary: bool = True,
) -> tuple[list[SampleSpec], list[DiscoveryFailure]]:
    failures: list[DiscoveryFailure] = []
    candidates: list[SampleSpec] = []
    if not datasets or "gaia" in datasets:
        discovered, failed = _discover_gaia_samples(
            data_root,
            workflows=workflows,
            families=families,
            max_inline_file_bytes=max_inline_file_bytes,
            gaia_file_smoke_summary=gaia_file_smoke_summary,
        )
        candidates.extend(discovered)
        failures.extend(failed)
    if not datasets or "openagi" in datasets:
        discovered, failed = _discover_openagi_samples(
            data_root,
            workflows=workflows,
            families=families,
            max_inline_file_bytes=max_inline_file_bytes,
        )
        candidates.extend(discovered)
        failures.extend(failed)
    if not datasets or "tbench" in datasets:
        discovered, failed = _discover_tbench_samples(
            data_root,
            workflows=workflows,
            families=families,
            tbench_smoke_overrides=tbench_smoke_overrides,
        )
        candidates.extend(discovered)
        failures.extend(failed)

    grouped: dict[tuple[str, str], list[SampleSpec]] = defaultdict(list)
    for sample in candidates:
        grouped[(sample.dataset, sample.workflow)].append(sample)

    selected_samples: list[SampleSpec] = []
    for key in sorted(grouped):
        items = sorted(grouped[key], key=lambda item: item.query_index)
        selected_samples.extend(
            items[sample_offset : sample_offset + samples_per_workflow]
        )
    return selected_samples, failures


def _discover_gaia_samples(
    data_root: Path,
    *,
    workflows: set[str],
    families: set[str],
    max_inline_file_bytes: int,
    gaia_file_smoke_summary: bool,
) -> tuple[list[SampleSpec], list[DiscoveryFailure]]:
    dataset = "gaia"
    root = data_root / "gaia"
    query_path = root / "gaia_query.jsonl"
    metadata = _load_gaia_metadata(root / "2023")
    samples: list[SampleSpec] = []
    failures: list[DiscoveryFailure] = []
    for query_index, record in enumerate(_read_jsonl(query_path)):
        workflow = str(record.get("dag_type", ""))
        dag_id = str(record.get("dag_id", ""))
        family = _workflow_family(dataset, workflow)
        if (
            (dataset, workflow) not in WORKFLOW_MODULES
            or not _workflow_selected(dataset, workflow, workflows)
            or not _family_selected(family, families)
        ):
            continue
        try:
            meta = metadata[dag_id]
            question = str(meta.get("Question", ""))
            answer = str(meta.get("Final answer", ""))
            split_dir = Path(str(meta["_split_dir"]))
            raw_files = record.get("dag_supplementary_files", ())
            if not isinstance(raw_files, list):
                raw_files = []
            supplementary_files: dict[str, bytes] = {}
            source_files: list[str] = []
            for item in raw_files:
                rel = str(item)
                path = split_dir / rel
                content = _read_bytes(
                    path,
                    max_inline_file_bytes=max_inline_file_bytes,
                )
                if workflow == "file" and gaia_file_smoke_summary:
                    supplementary_files[Path(rel).name] = _gaia_file_smoke_summary(
                        path=path,
                        content=content,
                    )
                else:
                    supplementary_files[Path(rel).name] = content
                source_files.append(str(path))
            sample_metadata = {
                "expected_answer": answer,
                "gaia_level": meta.get("Level", ""),
                "gaia_file_name": meta.get("file_name", ""),
                "smoke_runner": "qwen_benchmark_smoke",
            }
            if workflow == "file" and gaia_file_smoke_summary and source_files:
                sample_metadata["gaia_file_smoke_mode"] = "file_summary_not_full_inline"
            samples.append(
                SampleSpec(
                    dataset=dataset,
                    workflow=workflow,
                    family=family,
                    dag_id=dag_id,
                    query_index=query_index,
                    inputs={
                        "dag_id": dag_id,
                        "question": question,
                        "answer": answer,
                        "supplementary_files": supplementary_files,
                        "metadata": sample_metadata,
                    },
                    source_files=tuple(source_files),
                    expected_answer=answer,
                    vision_mode=(
                        "true_multimodal" if family == "vision" else None
                    ),
                )
            )
        except Exception as exc:
            failures.append(
                DiscoveryFailure(
                    dataset=dataset,
                    workflow=workflow,
                    dag_id=dag_id,
                    query_index=query_index,
                    phase="sample_load",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return samples, failures


def _load_gaia_metadata(root: Path) -> dict[str, dict[str, object]]:
    by_id: dict[str, dict[str, object]] = {}
    for split in ("validation", "test"):
        split_dir = root / split
        path = split_dir / "metadata.jsonl"
        if not path.exists():
            continue
        for record in _read_jsonl(path):
            dag_id = str(record.get("task_id", ""))
            if not dag_id:
                continue
            enriched = dict(record)
            enriched["_split_dir"] = str(split_dir)
            by_id[dag_id] = enriched
    return by_id


def _discover_openagi_samples(
    data_root: Path,
    *,
    workflows: set[str],
    families: set[str],
    max_inline_file_bytes: int,
) -> tuple[list[SampleSpec], list[DiscoveryFailure]]:
    dataset = "openagi"
    root = data_root / "openagi"
    query_path = root / "openagi_query.jsonl"
    samples: list[SampleSpec] = []
    failures: list[DiscoveryFailure] = []
    for query_index, record in enumerate(_read_jsonl(query_path)):
        workflow = str(record.get("dag_type", ""))
        dag_id = str(record.get("dag_id", ""))
        family = _workflow_family(dataset, workflow)
        if (
            (dataset, workflow) not in WORKFLOW_MODULES
            or not _workflow_selected(dataset, workflow, workflows)
            or not _family_selected(family, families)
        ):
            continue
        try:
            sample_dir = root / workflow / dag_id
            inputs_dir = sample_dir / "inputs"
            if not inputs_dir.is_dir():
                raise FileNotFoundError(f"missing OpenAGI input directory: {inputs_dir}")
            raw_files = record.get("dag_supplementary_files", ())
            if not isinstance(raw_files, list):
                raw_files = []
            question = _openagi_question(sample_dir, workflow)
            supplementary_files: dict[str, bytes] = {}
            source_files: list[str] = []
            for item in raw_files:
                rel = str(item)
                if rel in {"question.txt", "questions.txt"}:
                    continue
                path = inputs_dir / rel
                content = _read_bytes(
                    path,
                    max_inline_file_bytes=max_inline_file_bytes,
                )
                supplementary_files[rel] = content
                source_files.append(str(path))
            answer = _safe_answer(sample_dir / "outputs" / "answers.txt")
            if not answer:
                answer = _safe_answer(sample_dir / "outputs" / "labels.txt")
            metadata = {
                "expected_answer": answer,
                "instruction": _safe_answer(inputs_dir / "question.txt"),
                "smoke_runner": "qwen_benchmark_smoke",
            }
            samples.append(
                SampleSpec(
                    dataset=dataset,
                    workflow=workflow,
                    family=family,
                    dag_id=dag_id,
                    query_index=query_index,
                    inputs={
                        "dag_id": dag_id,
                        "question": question,
                        "answer": answer,
                        "supplementary_files": supplementary_files,
                        "metadata": metadata,
                    },
                    source_files=tuple(source_files),
                    expected_answer=answer,
                    vision_mode=(
                        "true_multimodal" if family == "vision" else None
                    ),
                )
            )
        except Exception as exc:
            failures.append(
                DiscoveryFailure(
                    dataset=dataset,
                    workflow=workflow,
                    dag_id=dag_id,
                    query_index=query_index,
                    phase="sample_load",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return samples, failures


def _openagi_question(sample_dir: Path, workflow: str) -> str:
    inputs_dir = sample_dir / "inputs"
    if workflow == "document_qa" and (inputs_dir / "questions.txt").is_file():
        return _read_text(inputs_dir / "questions.txt")
    return _read_text(inputs_dir / "question.txt")


def _discover_tbench_samples(
    data_root: Path,
    *,
    workflows: set[str],
    families: set[str],
    tbench_smoke_overrides: bool,
) -> tuple[list[SampleSpec], list[DiscoveryFailure]]:
    dataset = "tbench"
    root = data_root / "tbench"
    query_path = root / "tbench_query.jsonl"
    instructions = _load_tbench_instructions(root / "question")
    backend = _load_tbench_backend(root / "data")
    samples: list[SampleSpec] = []
    failures: list[DiscoveryFailure] = []
    for query_index, record in enumerate(_read_jsonl(query_path)):
        workflow = str(record.get("dag_type", ""))
        dag_id = str(record.get("dag_id", ""))
        family = _workflow_family(dataset, workflow)
        if (
            (dataset, workflow) not in WORKFLOW_MODULES
            or not _workflow_selected(dataset, workflow, workflows)
            or not _family_selected(family, families)
        ):
            continue
        try:
            instruction_record = instructions[workflow][dag_id]
            domain = "airline" if workflow.startswith("airline_") else "retail"
            supplementary_files = {
                name: value
                for name, value in backend[domain].items()
            }
            metadata = {
                "user_id": instruction_record.get("user_id", ""),
                "smoke_runner": "qwen_benchmark_smoke",
            }
            if tbench_smoke_overrides:
                metadata.update(
                    _tbench_smoke_overrides(
                        workflow=workflow,
                        instruction_record=instruction_record,
                    )
                )
            samples.append(
                SampleSpec(
                    dataset=dataset,
                    workflow=workflow,
                    family=family,
                    dag_id=dag_id,
                    query_index=query_index,
                    inputs={
                        "dag_id": dag_id,
                        "question": str(instruction_record["instruction"]),
                        "answer": "",
                        "supplementary_files": supplementary_files,
                        "metadata": metadata,
                    },
                    source_files=tuple(
                        str(root / "data" / domain / name)
                        for name in sorted(supplementary_files)
                    ),
                    expected_answer="",
                    vision_mode=None,
                )
            )
        except Exception as exc:
            failures.append(
                DiscoveryFailure(
                    dataset=dataset,
                    workflow=workflow,
                    dag_id=dag_id,
                    query_index=query_index,
                    phase="sample_load",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return samples, failures


def _load_tbench_backend(root: Path) -> dict[str, dict[str, object]]:
    return {
        "airline": {
            "flights.json": json.loads(_read_text(root / "airline" / "flights.json")),
            "users.json": json.loads(_read_text(root / "airline" / "users.json")),
            "reservations.json": json.loads(
                _read_text(root / "airline" / "reservations.json")
            ),
        },
        "retail": {
            "products.json": json.loads(_read_text(root / "retail" / "products.json")),
            "users.json": json.loads(_read_text(root / "retail" / "users.json")),
            "orders.json": json.loads(_read_text(root / "retail" / "orders.json")),
        },
    }


def _load_tbench_instructions(
    question_root: Path,
) -> dict[str, dict[str, dict[str, object]]]:
    result: dict[str, dict[str, dict[str, object]]] = {}
    for workflow, filename in TBENCH_QUESTION_FILES.items():
        result[workflow] = _parse_tbench_task_file(question_root / filename)
    return result


def _parse_tbench_task_file(path: Path) -> dict[str, dict[str, object]]:
    tree = ast.parse(_read_text(path), filename=str(path))
    tasks: dict[str, dict[str, object]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "Task":
            continue
        values: dict[str, str] = {}
        for keyword in node.keywords:
            if keyword.arg not in {"uuid", "instruction", "user_id"}:
                continue
            if isinstance(keyword.value, ast.Constant) and isinstance(
                keyword.value.value, str
            ):
                values[keyword.arg] = keyword.value.value
        uuid = values.get("uuid")
        instruction = values.get("instruction")
        if uuid and instruction:
            task_record: dict[str, object] = dict(values)
            actions = _parse_tbench_actions(node)
            if actions:
                task_record["actions"] = actions
            tasks[uuid] = task_record
    return tasks


def _parse_tbench_actions(task_call: ast.Call) -> list[dict[str, object]]:
    for keyword in task_call.keywords:
        if keyword.arg != "actions":
            continue
        if not isinstance(keyword.value, ast.List):
            return []
        actions: list[dict[str, object]] = []
        for item in keyword.value.elts:
            if not isinstance(item, ast.Call):
                continue
            if not isinstance(item.func, ast.Name) or item.func.id != "Action":
                continue
            action_name = ""
            kwargs: dict[str, object] = {}
            for action_keyword in item.keywords:
                if action_keyword.arg == "name":
                    if (
                        isinstance(action_keyword.value, ast.Constant)
                        and isinstance(action_keyword.value.value, str)
                    ):
                        action_name = action_keyword.value.value
                elif action_keyword.arg == "kwargs":
                    parsed_kwargs = _literal_from_ast(action_keyword.value)
                    if isinstance(parsed_kwargs, dict):
                        kwargs = {
                            str(key): value for key, value in parsed_kwargs.items()
                        }
            if action_name:
                actions.append({"name": action_name, "kwargs": kwargs})
        return actions
    return []


def _literal_from_ast(node: ast.AST) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List):
        return [_literal_from_ast(item) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return [_literal_from_ast(item) for item in node.elts]
    if isinstance(node, ast.Dict):
        result: dict[object, object] = {}
        for key_node, value_node in zip(node.keys, node.values):
            if key_node is None:
                continue
            result[_literal_from_ast(key_node)] = _literal_from_ast(value_node)
        return result
    return None


def _tbench_smoke_overrides(
    *,
    workflow: str,
    instruction_record: dict[str, object],
) -> dict[str, object]:
    """Build explicit smoke-only overrides from stable tau-bench task data.

    The workflow NPU tasks still call Qwen/vLLM before reading these metadata
    fields.  The overrides only prevent small-model JSON-formatting misses from
    blocking backend workflow execution in a system-path smoke run.
    """

    instruction = str(instruction_record.get("instruction", ""))
    user_id = str(instruction_record.get("user_id", ""))
    actions = [
        item
        for item in instruction_record.get("actions", [])
        if isinstance(item, dict)
    ]
    overrides: dict[str, object] = {}

    if workflow == "airline_book":
        request = _airline_book_smoke_request(instruction, user_id)
        if request:
            overrides["booking_extract_output_override"] = _json_text(request)
            overrides["itinerary_output_override"] = "[]"
    elif workflow == "airline_cancel":
        request = _airline_cancel_smoke_request(instruction, user_id)
        if request:
            overrides["cancel_extract_output_override"] = _json_text(request)
            overrides["flight_selection_output_override"] = _json_text(
                {"outbound_flight_number": "", "return_flight_number": ""}
            )
    elif workflow == "retail_cancel":
        cancellations = _retail_cancel_actions(actions)
        if cancellations:
            overrides["llm_output_override"] = _json_text(cancellations)
    elif workflow == "retail_cancel_modify":
        request = _retail_cancel_modify_actions(actions)
        if request:
            overrides["llm_output_override"] = _json_text(request)
    elif workflow == "retail_modify":
        request = _retail_modify_actions(actions)
        if request:
            overrides["llm_output_override"] = _json_text(request)
    elif workflow == "retail_return":
        request = _retail_return_actions(actions)
        if request:
            overrides["llm_output_override"] = _json_text(request)

    if overrides:
        overrides["smoke_override_mode"] = "tbench_ground_truth_actions_or_regex"
    return overrides


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _retail_cancel_actions(actions: list[dict[str, object]]) -> list[dict[str, object]]:
    cancellations: list[dict[str, object]] = []
    for action in actions:
        if action.get("name") != "cancel_pending_order":
            continue
        kwargs = action.get("kwargs")
        if not isinstance(kwargs, dict):
            continue
        order_id = kwargs.get("order_id")
        if not isinstance(order_id, str) or not order_id:
            continue
        reason = kwargs.get("reason", "")
        cancellations.append(
            {
                "order_id": order_id,
                "reason": reason if isinstance(reason, str) else "",
            }
        )
    return cancellations


def _retail_cancel_modify_actions(
    actions: list[dict[str, object]],
) -> dict[str, object]:
    request: dict[str, object] = {}
    cancellations = _retail_cancel_actions(actions)
    if cancellations:
        request["cancellation"] = cancellations
    modifications: list[dict[str, object]] = []
    for action in actions:
        if action.get("name") != "modify_pending_order_items":
            continue
        kwargs = action.get("kwargs")
        if not isinstance(kwargs, dict):
            continue
        operation = _cancel_modify_item_operation(kwargs)
        if operation:
            modifications.append(operation)
    if modifications:
        request["modification"] = modifications
    return request


def _retail_modify_actions(actions: list[dict[str, object]]) -> dict[str, object]:
    request: dict[str, object] = {}
    for action in actions:
        kwargs = action.get("kwargs")
        if not isinstance(kwargs, dict):
            continue
        if action.get("name") == "modify_pending_order_payment":
            order_id = kwargs.get("order_id")
            payment_method_id = kwargs.get("payment_method_id")
            if isinstance(order_id, str) and isinstance(payment_method_id, str):
                request["payment_modification"] = {
                    "order_id": order_id,
                    "payment_method_id": payment_method_id,
                }
        elif action.get("name") == "modify_pending_order_address":
            order_id = kwargs.get("order_id")
            address = kwargs.get("address")
            if isinstance(order_id, str) and isinstance(address, dict):
                request["order_address_modification"] = {
                    "order_id": order_id,
                    **{str(key): value for key, value in address.items()},
                }
        elif action.get("name") == "modify_user_address":
            user_id = kwargs.get("user_id")
            address = kwargs.get("address")
            if isinstance(user_id, str) and isinstance(address, dict):
                request["user_address_modification"] = {
                    "user_id": user_id,
                    **{str(key): value for key, value in address.items()},
                }
        elif action.get("name") == "modify_pending_order_items":
            item_modification = _retail_modify_item_modification(kwargs)
            if item_modification:
                request["item_modification"] = item_modification
    return request


def _retail_modify_item_modification(kwargs: dict[str, object]) -> dict[str, object]:
    order_id = kwargs.get("order_id")
    item_ids = kwargs.get("item_ids")
    new_item_ids = kwargs.get("new_item_ids")
    payment_method_id = kwargs.get("payment_method_id", "")
    if (
        not isinstance(order_id, str)
        or not isinstance(item_ids, list)
        or not isinstance(new_item_ids, list)
    ):
        return {}
    return {
        "order_id": order_id,
        "items_to_modify": [{"item_id": str(item)} for item in item_ids],
        "new_items_spec": [{"item_id": str(item)} for item in new_item_ids],
        "payment_method_id": payment_method_id
        if isinstance(payment_method_id, str)
        else "",
    }


def _cancel_modify_item_operation(kwargs: dict[str, object]) -> dict[str, object]:
    order_id = kwargs.get("order_id")
    item_ids = kwargs.get("item_ids")
    new_item_ids = kwargs.get("new_item_ids")
    payment_method_id = kwargs.get("payment_method_id", "")
    if (
        not isinstance(order_id, str)
        or not isinstance(item_ids, list)
        or not isinstance(new_item_ids, list)
    ):
        return {}
    operation: dict[str, object] = {
        "order_id": order_id,
        "item_to_modify": [{"item_id": str(item)} for item in item_ids],
        "new_item_spec": [{"item_id": str(item)} for item in new_item_ids],
    }
    if isinstance(payment_method_id, str) and payment_method_id:
        operation["payment_method_id"] = payment_method_id
    return operation


def _retail_return_actions(actions: list[dict[str, object]]) -> dict[str, object]:
    for action in actions:
        if action.get("name") != "return_delivered_order_items":
            continue
        kwargs = action.get("kwargs")
        if not isinstance(kwargs, dict):
            continue
        order_id = kwargs.get("order_id")
        payment_method_id = kwargs.get("payment_method_id", "")
        if not isinstance(order_id, str) or not order_id:
            continue
        return {
            "order_id": order_id,
            "items": ["all"],
            "payment_method_id": payment_method_id
            if isinstance(payment_method_id, str)
            else "",
        }
    return {}


def _airline_book_smoke_request(instruction: str, user_id: str) -> dict[str, object]:
    route = re.search(r"\bfrom\s+([A-Z]{3})\s+to\s+([A-Z]{3})\b", instruction)
    date = _first_iso_date(instruction) or _month_day_date(instruction)
    cabin = _airline_cabin(instruction)
    if not user_id or route is None or not date or not cabin:
        return {}
    return {
        "user_id": user_id,
        "origin": route.group(1),
        "destination": route.group(2),
        "date": date,
        "cabin": cabin,
        "baggages": _baggage_count(instruction),
        "insurance": _insurance_choice(instruction),
        "constraints": [],
        "num_passengers": _passenger_count(instruction),
        "passengers": [],
        "flight_type": "round_trip" if "round trip" in instruction.lower() else "one_way",
    }


def _airline_cancel_smoke_request(instruction: str, user_id: str) -> dict[str, object]:
    route = re.search(r"\bfrom\s+([A-Z]{3})\s+to\s+([A-Z]{3})\b", instruction)
    reservation = re.search(r"\breservation\s+([A-Z0-9]{5,8})\b", instruction)
    departure = _labeled_month_day_date(instruction, "departure date")
    departure = departure or _first_iso_date(instruction) or _month_day_date(instruction)
    return_date = _labeled_month_day_date(instruction, "return date")
    cabin = _airline_cabin(instruction)
    if (
        not user_id
        or route is None
        or reservation is None
        or not departure
        or not cabin
    ):
        return {}
    return {
        "user_id": user_id,
        "cancel_reservation_id": reservation.group(1),
        "origin": route.group(1),
        "destination": route.group(2),
        "departure_date": departure,
        "return_date": return_date,
        "cabin": cabin,
        "baggages": _baggage_count(instruction),
        "insurance": _insurance_choice(instruction),
        "payment_preference": "",
        "constraints": [],
        "num_passengers": _passenger_count(instruction),
        "passengers": [],
    }


def _first_iso_date(text: str) -> str:
    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    return "" if match is None else match.group(1)


def _labeled_month_day_date(text: str, label: str) -> str:
    pattern = rf"{re.escape(label)}\s+([A-Z][a-z]+)\s+(\d{{1,2}})"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if match is None:
        return ""
    return _format_month_day(match.group(1), match.group(2))


def _month_day_date(text: str) -> str:
    match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+(\d{1,2})\b",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        return ""
    return _format_month_day(match.group(1), match.group(2))


def _format_month_day(month_name: str, day_text: str) -> str:
    months = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
    month = months.get(month_name.lower())
    if month is None:
        return ""
    return f"2024-{month:02d}-{int(day_text):02d}"


def _airline_cabin(text: str) -> str:
    lowered = text.lower()
    if "basic economy" in lowered:
        return "basic_economy"
    if "business" in lowered:
        return "business"
    if "economy" in lowered:
        return "economy"
    return ""


def _baggage_count(text: str) -> int:
    match = re.search(r"\b(\d+)\s+bags?gages?\b|\b(\d+)\s+bags?\b", text, re.IGNORECASE)
    if match is not None:
        value = match.group(1) or match.group(2)
        return int(value)
    if "free baggage allowance" in text.lower():
        return 1
    return 0


def _insurance_choice(text: str) -> str:
    lowered = text.lower()
    if "no insurance" in lowered or "insurance needed" in lowered and "no " in lowered:
        return "no"
    if "travel insurance" in lowered or "want insurance" in lowered:
        return "yes"
    return "no"


def _passenger_count(text: str) -> int:
    match = re.search(r"\b(\d+)\s+passengers?\b", text, re.IGNORECASE)
    if match is None:
        return 1
    return max(1, int(match.group(1)))


def _artifact_revision(path: Path) -> str:
    digests: list[tuple[str, str]] = []
    for name in (
        "config.json",
        "generation_config.json",
        "model.safetensors.index.json",
        "tokenizer_config.json",
    ):
        candidate = path / name
        if candidate.is_file():
            digests.append(
                (name, hashlib.sha256(candidate.read_bytes()).hexdigest())
            )
    if not digests:
        return hashlib.sha256(str(path).encode("utf-8")).hexdigest()
    return hashlib.sha256(
        json.dumps(digests, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _rewrite_model_anchors(workflow: object, target_model_id: str) -> dict[str, str]:
    from ascend_maze.compiler.ir import ModelAnchorSpec

    rewrite: dict[str, str] = {}
    for draft in getattr(workflow, "_draft_tasks"):
        anchor = getattr(draft, "model_anchor")
        if anchor is None:
            continue
        rewrite[anchor.model] = target_model_id
        draft.model_anchor = ModelAnchorSpec(model=target_model_id, mode=anchor.mode)
    return dict(sorted(rewrite.items()))


def _build_workflow(dataset: str, workflow: str, target_model_id: str) -> tuple[object, dict[str, str]]:
    module_name = WORKFLOW_MODULES[(dataset, workflow)]
    module = importlib.import_module(module_name)
    built = module.build()
    return built, _rewrite_model_anchors(built, target_model_id)


class _PortLeaseWrapper:
    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self._leases: dict[str, Any] = {}

    async def acquire(
        self,
        *,
        node_id: str,
        boot_id: str,
        owner_instance_id: str,
        generation: int,
    ) -> Any:
        lease = await self.manager.acquire_port(
            node_id=node_id,
            boot_id=boot_id,
            owner_instance_id=owner_instance_id,
            generation=generation,
        )
        self._leases[lease.port_lease_id] = lease
        return lease

    async def release(self, lease: Any) -> bool:
        released = await self.manager.release_port(lease)
        if released:
            self._leases.pop(lease.port_lease_id, None)
        return released

    def active_count(self) -> int:
        return len(self._leases)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run migrated GAIA/OpenAGI/tau-bench workflows through local "
            "Qwen Transformers or vLLM-Ascend inference."
        )
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--text-model-path", type=Path, default=DEFAULT_TEXT_MODEL_PATH)
    parser.add_argument(
        "--vision-model-path",
        type=Path,
        default=DEFAULT_VISION_MODEL_PATH,
    )
    parser.add_argument("--python-executable", type=Path, default=_default_python())
    parser.add_argument("--device-id", default="0")
    parser.add_argument(
        "--inference-backend",
        choices=("vllm", "transformers"),
        default="vllm",
        help=(
            "Inference backend for model tasks. 'transformers' is a cold-load "
            "text/vision path that loads the model inside every chat() call."
        ),
    )
    parser.add_argument(
        "--dataset",
        action="append",
        choices=("gaia", "openagi", "tbench"),
        default=[],
        help="Dataset to include. Repeatable. Default: all datasets.",
    )
    parser.add_argument(
        "--workflow",
        action="append",
        default=[],
        help=(
            "Workflow selector, e.g. gaia.reason, document_qa, or tbench. "
            "Repeatable. Default: all migrated workflows."
        ),
    )
    parser.add_argument(
        "--family",
        action="append",
        choices=("text", "vision"),
        default=[],
        help="Model family to include. Repeatable. Default: text and vision.",
    )
    parser.add_argument("--samples-per-workflow", type=int, default=1)
    parser.add_argument("--sample-offset", type=int, default=0)
    parser.add_argument("--max-inline-file-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--text-max-model-len", type=int, default=10240)
    parser.add_argument("--vision-max-model-len", type=int, default=12288)
    parser.add_argument(
        "--text-dtype",
        choices=("bfloat16", "float16"),
        default="bfloat16",
    )
    parser.add_argument(
        "--vision-dtype",
        choices=("bfloat16", "float16"),
        default="bfloat16",
    )
    parser.add_argument("--text-gpu-memory-utilization", type=float, default=0.5)
    parser.add_argument("--vision-gpu-memory-utilization", type=float, default=0.7)
    parser.add_argument("--max-num-seqs", type=int, default=1)
    parser.add_argument(
        "--text-max-num-batched-tokens",
        type=int,
        default=None,
        help="Optional vLLM --max-num-batched-tokens for the text model service.",
    )
    parser.add_argument(
        "--vision-max-num-batched-tokens",
        type=int,
        default=4096,
        help=(
            "Optional vLLM --max-num-batched-tokens for the visual model service. "
            "Default leaves room for migrated visual workflows that request "
            "1024 output tokens."
        ),
    )
    parser.add_argument("--startup-timeout-ms", type=int, default=600_000)
    parser.add_argument("--request-timeout-ms", type=int, default=180_000)
    parser.add_argument("--run-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--hbm-recovery-tolerance-mb", type=int, default=1024)
    parser.add_argument("--first-port", type=int, default=31240)
    parser.add_argument("--last-port", type=int, default=31320)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--vision-trust-remote-code",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--text-trust-remote-code",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Default: experiments/qwen_benchmark_smoke/run-<timestamp>",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Load samples and write plan.json without checking hardware.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Run sample discovery plus hardware/model preflight without launching vLLM.",
    )
    parser.add_argument(
        "--allow-busy-device",
        action="store_true",
        help="Do not fail preflight when the selected NPU already has processes.",
    )
    parser.add_argument(
        "--allow-sample-failures",
        action="store_true",
        help="Return zero if the runner completed but one or more samples failed.",
    )
    parser.add_argument(
        "--tbench-smoke-overrides",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Inject explicit tau-bench metadata overrides derived from task "
            "actions/regex so smoke runs still exercise Qwen/vLLM but do not "
            "depend on small-model JSON formatting quality."
        ),
    )
    parser.add_argument(
        "--gaia-file-smoke-summary",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "For GAIA file smoke samples, pass a deterministic file summary "
            "instead of inlining large supplementary file contents into the "
            "model prompt."
        ),
    )
    parser.add_argument(
        "--unsafe-no-deepcopy-large-values",
        action="store_true",
        help=(
            "Removed InMemory-only option. Formal benchmark execution always "
            "uses RayRuntimeBackend and RayDataStore."
        ),
    )
    return parser.parse_args()


def _validate_args(args: argparse.Namespace) -> None:
    if args.samples_per_workflow < 1:
        raise SystemExit("--samples-per-workflow must be positive")
    if args.sample_offset < 0:
        raise SystemExit("--sample-offset must be non-negative")
    if args.max_inline_file_bytes < 1:
        raise SystemExit("--max-inline-file-bytes must be positive")
    if args.text_max_model_len < 1 or args.vision_max_model_len < 1:
        raise SystemExit("max model lengths must be positive")
    for name in ("text_gpu_memory_utilization", "vision_gpu_memory_utilization"):
        value = getattr(args, name)
        if not 0 < value <= 0.9:
            raise SystemExit(f"--{name.replace('_', '-')} must be within (0, 0.9]")
    if args.max_num_seqs < 1:
        raise SystemExit("--max-num-seqs must be positive")
    for name in ("text_max_num_batched_tokens", "vision_max_num_batched_tokens"):
        value = getattr(args, name)
        if value is not None and value < 1:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    if args.first_port > args.last_port:
        raise SystemExit("--first-port cannot exceed --last-port")
    if args.run_timeout_seconds <= 0:
        raise SystemExit("--run-timeout-seconds must be positive")
    if args.unsafe_no_deepcopy_large_values:
        raise SystemExit(
            "--unsafe-no-deepcopy-large-values is unavailable on the formal Ray path"
        )


async def _run_family(
    *,
    args: argparse.Namespace,
    family: str,
    samples: list[SampleSpec],
    output_dir: Path,
    environment: Any,
    preloads: Mapping[str, str],
    runtime_paths: tuple[str, ...],
    device_adapter: Any,
) -> dict[str, object]:
    from ascend_maze.ascend.contracts import AscendCorrectnessConfig
    from ascend_maze.ascend.discovery import build_ascend_node_capacity
    import ray

    from ascend_maze.control import InMemoryRuntimeClient
    from ascend_maze.control.node_rpc import NodeAgent, NodeAgentIdentity
    from ascend_maze.control.ray_controller import RayHostController
    from ascend_maze.control.service_process import NodeServiceProcessManager
    from ascend_maze.core.canonical import canonical_digest
    from ascend_maze.inference import (
        InferenceCoordinator,
        InMemoryPortLeaseManager,
        ModelCatalog,
        ModelSpec,
    )
    from ascend_maze.inference.adapters.transformers_local import (
        TransformersLocalInferenceEngineAdapter,
    )
    from ascend_maze.inference.adapters.vllm_ascend import (
        VllmAscendInferenceEngineAdapter,
    )
    from ascend_maze.placement import PlacementManager

    model_path = (
        args.text_model_path if family == "text" else args.vision_model_path
    ).expanduser().resolve(strict=False)
    target_model_id = TEXT_MODEL_ID if family == "text" else VISION_MODEL_ID
    node_id = f"local_{family}_benchmark_smoke"
    boot_id = f"boot_{family}_{int(time.time())}"
    log_dir = output_dir / "logs" / family
    records_path = output_dir / f"{family}_records.jsonl"
    failures_path = output_dir / f"{family}_failures.jsonl"
    launched_services: list[dict[str, object]] = []

    class _LoggingProcessManager(NodeServiceProcessManager):
        async def launch(self, request: Any, lease: Any) -> Any:
            payload = {
                "argv": list(request.argv),
                "working_directory": request.working_directory,
                "environment": dict(request.environment.items_tuple()),
                "node_id": lease.node_id,
                "boot_id": lease.boot_id,
                "npu_device_id": lease.npu_device_id,
                "log_path": str(
                    self.log_directory
                    / f"{request.instance_id}.{request.generation}.log"
                ),
            }
            launched_services.append(payload)
            handle = await super().launch(request, lease)
            payload["process_id"] = handle.process_id
            payload["process_group_id"] = handle.process_id
            emit("SERVICE_LAUNCH_JSON", payload)
            return handle

    correctness = AscendCorrectnessConfig(
        task_slots_total=1,
        allow_colocation=False,
        max_tasks_per_worker=1,
        standby_min_idle=0,
        npu_system_reserved_hbm_mb=4096,
        npu_hbm_headroom_mb=1024,
        host_mem_headroom_mb=1024,
        io_slots_total=8,
        hbm_recovery_tolerance_mb=args.hbm_recovery_tolerance_mb,
    )
    node = build_ascend_node_capacity(
        node_id=node_id,
        boot_id=boot_id,
        node_ip="127.0.0.1",
        adapter=device_adapter,
        environment=environment,
        config=correctness,
    )
    selected_npus = tuple(
        npu for npu in node.npus if npu.device_id == str(args.device_id)
    )
    if not selected_npus:
        raise SmokePreflightError(f"NPU {args.device_id} is not visible through DCMI")
    node = replace(node, npus=selected_npus)

    is_vision = family == "vision"
    trust_remote_code = (
        bool(args.vision_trust_remote_code)
        if is_vision
        else bool(args.text_trust_remote_code)
    )
    if args.inference_backend == "transformers":
        launch_options = _transformers_local_launch_options(
            device_id=str(args.device_id),
            request_timeout_ms=int(args.request_timeout_ms),
            runtime_paths=runtime_paths,
            trust_remote_code=trust_remote_code,
            is_vision=is_vision,
        )
    else:
        launch_options = {
            "block_size": 128,
            "enable_prefix_caching": False,
            "enforce_eager": True,
            "gpu_memory_utilization": (
                float(args.vision_gpu_memory_utilization)
                if is_vision
                else float(args.text_gpu_memory_utilization)
            ),
            "log_level": str(args.log_level),
            "max_num_seqs": int(args.max_num_seqs),
        }
        max_num_batched_tokens = (
            args.vision_max_num_batched_tokens
            if is_vision
            else args.text_max_num_batched_tokens
        )
        if max_num_batched_tokens is not None:
            launch_options["max_num_batched_tokens"] = int(max_num_batched_tokens)
        if trust_remote_code:
            launch_options["trust_remote_code"] = True

    calibrated_allow_colocation = args.inference_backend == "transformers"
    if is_vision and calibrated_allow_colocation:
        weight_hbm_mb = 8_192
        runtime_hbm_mb = 3_072
        kv_cache_hbm_mb = 512
        instance_hbm_mb = 11_776
        max_model_len = int(args.vision_max_model_len)
        dtype = str(args.vision_dtype)
    elif is_vision:
        if args.inference_backend == "vllm":
            launch_options["generation_config"] = "vllm"
            launch_options["qwen2_5_vl_cpu_unique_consecutive_workaround"] = True
        weight_hbm_mb = 18_000
        runtime_hbm_mb = 8_000
        kv_cache_hbm_mb = 20_000
        instance_hbm_mb = 46_000
        max_model_len = int(args.vision_max_model_len)
        dtype = str(args.vision_dtype)
    elif calibrated_allow_colocation:
        weight_hbm_mb = 8_192
        runtime_hbm_mb = 4_096
        kv_cache_hbm_mb = 1_536
        instance_hbm_mb = 13_824
        max_model_len = int(args.text_max_model_len)
        dtype = str(args.text_dtype)
    else:
        weight_hbm_mb = 7_500
        runtime_hbm_mb = 4_000
        kv_cache_hbm_mb = 22_000
        instance_hbm_mb = 36_000
        max_model_len = int(args.text_max_model_len)
        dtype = str(args.text_dtype)

    backend_name = (
        "transformers_local"
        if args.inference_backend == "transformers"
        else "vllm_ascend"
    )

    spec = ModelSpec(
        model_id=target_model_id,
        catalog_revision=f"benchmark-smoke-{family}-{_artifact_revision(model_path)[:12]}",
        artifact_path=str(model_path),
        tokenizer_path=str(model_path),
        artifact_revision=_artifact_revision(model_path),
        backend=backend_name,
        dtype=dtype,
        quantization=None,
        tensor_parallel_size=1,
        max_model_len=max_model_len,
        instance_cpu_num=4,
        instance_host_mem_mb=16_384,
        weight_hbm_mb=weight_hbm_mb,
        runtime_hbm_mb=runtime_hbm_mb,
        kv_cache_hbm_mb=kv_cache_hbm_mb,
        instance_hbm_mb=instance_hbm_mb,
        npu_slots=1,
        allow_colocation=calibrated_allow_colocation,
        request_capacity=1,
        required_capabilities=(backend_name,),
        environment_fingerprint=environment.environment_fingerprint,
        launch_options=launch_options,
        warmup_request={
            "messages": [{"role": "user", "content": "Reply with exactly: ready"}],
            "max_tokens": 8,
            "temperature": 0.0,
        },
        min_replicas=0,
        max_replicas=1,
        target_route_utilization=1.0,
        scale_up_pending_threshold=1,
        scale_up_sustain_ms=0,
        scale_down_idle_ms=600_000,
        scale_cooldown_ms=600_000,
        max_parallel_starts=1,
        startup_timeout_ms=int(args.startup_timeout_ms),
        drain_timeout_ms=120_000,
    )

    service_manager: Any = None
    controller: Any = None
    ray_started_here = False
    family_summary: dict[str, object] = {
        "family": family,
        "runtime_backend": "ray",
        "inference_backend": str(args.inference_backend),
        "target_model_id": target_model_id,
        "model_path": str(model_path),
        "sample_count": len(samples),
        "records_path": str(records_path),
        "failures_path": str(failures_path),
        "service_launches": launched_services,
        "status": "not_started",
    }
    succeeded = 0
    failed = 0
    cleanup_errors: list[str] = []

    try:
        if args.inference_backend == "transformers":
            adapter = TransformersLocalInferenceEngineAdapter()
            service_backend = adapter
            port_leases = InMemoryPortLeaseManager(
                first_port=int(args.first_port),
                last_port=int(args.last_port),
            )
        else:
            service_manager = _LoggingProcessManager(
                node_id=node_id,
                boot_id=boot_id,
                device_monitor=device_adapter,
                allowed_executables=(str(args.python_executable),),
                log_directory=log_dir,
                first_port=int(args.first_port),
                last_port=int(args.last_port),
                port_bind_host="127.0.0.1",
                hbm_recovery_tolerance_mb=args.hbm_recovery_tolerance_mb,
                poll_interval_ms=500,
            )
            service_backend = service_manager
            port_leases = _PortLeaseWrapper(service_manager)
            adapter = VllmAscendInferenceEngineAdapter(
                process_backend=service_manager,
                python_executable=str(args.python_executable),
                endpoint_host_resolver=lambda lease: "127.0.0.1",
                bind_host="127.0.0.1",
                runtime_library_preloads=preloads,
                runtime_library_paths=runtime_paths,
                request_timeout_ms=int(args.request_timeout_ms),
                probe_timeout_ms=int(args.startup_timeout_ms),
                probe_interval_ms=1_000,
            )
        placement = PlacementManager(
            host_mem_headroom_mb=correctness.host_mem_headroom_mb,
            npu_hbm_headroom_mb=correctness.npu_hbm_headroom_mb,
            required_environment_fingerprint=environment.environment_fingerprint,
        )
        catalog = ModelCatalog(
            (spec,),
            adapters={backend_name: adapter},
            environment_capabilities=("ascend", backend_name),
            max_single_npu_hbm_mb=max(
                npu.total_hbm_mb - npu.system_reserved_hbm_mb
                for npu in node.npus
            ),
        )
        inference = InferenceCoordinator(
            catalog=catalog,
            placement=placement,
            service_backend=service_backend,
            port_leases=port_leases,
            reconcile_interval_ms=500,
        )
        config_fingerprint = canonical_digest(
            {
                "profile": "qwen-benchmark-smoke",
                "family": family,
                "runtime_backend": "ray",
                "inference_backend": str(args.inference_backend),
                "environment_fingerprint": environment.environment_fingerprint,
                "model_catalog_digest": catalog.content_digest,
                "device": str(args.device_id),
                "launch_options": dict(spec.launch_options.items_tuple()),
                "runtime_preloads": dict(preloads.items()),
                "runtime_library_paths": runtime_paths,
            }
        )
        ray_namespace = (
            f"ascend-maze-qwen-{family}-{os.getpid()}-{int(time.time() * 1000)}"
        )
        if not ray.is_initialized():
            ray.init(
                namespace=ray_namespace,
                include_dashboard=False,
                log_to_driver=False,
            )
            ray_started_here = True
        else:
            ray_namespace = ray.get_runtime_context().namespace
        head_agent = NodeAgent(
            identity=NodeAgentIdentity(
                cluster_id=f"qwen_benchmark_{family}",
                node_id=node.node_id,
                boot_id=node.boot_id,
                ray_node_id=ray.get_runtime_context().get_node_id(),
                agent_generation=f"agent_{family}_{int(time.time() * 1000)}",
                environment_fingerprint=environment.environment_fingerprint,
                producer_id=f"node_agent:{node.node_id}:benchmark",
            ),
            authorization_token=b"qwen-benchmark-smoke",
            heartbeat_interval_ms=250,
        )
        controller = RayHostController(
            cluster_id=f"qwen_benchmark_{family}",
            authorization_token=b"qwen-benchmark-smoke",
            ray_namespace=ray_namespace,
            config_fingerprint=config_fingerprint,
            environment_fingerprint=environment.environment_fingerprint,
            build_revision=_git_revision(),
            node_capacities=(node,),
            placement=placement,
            inference=inference,
            dispatch_timeout_ms=int(args.startup_timeout_ms),
            shutdown_drain_timeout_ms=5_000,
            shutdown_cleanup_timeout_ms=120_000,
            head_node_agent=head_agent,
        )
        await controller.start()
        client = InMemoryRuntimeClient(controller)

        for sample in samples:
            record = await _run_one_sample(
                controller=controller,
                client=client,
                inference=inference,
                sample=sample,
                target_model_id=target_model_id,
                run_timeout_seconds=float(args.run_timeout_seconds),
            )
            _append_jsonl(records_path, record)
            if record["status"] == "succeeded":
                succeeded += 1
            else:
                failed += 1
                _append_jsonl(failures_path, record)
    finally:
        if controller is not None:
            try:
                shutdown = await controller.shutdown(force=True, drain_timeout_ms=0)
                family_summary["controller_shutdown"] = _jsonable(shutdown)
                emit("CONTROLLER_SHUTDOWN_JSON", shutdown)
            except Exception as exc:
                cleanup_errors.append(
                    f"controller_shutdown:{type(exc).__name__}:{exc}"
                )
                emit("CONTROLLER_SHUTDOWN_ERROR", traceback.format_exc())
        elif service_manager is not None:
            try:
                await service_manager.close(timeout_ms=120_000)
            except Exception as exc:
                cleanup_errors.append(
                    f"service_manager_close:{type(exc).__name__}:{exc}"
                )
                emit("SERVICE_MANAGER_CLOSE_ERROR", traceback.format_exc())
        if ray_started_here:
            ray.shutdown()

    family_summary.update(
        {
            "status": "completed",
            "succeeded": succeeded,
            "failed": failed,
            "cleanup_errors": cleanup_errors,
            "service_log_tails": _tail_logs(log_dir) if failed else {},
        }
    )
    _write_json(output_dir / f"{family}_summary.json", family_summary)
    return family_summary


def _transformers_local_launch_options(
    *,
    device_id: str,
    request_timeout_ms: int,
    runtime_paths: tuple[str, ...],
    trust_remote_code: bool,
    is_vision: bool,
) -> dict[str, object]:
    options: dict[str, object] = {
        "device_id": device_id,
        "enable_thinking": False,
        "generation_method": "manual_greedy",
        "model_kind": "vision_language" if is_vision else "text",
        "request_timeout_ms": request_timeout_ms,
        "runtime_library_paths": runtime_paths,
        "trust_remote_code": trust_remote_code,
    }
    if is_vision:
        options["qwen2_5_vl_cpu_unique_consecutive_workaround"] = True
    return options


async def _run_one_sample(
    *,
    controller: Any,
    client: Any,
    inference: Any,
    sample: SampleSpec,
    target_model_id: str,
    run_timeout_seconds: float,
) -> dict[str, object]:
    sample_started = time.perf_counter()
    started_ms = int(time.time() * 1000)
    data_store_metrics_start = _data_store_metrics_snapshot(controller)
    latency_metrics: dict[str, object] = {}
    record: dict[str, object] = {
        "schema_version": 1,
        "sample": sample.manifest(),
        "target_model_id": target_model_id,
        "started_at_ms": started_ms,
        "status": "not_started",
    }
    run_id: str | None = None
    destroyed = False
    client_e2e_started: float | None = None
    client_e2e_finished: float | None = None
    try:
        stage_started = time.perf_counter()
        workflow, model_aliases = _build_workflow(
            sample.dataset,
            sample.workflow,
            target_model_id,
        )
        compiled = workflow.compile()
        task_id_by_name = {
            task.task_name: task_id
            for task_id, task in compiled.tasks.items_tuple()
        }
        latency_metrics["prepare_ms"] = _elapsed_ms(stage_started)
        record["workflow_fingerprint"] = compiled.workflow_fingerprint
        record["model_aliases"] = model_aliases
        record["task_id_by_name"] = task_id_by_name

        submission_id = (
            "qwen-smoke-"
            + hashlib.sha256(
                f"{sample.sample_id}:{target_model_id}:{started_ms}".encode()
            ).hexdigest()[:20]
        )
        emit(
            "SAMPLE_START_JSON",
            {
                "sample_id": sample.sample_id,
                "family": sample.family,
                "target_model_id": target_model_id,
                "submission_id": submission_id,
            },
        )
        client_e2e_started = time.perf_counter()
        stage_started = time.perf_counter()
        prepare_submit_started = time.perf_counter()
        prepared = client.prepare_submission(
            workflow,
            inputs=sample.inputs,
            submission_id=submission_id,
            session_key=f"{submission_id}-session",
            run_deadline_ms=int(run_timeout_seconds * 1_000),
        )
        latency_metrics["client_prepare_submission_ms"] = _elapsed_ms(
            prepare_submit_started
        )
        latency_metrics["client_prepare_trace"] = _jsonable(
            getattr(client, "last_prepare_trace", {})
        )
        controller_submit_started = time.perf_counter()
        outcome = await client.submit_prepared(prepared)
        latency_metrics["controller_submit_roundtrip_ms"] = _elapsed_ms(
            controller_submit_started
        )
        latency_metrics["controller_submit_trace"] = _jsonable(
            getattr(controller, "last_submit_trace", {})
        )
        latency_metrics["submit_ms"] = _elapsed_ms(stage_started)
        record["submission"] = {
            "submission_id": outcome.submission_id,
            "state": outcome.state.value,
            "run_id": outcome.run_id,
            "payload_hash": outcome.submission_payload_hash,
            "replayed": outcome.replayed,
            "error": outcome.error,
        }
        if outcome.run_id is None:
            raise RuntimeError("submission did not produce a run_id")
        run_id = outcome.run_id
        stage_started = time.perf_counter()
        snapshot = await _wait_terminal_or_cancel(
            controller=controller,
            inference=inference,
            run_id=run_id,
            timeout_seconds=run_timeout_seconds,
        )
        latency_metrics["wait_terminal_ms"] = _elapsed_ms(stage_started)
        terminal = _run_snapshot_payload(snapshot)
        record["run_terminal"] = terminal
        if snapshot.status.value == "succeeded":
            record["status"] = "succeeded"
            stage_started = time.perf_counter()
            record["exit_task_results"] = {
                compiled.tasks[task_id].task_name: controller.result(run_id, task_id)
                for task_id in compiled.exit_tasks
            }
            latency_metrics["final_result_fetch_ms"] = _elapsed_ms(stage_started)
        else:
            record["status"] = f"failed:{snapshot.status.value}"
            record["failure"] = terminal.get("failure")
        client_e2e_finished = time.perf_counter()

        # Detailed evidence is intentionally collected outside client E2E. The
        # user-visible request is complete once the exit-task results return.
        record["inference_records"] = [
            asdict(item)
            for item in inference.request_records()
            if item.run_id == run_id
        ]
        record["task_timing_records"] = _task_timing_records(
            controller,
            run_id,
            task_id_by_name,
        )
        record["transformers_local_records"] = _transformers_local_records(
            inference,
            record["inference_records"],
        )
        for timing in record["task_timing_records"]:
            metrics = timing.get("inference_metrics")
            if isinstance(metrics, list):
                record["transformers_local_records"].extend(
                    dict(item) for item in metrics if isinstance(item, Mapping)
                )
        record["task_timing_summary"] = _task_timing_summary(
            record["task_timing_records"]
        )
        latency_metrics["model_request_ms"] = _model_request_ms(
            record["inference_records"]
        )
        if snapshot.status.value == "succeeded":
            stage_started = time.perf_counter()
            record["task_results"] = {
                task_name: controller.result(run_id, task_id)
                for task_name, task_id in sorted(task_id_by_name.items())
            }
            latency_metrics["result_fetch_ms"] = _elapsed_ms(stage_started)
        record["run_events"] = _run_event_records(
            controller,
            run_id,
            task_id_by_name,
        )
        stage_started = time.perf_counter()
        destroy = await controller.destroy_run(run_id, force=True)
        destroyed = True
        latency_metrics["destroy_ms"] = _elapsed_ms(stage_started)
        record["destroy_result"] = _jsonable(destroy)
    except Exception as exc:
        record["status"] = "unexpected_exception"
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["traceback"] = traceback.format_exc()
        emit("SAMPLE_EXCEPTION_TRACEBACK", record["traceback"])
    finally:
        cleanup_started = time.perf_counter()
        if run_id is not None and not destroyed:
            try:
                await controller.cancel_run(run_id, reason="sample_cleanup")
            except Exception as exc:
                record.setdefault("cleanup_errors", []).append(
                    f"cancel_run:{type(exc).__name__}:{exc}"
                )
            try:
                destroy = await controller.destroy_run(run_id, force=True)
                record["destroy_result"] = _jsonable(destroy)
            except Exception as exc:
                record.setdefault("cleanup_errors", []).append(
                    f"destroy_run:{type(exc).__name__}:{exc}"
                )
        if "destroy_ms" not in latency_metrics and cleanup_started is not None:
            latency_metrics["cleanup_ms"] = _elapsed_ms(cleanup_started)
        record["finished_at_ms"] = int(time.time() * 1000)
        record["duration_ms"] = int(record["finished_at_ms"]) - started_ms
        latency_metrics["total_sample_ms"] = _elapsed_ms(sample_started)
        if client_e2e_started is not None:
            end = client_e2e_finished or time.perf_counter()
            client_e2e_ms = max(0, int((end - client_e2e_started) * 1_000))
            latency_metrics["client_e2e_ms"] = client_e2e_ms
            model_ms = latency_metrics.get("model_request_ms")
            if isinstance(model_ms, int):
                latency_metrics["client_e2e_minus_model_ms"] = (
                    client_e2e_ms - model_ms
                )
        record["latency_metrics"] = latency_metrics
        data_store_metrics_end = _data_store_metrics_snapshot(controller)
        record["data_store_metrics"] = {
            "start": data_store_metrics_start,
            "end": data_store_metrics_end,
            "delta": _data_store_metrics_delta(
                data_store_metrics_start,
                data_store_metrics_end,
            ),
        }
        emit(
            "SAMPLE_RESULT_JSON",
            {
                "sample_id": sample.sample_id,
                "status": record["status"],
                "duration_ms": record["duration_ms"],
            },
        )
    return record


async def _wait_terminal_or_cancel(
    *,
    controller: Any,
    inference: Any,
    run_id: str,
    timeout_seconds: float,
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    last_state: object = None
    snapshot = controller.snapshot(run_id)
    while not snapshot.terminal:
        states = [
            (
                instance.instance_id,
                instance.generation,
                instance.model_id,
                instance.state.value,
                instance.failure_reason,
            )
            for instance in inference.model_instances()
        ]
        if states != last_state:
            emit("MODEL_INSTANCE_STATES_JSON", states)
            last_state = states
        failed_or_stopped = [
            instance
            for instance in inference.model_instances()
            if instance.failure_reason
            or instance.state.value in {"failed", "stopped"}
        ]
        if failed_or_stopped:
            emit(
                "MODEL_INSTANCE_FAILURE_JSON",
                [
                    {
                        "instance_id": instance.instance_id,
                        "generation": instance.generation,
                        "model_id": instance.model_id,
                        "state": instance.state.value,
                        "failure_reason": instance.failure_reason,
                    }
                    for instance in failed_or_stopped
                ],
            )
            return await controller.cancel_run(
                run_id,
                reason="smoke_model_instance_failed",
            )
        if time.monotonic() >= deadline:
            return await controller.cancel_run(run_id, reason="smoke_timeout")
        await asyncio.sleep(1.0)
        snapshot = controller.snapshot(run_id)
    return snapshot


def _run_snapshot_payload(snapshot: Any) -> dict[str, object]:
    return {
        "run_id": snapshot.run_id,
        "status": snapshot.status.value,
        "failure": None
        if snapshot.failure is None
        else {
            "error_code": snapshot.failure.error_code,
            "message": snapshot.failure.message,
            "phase": snapshot.failure.execution_phase,
            "origin": snapshot.failure.origin,
            "category": snapshot.failure.category,
        },
        "tasks": [
            {
                "task_id": task.task_id,
                "status": task.status.value,
                "pending_reason": task.pending_reason,
                "last_error": None
                if task.last_error is None
                else {
                    "error_code": task.last_error.error_code,
                    "message": task.last_error.message,
                    "phase": task.last_error.execution_phase,
                    "origin": task.last_error.origin,
                    "category": task.last_error.category,
                },
            }
            for task in snapshot.task_states
        ],
    }


async def run_smoke(args: argparse.Namespace) -> int:
    _install_repo_path()
    output_dir = (
        args.output_dir.expanduser().resolve(strict=False)
        if args.output_dir is not None
        else REPO_ROOT
        / "experiments"
        / "qwen_benchmark_smoke"
        / f"run-{int(time.time())}"
    )
    args.data_root = args.data_root.expanduser().resolve(strict=False)
    args.text_model_path = args.text_model_path.expanduser().resolve(strict=False)
    args.vision_model_path = args.vision_model_path.expanduser().resolve(strict=False)
    args.python_executable = args.python_executable.expanduser().resolve(strict=False)

    selected_datasets = set(args.dataset)
    selected_workflows = set(args.workflow)
    selected_families = set(args.family)
    samples, discovery_failures = discover_samples(
        data_root=args.data_root,
        datasets=selected_datasets,
        workflows=selected_workflows,
        families=selected_families,
        samples_per_workflow=int(args.samples_per_workflow),
        sample_offset=int(args.sample_offset),
        max_inline_file_bytes=int(args.max_inline_file_bytes),
        tbench_smoke_overrides=bool(args.tbench_smoke_overrides),
        gaia_file_smoke_summary=bool(args.gaia_file_smoke_summary),
    )
    plan = {
        "schema_version": 1,
        "objective": "real_qwen_benchmark_workflow_smoke",
        "data_root": str(args.data_root),
        "output_dir": str(output_dir),
        "inference_backend": str(args.inference_backend),
        "runtime_backend": "ray",
        "samples_per_workflow": int(args.samples_per_workflow),
        "sample_offset": int(args.sample_offset),
        "tbench_smoke_overrides": bool(args.tbench_smoke_overrides),
        "gaia_file_smoke_summary": bool(args.gaia_file_smoke_summary),
        "samples": [sample.manifest() for sample in samples],
        "discovery_failures": discovery_failures,
        "text_model": {
            "model_id": TEXT_MODEL_ID,
            "path": str(args.text_model_path),
            "dtype": str(args.text_dtype),
            "max_model_len": int(args.text_max_model_len),
            "max_num_batched_tokens": args.text_max_num_batched_tokens,
        },
        "vision_model": {
            "model_id": VISION_MODEL_ID,
            "path": str(args.vision_model_path),
            "dtype": str(args.vision_dtype),
            "max_model_len": int(args.vision_max_model_len),
            "max_num_batched_tokens": args.vision_max_num_batched_tokens,
            "vision_mode": "true_multimodal",
        },
    }
    _write_json(output_dir / "plan.json", plan)
    emit("SMOKE_PLAN_PATH", str(output_dir / "plan.json"))
    emit(
        "SMOKE_PLAN_JSON",
        {
            "sample_count": len(samples),
            "discovery_failure_count": len(discovery_failures),
            "families": sorted({sample.family for sample in samples}),
        },
    )
    for failure in discovery_failures:
        _append_jsonl(output_dir / "discovery_failures.jsonl", failure)

    def preflight_failed(message: str, *, extra: Mapping[str, object] | None = None) -> int:
        payload: dict[str, object] = {
            "schema_version": 1,
            "result": "preflight_failed",
            "message": message,
            "sample_count": len(samples),
            "discovery_failure_count": len(discovery_failures),
            "output_dir": str(output_dir),
        }
        if extra:
            payload.update(dict(extra))
        _write_json(output_dir / "summary.json", payload)
        _append_jsonl(
            output_dir / "preflight_failures.jsonl",
            {
                "event": "preflight_failed",
                "message": message,
                "sample_count": len(samples),
                "discovery_failure_count": len(discovery_failures),
            },
        )
        emit("SMOKE_PREFLIGHT_FAILED", message)
        return 2

    if args.plan_only:
        _write_json(
            output_dir / "summary.json",
            {
                "schema_version": 1,
                "result": "plan_only_succeeded",
                "sample_count": len(samples),
                "discovery_failure_count": len(discovery_failures),
                "output_dir": str(output_dir),
            },
        )
        emit("SMOKE_RESULT", "plan_only_succeeded")
        return 0

    if not samples:
        return preflight_failed("sample discovery produced no runnable samples")
    families_present = {sample.family for sample in samples}
    if not args.python_executable.is_file():
        return preflight_failed(
            f"python executable does not exist: {args.python_executable}"
        )

    required_model_paths: list[Path] = []
    if "text" in families_present:
        required_model_paths.append(args.text_model_path)
    if "vision" in families_present:
        required_model_paths.append(args.vision_model_path)
    model_artifacts: list[dict[str, object]] = []
    for model_path in required_model_paths:
        try:
            model_artifacts.append(validate_model_artifact(model_path))
        except SmokePreflightError as exc:
            return preflight_failed(str(exc))

    try:
        from ascend_maze.ascend.dcmi import DcmiDeviceAdapter
        from ascend_maze.ascend.discovery import (
            discover_aicpu_runtime_library_paths,
            discover_ascend_environment,
            discover_atb_runtime_library_preloads,
        )
    except Exception as exc:
        message = (
            "failed to import Ascend-Maze hardware modules: "
            f"{type(exc).__name__}: {exc}"
        )
        preflight_failed(
            message,
            extra={"traceback": traceback.format_exc()},
        )
        emit("SMOKE_EXCEPTION_TRACEBACK", traceback.format_exc())
        return 2

    try:
        module_set = (
            TRANSFORMERS_LOCAL_MODULES
            if args.inference_backend == "transformers"
            else VLLM_MODULES
        )
        current_modules = check_current_python_modules(module_set)
        service_modules = check_service_python_modules(args.python_executable, module_set)
        emit("CURRENT_PYTHON_MODULES_JSON", current_modules)
        emit("SERVICE_PYTHON_MODULES_JSON", service_modules)
        device_adapter = DcmiDeviceAdapter()
        initial_devices = _device_summary(device_adapter)
        emit("ASCEND_DEVICES_JSON", initial_devices)
        selected_processes = _processes_on_device(initial_devices, str(args.device_id))
        if selected_processes and not args.allow_busy_device:
            raise SmokePreflightError(
                f"NPU {args.device_id} already has processes: {selected_processes}"
            )
        environment = discover_ascend_environment(device_adapter)
        preloads = dict(discover_atb_runtime_library_preloads())
        runtime_paths = discover_aicpu_runtime_library_paths()
        if args.inference_backend == "transformers":
            _prepend_env_paths("LD_LIBRARY_PATH", runtime_paths)
        emit("ASCEND_ENVIRONMENT_FINGERPRINT", environment.environment_fingerprint)
        emit(
            "ASCEND_ENVIRONMENT_VERSIONS_JSON",
            dict(environment.versions.items_tuple()),
        )
        emit("ATB_RUNTIME_PRELOADS_JSON", preloads)
        emit("AICPU_RUNTIME_LIBRARY_PATHS_JSON", runtime_paths)
        if args.inference_backend == "vllm" and not preloads:
            raise SmokePreflightError("ATB runtime preload libmki.so was not found")
        if args.inference_backend == "vllm" and not runtime_paths:
            raise SmokePreflightError("AICPU runtime library paths were not found")
    except SmokePreflightError as exc:
        return preflight_failed(
            str(exc),
            extra={
                "model_artifacts": model_artifacts,
                "current_python_modules": locals().get("current_modules"),
                "service_python_modules": locals().get("service_modules"),
                "initial_devices": locals().get("initial_devices"),
                "atb_runtime_preloads": locals().get("preloads"),
                "aicpu_runtime_library_paths": locals().get("runtime_paths"),
            },
        )
    except Exception:
        emit("SMOKE_EXCEPTION_TRACEBACK", traceback.format_exc())
        return 99

    if args.check_only:
        _write_json(
            output_dir / "summary.json",
            {
                "schema_version": 1,
                "result": "check_only_succeeded",
                "sample_count": len(samples),
                "discovery_failure_count": len(discovery_failures),
                "current_python_modules": current_modules,
                "service_python_modules": service_modules,
                "model_artifacts": model_artifacts,
                "initial_devices": initial_devices,
                "environment_fingerprint": environment.environment_fingerprint,
                "environment_versions": dict(environment.versions.items_tuple()),
                "atb_runtime_preloads": preloads,
                "aicpu_runtime_library_paths": runtime_paths,
                "output_dir": str(output_dir),
            },
        )
        emit("SMOKE_RESULT", "check_only_succeeded")
        return 0

    summaries: list[dict[str, object]] = []
    result_code = 0
    try:
        for family in ("text", "vision"):
            family_samples = [sample for sample in samples if sample.family == family]
            if not family_samples:
                continue
            emit(
                "FAMILY_START_JSON",
                {
                    "family": family,
                    "inference_backend": str(args.inference_backend),
                    "sample_count": len(family_samples),
                    "model_path": str(
                        args.text_model_path
                        if family == "text"
                        else args.vision_model_path
                    ),
                },
            )
            summaries.append(
                await _run_family(
                    args=args,
                    family=family,
                    samples=family_samples,
                    output_dir=output_dir,
                    environment=environment,
                    preloads=preloads,
                    runtime_paths=runtime_paths,
                    device_adapter=device_adapter,
                )
            )
    except SmokePreflightError as exc:
        emit("SMOKE_PREFLIGHT_FAILED", str(exc))
        result_code = 2
    except Exception:
        emit("SMOKE_EXCEPTION_TRACEBACK", traceback.format_exc())
        result_code = 99

    final_devices: list[dict[str, object]]
    cleanup_errors: list[str] = []
    try:
        final_devices = _device_summary(device_adapter)
    except Exception as exc:
        final_devices = []
        cleanup_errors.append(f"final_dcmi_audit:{type(exc).__name__}:{exc}")
        emit("FINAL_ASCEND_AUDIT_FAILED", traceback.format_exc())
    else:
        emit("FINAL_ASCEND_DEVICES_JSON", final_devices)

    ports = tuple(range(int(args.first_port), int(args.last_port) + 1))
    owned_process_group_ids = tuple(
        int(service["process_group_id"])
        for summary in summaries
        for service in summary.get("service_launches", ())
        if isinstance(service, dict) and "process_group_id" in service
    )
    residual = _residual_vllm_processes(
        required_model_paths,
        ports,
        owned_process_group_ids=owned_process_group_ids,
    )
    emit("FINAL_RESIDUAL_VLLM_PROCESSES_JSON", residual)
    total_failed = sum(int(summary.get("failed", 0)) for summary in summaries)
    total_succeeded = sum(int(summary.get("succeeded", 0)) for summary in summaries)
    if result_code == 0 and total_failed and not args.allow_sample_failures:
        result_code = 20
    if result_code == 0 and (residual or cleanup_errors):
        result_code = 11
    summary_payload = {
        "schema_version": 1,
        "result": (
            "succeeded"
            if result_code == 0
            else (
                "sample_failures"
                if result_code == 20
                else "cleanup_failed"
                if result_code == 11
                else f"failed:{result_code}"
            )
        ),
        "exit_code": result_code,
        "sample_count": len(samples),
        "succeeded": total_succeeded,
        "failed": total_failed,
        "discovery_failure_count": len(discovery_failures),
        "families": summaries,
        "final_devices": final_devices,
        "residual_vllm_processes": residual,
        "cleanup_errors": cleanup_errors,
        "output_dir": str(output_dir),
    }
    _write_json(output_dir / "summary.json", summary_payload)
    emit("SMOKE_SUMMARY_PATH", str(output_dir / "summary.json"))
    emit("SMOKE_SUMMARY_JSON", summary_payload)
    emit("SMOKE_EXIT_CODE", result_code)
    return result_code


def main() -> int:
    args = parse_args()
    _validate_args(args)
    return asyncio.run(run_smoke(args))


if __name__ == "__main__":
    raise SystemExit(main())
