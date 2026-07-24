#!/usr/bin/env python3
"""Calibrate per-instance HBM for the local Transformers benchmark adapter."""

from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from typing import Any, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
for _path in (str(SRC_ROOT), str(REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


DEFAULT_PYTHON = Path("/home/user2/workplace/miniconda3/envs/ascend-maze/bin/python")
DEFAULT_OUTPUT_DIR = (
    Path.home() / ".local/state/ascend-maze/hbm-mixed-batch20/calibration"
)
DEFAULT_TEXT_MODEL = Path("/home/user2/workplace/model_weight/model_from_hf/Qwen3-4B")
DEFAULT_VISION_MODEL = Path(
    "/home/user2/workplace/model_weight/model_from_hf/Qwen2.5-VL-3B-Instruct"
)
DEFAULT_VISION_IMAGE = (
    REPO_ROOT / "data/gaia/2023/validation/b2c257e0-3ad7-4f05-b8e3-d9da973be36e.jpg"
)
SCHEMA_VERSION = 1
SYSTEM_RESERVED_HBM_MB = 4_096
HBM_HEADROOM_MB = 1_024


@dataclass(frozen=True, slots=True)
class FamilySpec:
    family: str
    model_path: Path
    model_kind: str
    max_model_len: int
    num_hidden_layers: int
    num_key_value_heads: int
    head_dim: int
    trust_remote_code: bool
    vision_workaround: bool


@dataclass(frozen=True, slots=True)
class Scenario:
    scenario_id: str
    families: tuple[str, ...]


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _append_jsonl(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON document is not an object: {path}")
    return value


def _ceil_multiple(value: float, multiple: int) -> int:
    return math.ceil(value / multiple) * multiple


def theoretical_kv_cache_mb(spec: FamilySpec, *, bytes_per_element: int = 2) -> int:
    total_bytes = (
        2
        * spec.num_hidden_layers
        * spec.num_key_value_heads
        * spec.head_dim
        * bytes_per_element
        * spec.max_model_len
    )
    return math.ceil(total_bytes / (1024 * 1024))


def recommended_instance_hbm_mb(peak_process_hbm_mb: int) -> int:
    if peak_process_hbm_mb < 1:
        raise ValueError("peak process HBM must be positive")
    safety_mb = max(2_048, math.ceil(peak_process_hbm_mb * 0.15))
    return _ceil_multiple(peak_process_hbm_mb + safety_mb, 512)


def two_instances_fit(
    instance_hbm_mb: int,
    *,
    total_hbm_mb: int,
    system_reserved_hbm_mb: int = SYSTEM_RESERVED_HBM_MB,
    hbm_headroom_mb: int = HBM_HEADROOM_MB,
) -> bool:
    return (
        2 * instance_hbm_mb + system_reserved_hbm_mb + hbm_headroom_mb <= total_hbm_mb
    )


def _family_specs(args: argparse.Namespace) -> dict[str, FamilySpec]:
    return {
        "text": FamilySpec(
            family="text",
            model_path=args.text_model_path.expanduser().resolve(),
            model_kind="text",
            max_model_len=10_240,
            num_hidden_layers=36,
            num_key_value_heads=8,
            head_dim=128,
            trust_remote_code=True,
            vision_workaround=False,
        ),
        "vision": FamilySpec(
            family="vision",
            model_path=args.vision_model_path.expanduser().resolve(),
            model_kind="vision_language",
            max_model_len=12_288,
            num_hidden_layers=36,
            num_key_value_heads=2,
            head_dim=128,
            trust_remote_code=False,
            vision_workaround=True,
        ),
    }


def _scenarios(
    families: Sequence[str],
    scenario_ids: Sequence[str] | None = None,
) -> tuple[Scenario, ...]:
    selected = set(families)
    result: list[Scenario] = []
    if "text" in selected:
        result.extend(
            (
                Scenario("text-single", ("text",)),
                Scenario("text-double", ("text", "text")),
            )
        )
    if "vision" in selected:
        result.extend(
            (
                Scenario("vision-single", ("vision",)),
                Scenario("vision-double", ("vision", "vision")),
            )
        )
    if selected == {"text", "vision"}:
        result.append(Scenario("text-vision-double", ("text", "vision")))
    if scenario_ids is None:
        return tuple(result)
    requested = set(scenario_ids)
    available = {scenario.scenario_id for scenario in result}
    unavailable = requested - available
    if unavailable:
        raise ValueError(
            "scenarios are unavailable for the selected families: "
            + ", ".join(sorted(unavailable))
        )
    return tuple(
        scenario for scenario in result if scenario.scenario_id in requested
    )


def _data_uri(path: Path, *, max_pixels: int) -> str:
    from PIL import Image

    with Image.open(path) as source:
        image = source.convert("RGB")
        pixels = image.width * image.height
        if pixels > max_pixels:
            scale = math.sqrt(max_pixels / pixels)
            size = (
                max(1, math.floor(image.width * scale)),
                max(1, math.floor(image.height * scale)),
            )
            image = image.resize(size)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=90)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _chat_template_text(tokenizer: Any, text: str) -> tuple[str, int]:
    messages = [{"role": "user", "content": text}]
    kwargs: dict[str, object] = {
        "tokenize": False,
        "add_generation_prompt": True,
        "enable_thinking": False,
    }
    try:
        prompt = tokenizer.apply_chat_template(messages, **kwargs)
    except TypeError:
        kwargs.pop("enable_thinking", None)
        prompt = tokenizer.apply_chat_template(messages, **kwargs)
    encoded = tokenizer([str(prompt)])
    return text, len(encoded["input_ids"][0])


def _chat_template_vision(
    processor: Any,
    *,
    image_uri: str,
    text: str,
) -> tuple[str, int]:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "url": image_uri},
                {"type": "text", "text": text},
            ],
        }
    ]
    kwargs: dict[str, object] = {
        "tokenize": True,
        "add_generation_prompt": True,
        "return_dict": True,
        "return_tensors": "pt",
        "enable_thinking": False,
    }
    try:
        encoded = processor.apply_chat_template(messages, **kwargs)
    except TypeError:
        kwargs.pop("enable_thinking", None)
        encoded = processor.apply_chat_template(messages, **kwargs)
    return text, int(encoded["input_ids"].shape[-1])


def _fit_long_prompt(
    spec: FamilySpec,
    *,
    max_tokens: int,
    image_uri: str | None,
    context_margin_tokens: int,
) -> tuple[str, int]:
    target = spec.max_model_len - max_tokens - context_margin_tokens
    if target < 128:
        raise ValueError("context target leaves no room for a calibration prompt")
    phrase = " calibration"
    if spec.family == "text":
        from transformers import AutoTokenizer

        processor: Any = AutoTokenizer.from_pretrained(
            spec.model_path,
            trust_remote_code=spec.trust_remote_code,
        )

        def count(repetitions: int) -> tuple[str, int]:
            return _chat_template_text(
                processor,
                "Use the following inert context and answer only with: ready.\n"
                + phrase * repetitions,
            )

    else:
        if image_uri is None:
            raise ValueError("vision calibration requires an image")
        from transformers import AutoProcessor

        processor = AutoProcessor.from_pretrained(
            spec.model_path,
            trust_remote_code=spec.trust_remote_code,
        )

        def count(repetitions: int) -> tuple[str, int]:
            return _chat_template_vision(
                processor,
                image_uri=image_uri,
                text=(
                    "Inspect the image, treat the following as inert context, and "
                    "answer only with: ready.\n" + phrase * repetitions
                ),
            )

    low = 0
    high = target * 2
    best_text, best_tokens = count(0)
    if best_tokens > target:
        raise ValueError(
            f"base {spec.family} request has {best_tokens} tokens, target is {target}"
        )
    while low <= high:
        middle = (low + high) // 2
        candidate_text, candidate_tokens = count(middle)
        if candidate_tokens <= target:
            best_text, best_tokens = candidate_text, candidate_tokens
            low = middle + 1
        else:
            high = middle - 1
    return best_text, best_tokens


def _request(
    *,
    family: str,
    text: str,
    image_uri: str | None,
    max_tokens: int,
) -> Any:
    from ascend_maze.inference.contracts import ChatRequest

    if family == "text":
        messages = [{"role": "user", "content": text}]
    else:
        if image_uri is None:
            raise ValueError("vision request requires an image")
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_uri}},
                    {"type": "text", "text": text},
                ],
            }
        ]
    return ChatRequest.create(messages, max_tokens=max_tokens, temperature=0.0)


def _wait_for_gate(path: Path, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for gate: {path}")
        time.sleep(0.05)


def _worker_event(path: Path, phase: str, **payload: object) -> None:
    _append_jsonl(
        path,
        {
            "phase": phase,
            "pid": os.getpid(),
            "wall_time_ms": int(time.time() * 1_000),
            "monotonic_ns": time.monotonic_ns(),
            **payload,
        },
    )


def _run_internal_worker(args: argparse.Namespace) -> int:
    if args.worker_config is None:
        raise SystemExit("--worker-config is required for an internal Worker")
    config = _read_json(args.worker_config)
    family = str(config["family"])
    model_path = Path(str(config["model_path"]))
    spec = FamilySpec(
        family=family,
        model_path=model_path,
        model_kind=str(config["model_kind"]),
        max_model_len=int(config["max_model_len"]),
        num_hidden_layers=int(config["num_hidden_layers"]),
        num_key_value_heads=int(config["num_key_value_heads"]),
        head_dim=int(config["head_dim"]),
        trust_remote_code=bool(config["trust_remote_code"]),
        vision_workaround=bool(config["vision_workaround"]),
    )
    start_gate = Path(str(config["start_gate"]))
    generate_gate = Path(str(config["generate_gate"]))
    ready_file = Path(str(config["ready_file"]))
    result_file = Path(str(config["result_file"]))
    events_file = Path(str(config["events_file"]))
    image_path = Path(str(config["vision_image_path"]))
    max_tokens = int(config["max_tokens"])
    gate_timeout_seconds = float(config["gate_timeout_seconds"])
    os.environ["ASCEND_RT_VISIBLE_DEVICES"] = str(config["device_id"])
    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "family": family,
        "pid": os.getpid(),
        "status": "failed",
    }
    session: Any | None = None
    try:
        image_uri = (
            _data_uri(image_path, max_pixels=int(config["vision_max_pixels"]))
            if family == "vision"
            else None
        )
        small_image_uri = (
            _data_uri(image_path, max_pixels=65_536) if family == "vision" else None
        )
        _worker_event(events_file, "prompt_fit_started")
        long_text, input_tokens = _fit_long_prompt(
            spec,
            max_tokens=max_tokens,
            image_uri=image_uri,
            context_margin_tokens=int(config["context_margin_tokens"]),
        )
        result["planned_input_tokens"] = input_tokens
        _worker_event(events_file, "prompt_fit_completed", input_tokens=input_tokens)

        from ascend_maze.ascend.discovery import discover_aicpu_runtime_library_paths
        from ascend_maze.inference.adapters.transformers_local import (
            TransformersLocalGenerationConfig,
            TransformersLocalGenerationSession,
        )

        generation_config = TransformersLocalGenerationConfig(
            model_path=str(model_path),
            tokenizer_path=str(model_path),
            dtype="bfloat16",
            max_model_len=spec.max_model_len,
            device_id=str(config["device_id"]),
            trust_remote_code=spec.trust_remote_code,
            enable_thinking=False,
            runtime_library_paths=tuple(discover_aicpu_runtime_library_paths()),
            generation_method="manual_greedy",
            model_kind=spec.model_kind,
            qwen2_5_vl_cpu_unique_consecutive_workaround=spec.vision_workaround,
        )
        session = TransformersLocalGenerationSession(generation_config)
        warmup = _request(
            family=family,
            text="Reply only with: ready",
            image_uri=small_image_uri,
            max_tokens=1,
        )
        calibration = _request(
            family=family,
            text=long_text,
            image_uri=image_uri,
            max_tokens=max_tokens,
        )
        _wait_for_gate(start_gate, timeout_seconds=gate_timeout_seconds)
        _worker_event(events_file, "model_load_started")
        warmup_response, warmup_metrics = session.generate(warmup)
        _worker_event(
            events_file,
            "model_load_completed",
            model_load_ms=warmup_metrics.get("model_load_ms"),
        )
        _write_json(
            ready_file,
            {
                "pid": os.getpid(),
                "family": family,
                "warmup_input_tokens": warmup_response.input_tokens,
                "warmup_output_tokens": warmup_response.output_tokens,
                "warmup_metrics": warmup_metrics,
            },
        )
        _wait_for_gate(generate_gate, timeout_seconds=gate_timeout_seconds)
        _worker_event(events_file, "long_generate_started")
        response, metrics = session.generate(calibration)
        _worker_event(
            events_file,
            "long_generate_completed",
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )
        time.sleep(float(config["post_generate_hold_seconds"]))
        result.update(
            {
                "status": "succeeded",
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "finish_reason": response.finish_reason,
                "response_text_bytes": len(response.text.encode("utf-8")),
                "response_sha256": hashlib.sha256(
                    response.text.encode("utf-8")
                ).hexdigest(),
                "warmup_metrics": warmup_metrics,
                "calibration_metrics": metrics,
            }
        )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
        _worker_event(events_file, "worker_failed", error=result["error"])
    finally:
        if session is not None:
            _worker_event(events_file, "cleanup_started")
            result["cleanup_ms"] = session.close()
            _worker_event(
                events_file,
                "cleanup_completed",
                cleanup_ms=result["cleanup_ms"],
            )
        _write_json(result_file, result)
    return 0 if result["status"] == "succeeded" else 1


def _device_sample(adapter: Any, device_id: str) -> dict[str, object]:
    device = adapter.device(device_id)
    return {
        "wall_time_ms": int(time.time() * 1_000),
        "monotonic_ns": time.monotonic_ns(),
        "device_id": device.physical_device_id,
        "used_hbm_mb": device.used_hbm_mb,
        "total_hbm_mb": device.total_hbm_mb,
        "utilization_pct": device.utilization,
        "processes": [asdict(process) for process in device.processes],
    }


def _sample_until(
    *,
    adapter: Any,
    device_id: str,
    samples_path: Path,
    interval_seconds: float,
    predicate: Any,
    deadline: float,
) -> None:
    while not predicate():
        _append_jsonl(samples_path, _device_sample(adapter, device_id))
        if time.monotonic() >= deadline:
            raise TimeoutError("HBM scenario deadline expired")
        time.sleep(interval_seconds)
    _append_jsonl(samples_path, _device_sample(adapter, device_id))


def _terminate_processes(processes: Sequence[subprocess.Popen[str]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 10
    for process in processes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            pass
    for process in processes:
        if process.poll() is None:
            process.kill()
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _load_samples(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [
        value
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for value in [json.loads(line)]
        if isinstance(value, dict)
    ]


def _scenario_summary(
    *,
    scenario: Scenario,
    baseline: dict[str, object],
    samples: list[dict[str, object]],
    workers: list[dict[str, object]],
    recovered: bool,
) -> dict[str, object]:
    pids = {
        int(worker["pid"]) for worker in workers if isinstance(worker.get("pid"), int)
    }
    peak_used = max((int(item["used_hbm_mb"]) for item in samples), default=0)
    baseline_used = int(baseline["used_hbm_mb"])
    per_process_peak = {
        str(pid): max(
            (
                int(process["hbm_mb"])
                for sample in samples
                for process in sample.get("processes", [])
                if isinstance(process, dict) and process.get("pid") == pid
            ),
            default=0,
        )
        for pid in sorted(pids)
    }
    return {
        "scenario_id": scenario.scenario_id,
        "families": scenario.families,
        "status": (
            "succeeded"
            if workers
            and all(worker.get("status") == "succeeded" for worker in workers)
            and recovered
            else "failed"
        ),
        "baseline_used_hbm_mb": baseline_used,
        "peak_used_hbm_mb": peak_used,
        "peak_incremental_hbm_mb": max(0, peak_used - baseline_used),
        "per_process_peak_hbm_mb": per_process_peak,
        "workers": workers,
        "sample_count": len(samples),
        "hbm_recovered": recovered,
    }


def _run_scenario(
    *,
    args: argparse.Namespace,
    adapter: Any,
    specs: dict[str, FamilySpec],
    scenario: Scenario,
) -> dict[str, object]:
    scenario_dir = args.output_dir / "scenarios" / scenario.scenario_id
    scenario_dir.mkdir(parents=True, exist_ok=True)
    start_gate = scenario_dir / "start.gate"
    generate_gate = scenario_dir / "generate.gate"
    samples_path = scenario_dir / "hbm_samples.jsonl"
    baseline = _device_sample(adapter, args.device_id)
    if baseline["processes"] and not args.allow_busy_device:
        raise RuntimeError(
            f"NPU {args.device_id} is busy before {scenario.scenario_id}: "
            f"{baseline['processes']}"
        )
    _write_json(scenario_dir / "baseline.json", baseline)
    processes: list[subprocess.Popen[str]] = []
    streams: list[Any] = []
    result_files: list[Path] = []
    ready_files: list[Path] = []
    deadline = time.monotonic() + args.scenario_timeout_seconds
    try:
        for index, family in enumerate(scenario.families, start=1):
            worker_dir = scenario_dir / f"worker-{index}-{family}"
            worker_dir.mkdir(parents=True, exist_ok=True)
            ready_file = worker_dir / "ready.json"
            result_file = worker_dir / "result.json"
            events_file = worker_dir / "events.jsonl"
            config_file = worker_dir / "config.json"
            worker_config = {
                **asdict(specs[family]),
                "model_path": str(specs[family].model_path),
                "device_id": args.device_id,
                "max_tokens": args.max_tokens,
                "context_margin_tokens": args.context_margin_tokens,
                "vision_image_path": str(args.vision_image_path.resolve()),
                "vision_max_pixels": args.vision_max_pixels,
                "post_generate_hold_seconds": args.post_generate_hold_seconds,
                "gate_timeout_seconds": args.scenario_timeout_seconds,
                "start_gate": str(start_gate),
                "generate_gate": str(generate_gate),
                "ready_file": str(ready_file),
                "result_file": str(result_file),
                "events_file": str(events_file),
            }
            _write_json(config_file, worker_config)
            stdout = (worker_dir / "stdout.log").open("w", encoding="utf-8")
            stderr = (worker_dir / "stderr.log").open("w", encoding="utf-8")
            streams.extend((stdout, stderr))
            env = dict(os.environ)
            env["ASCEND_RT_VISIBLE_DEVICES"] = args.device_id
            env["PYTHONPATH"] = os.pathsep.join(
                (str(SRC_ROOT), str(REPO_ROOT), env.get("PYTHONPATH", ""))
            )
            process = subprocess.Popen(
                [
                    str(args.python_executable),
                    str(Path(__file__).resolve()),
                    "--internal-worker",
                    "--worker-config",
                    str(config_file),
                ],
                cwd=REPO_ROOT,
                env=env,
                stdout=stdout,
                stderr=stderr,
                text=True,
            )
            processes.append(process)
            result_files.append(result_file)
            ready_files.append(ready_file)
        start_gate.touch()

        def all_ready_or_failed() -> bool:
            return all(path.exists() for path in ready_files) or any(
                process.poll() is not None and not ready.exists()
                for process, ready in zip(processes, ready_files, strict=True)
            )

        _sample_until(
            adapter=adapter,
            device_id=args.device_id,
            samples_path=samples_path,
            interval_seconds=args.sample_interval_seconds,
            predicate=all_ready_or_failed,
            deadline=deadline,
        )
        if not all(path.exists() for path in ready_files):
            raise RuntimeError("one or more calibration Workers failed during load")
        generate_gate.touch()
        _sample_until(
            adapter=adapter,
            device_id=args.device_id,
            samples_path=samples_path,
            interval_seconds=args.sample_interval_seconds,
            predicate=lambda: all(process.poll() is not None for process in processes),
            deadline=deadline,
        )
        for process in processes:
            process.wait(timeout=5)
    except Exception:
        _terminate_processes(processes)
    finally:
        for stream in streams:
            stream.close()

    recovery_deadline = time.monotonic() + args.recovery_timeout_seconds
    recovered = False
    while time.monotonic() < recovery_deadline:
        sample = _device_sample(adapter, args.device_id)
        _append_jsonl(samples_path, sample)
        owned_pids = {process.pid for process in processes}
        owned_alive = any(
            process.get("pid") in owned_pids for process in sample["processes"]
        )
        recovered = (
            not owned_alive
            and int(sample["used_hbm_mb"])
            <= int(baseline["used_hbm_mb"]) + args.recovery_tolerance_mb
        )
        if recovered:
            break
        time.sleep(args.sample_interval_seconds)
    workers = [
        _read_json(path)
        if path.exists()
        else {
            "status": "failed",
            "pid": process.pid,
            "error": f"worker exited {process.returncode} without result",
        }
        for path, process in zip(result_files, processes, strict=True)
    ]
    summary = _scenario_summary(
        scenario=scenario,
        baseline=baseline,
        samples=_load_samples(samples_path),
        workers=workers,
        recovered=recovered,
    )
    _write_json(scenario_dir / "summary.json", summary)
    return summary


def _recommendations(
    *,
    specs: dict[str, FamilySpec],
    scenarios: Sequence[dict[str, object]],
    total_hbm_mb: int,
) -> dict[str, object]:
    result: dict[str, object] = {}
    by_id = {str(item["scenario_id"]): item for item in scenarios}
    for family in ("text", "vision"):
        single = by_id.get(f"{family}-single")
        double = by_id.get(f"{family}-double")
        if single is None:
            continue
        peaks = [
            int(value)
            for value in single.get("per_process_peak_hbm_mb", {}).values()
            if isinstance(value, int)
        ]
        if not peaks or single.get("status") != "succeeded":
            result[family] = {"status": "insufficient_evidence"}
            continue
        peak = max(peaks)
        instance = recommended_instance_hbm_mb(peak)
        result[family] = {
            "status": "calibrated",
            "observed_single_process_peak_hbm_mb": peak,
            "safety_policy": "max(2048 MiB, 15 percent), rounded to 512 MiB",
            "recommended_instance_hbm_mb": instance,
            "theoretical_bf16_kv_cache_hbm_mb": theoretical_kv_cache_mb(specs[family]),
            "two_instances_fit_contract": two_instances_fit(
                instance,
                total_hbm_mb=total_hbm_mb,
            ),
            "double_scenario_status": None if double is None else double.get("status"),
            "double_peak_incremental_hbm_mb": (
                None if double is None else double.get("peak_incremental_hbm_mb")
            ),
            "double_hbm_recovered": (
                None if double is None else double.get("hbm_recovered")
            ),
        }
    return result


def _run_coordinator(args: argparse.Namespace) -> int:
    from ascend_maze.ascend.dcmi import DcmiDeviceAdapter

    args.output_dir = args.output_dir.expanduser().resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    specs = _family_specs(args)
    for family in args.family:
        if not specs[family].model_path.is_dir():
            raise FileNotFoundError(specs[family].model_path)
    if "vision" in args.family and not args.vision_image_path.is_file():
        raise FileNotFoundError(args.vision_image_path)
    adapter = DcmiDeviceAdapter()
    initial = _device_sample(adapter, args.device_id)
    scenarios: list[dict[str, object]] = []
    for scenario in _scenarios(args.family, args.scenario):
        print(json.dumps({"event": "scenario_start", "scenario": scenario.scenario_id}))
        summary = _run_scenario(
            args=args,
            adapter=adapter,
            specs=specs,
            scenario=scenario,
        )
        scenarios.append(summary)
        print(
            json.dumps(
                {
                    "event": "scenario_finish",
                    "scenario": scenario.scenario_id,
                    "status": summary["status"],
                    "peak_incremental_hbm_mb": summary["peak_incremental_hbm_mb"],
                    "hbm_recovered": summary["hbm_recovered"],
                }
            )
        )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "objective": "transformers_local_hbm_calibration",
        "contract": {
            "device_id": args.device_id,
            "max_tokens": args.max_tokens,
            "temperature": 0.0,
            "generation_method": "manual_greedy",
            "context_margin_tokens": args.context_margin_tokens,
            "vision_image_path": str(args.vision_image_path.resolve()),
            "vision_max_pixels": args.vision_max_pixels,
            "system_reserved_hbm_mb": SYSTEM_RESERVED_HBM_MB,
            "hbm_headroom_mb": HBM_HEADROOM_MB,
        },
        "initial_device": initial,
        "families": {name: asdict(spec) for name, spec in specs.items()},
        "scenarios": scenarios,
        "recommendations": _recommendations(
            specs=specs,
            scenarios=scenarios,
            total_hbm_mb=int(initial["total_hbm_mb"]),
        ),
        "result": (
            "succeeded"
            if scenarios and all(item["status"] == "succeeded" for item in scenarios)
            else "failed"
        ),
    }
    for payload in summary["families"].values():
        payload["model_path"] = str(payload["model_path"])
    _write_json(args.output_dir / "summary.json", summary)
    print(json.dumps({"result": summary["result"], "output_dir": str(args.output_dir)}))
    return 0 if summary["result"] == "succeeded" else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", action="append", choices=("text", "vision"))
    parser.add_argument(
        "--scenario",
        action="append",
        choices=(
            "text-single",
            "text-double",
            "vision-single",
            "vision-double",
            "text-vision-double",
        ),
        help="run only the selected scenario; may be repeated",
    )
    parser.add_argument("--device-id", default="0")
    parser.add_argument("--text-model-path", type=Path, default=DEFAULT_TEXT_MODEL)
    parser.add_argument("--vision-model-path", type=Path, default=DEFAULT_VISION_MODEL)
    parser.add_argument("--vision-image-path", type=Path, default=DEFAULT_VISION_IMAGE)
    parser.add_argument("--vision-max-pixels", type=int, default=3_000_000)
    parser.add_argument("--max-tokens", type=int, default=4_096)
    parser.add_argument("--context-margin-tokens", type=int, default=64)
    parser.add_argument("--sample-interval-seconds", type=float, default=0.1)
    parser.add_argument("--post-generate-hold-seconds", type=float, default=2.0)
    parser.add_argument("--scenario-timeout-seconds", type=float, default=1_200.0)
    parser.add_argument("--recovery-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--recovery-tolerance-mb", type=int, default=512)
    parser.add_argument("--python-executable", type=Path, default=DEFAULT_PYTHON)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--allow-busy-device", action="store_true")
    parser.add_argument(
        "--internal-worker", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument("--worker-config", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.family:
        args.family = ["text", "vision"]
    if len(set(args.family)) != len(args.family):
        parser.error("--family values must be unique")
    if args.scenario and len(set(args.scenario)) != len(args.scenario):
        parser.error("--scenario values must be unique")
    try:
        _scenarios(args.family, args.scenario)
    except ValueError as exc:
        parser.error(str(exc))
    for name in (
        "vision_max_pixels",
        "max_tokens",
        "context_margin_tokens",
        "recovery_tolerance_mb",
    ):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    for name in (
        "sample_interval_seconds",
        "post_generate_hold_seconds",
        "scenario_timeout_seconds",
        "recovery_timeout_seconds",
    ):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    return args


def main() -> int:
    args = parse_args()
    if args.internal_worker:
        return _run_internal_worker(args)
    return _run_coordinator(args)


if __name__ == "__main__":
    raise SystemExit(main())
