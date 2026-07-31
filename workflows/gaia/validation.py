"""Minimal GAIA validation runner for Maze's reason and file workflows.

The runner expects a GAIA directory containing ``gaia_query.jsonl`` and
``2023/validation/metadata.jsonl``. Reports contain validation answers and
must not be published with the gated GAIA dataset.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import time
from typing import Any

from maze import MaClient

from workflows.gaia.file import gaia_file
from workflows.gaia.reason import gaia_reason
from workflows.gaia.scorer import (
    GAIA_SCORER_BLOB,
    GAIA_SCORER_REVISION,
    GAIA_SCORER_SHA256,
    GAIA_SCORER_SOURCE_URL,
    question_scorer,
)


SUPPORTED_FILE_EXTENSIONS = frozenset({".txt", ".md", ".pdf"})
FINAL_ANSWER_MARKER = "FINAL ANSWER:"


@dataclass(frozen=True)
class GaiaSample:
    query_index: int
    dag_id: str
    workflow: str
    question: str
    expected: str
    source_file: Path | None = None
    skip_reason: str = ""
    load_error: str = ""


@dataclass(frozen=True)
class ValidationConfig:
    server_url: str
    data_root: Path
    output_dir: Path
    base_url: str
    model: str
    api_key: str = ""
    limit: int | None = None
    max_in_flight_runs: int | None = None
    timeout: float = 600.0
    temperature: float = 0.0
    max_tokens: int = 2048

    def validate(self) -> None:
        if not self.server_url.strip():
            raise ValueError("server_url is required")
        if not self.base_url.strip():
            raise ValueError("base_url is required")
        if not self.model.strip():
            raise ValueError("model is required")
        if self.limit is not None and self.limit < 1:
            raise ValueError("limit must be positive")
        if self.max_in_flight_runs is not None and self.max_in_flight_runs < 1:
            raise ValueError("max_in_flight_runs must be positive")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be positive")


def extract_final_answer(raw_answer: str) -> str | None:
    marker_index = raw_answer.rfind(FINAL_ANSWER_MARKER)
    if marker_index < 0:
        return None
    prediction = raw_answer[marker_index + len(FINAL_ANSWER_MARKER) :].strip()
    return prediction or None


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        raise FileNotFoundError(f"required GAIA file is missing: {path}")
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{line_number}: invalid JSON: {exc}"
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: record must be an object")
            records.append(value)
    return records


def _validation_metadata(data_root: Path) -> dict[str, dict[str, object]]:
    metadata_path = data_root / "2023" / "validation" / "metadata.jsonl"
    by_id: dict[str, dict[str, object]] = {}
    for record in _read_jsonl(metadata_path):
        dag_id = str(record.get("task_id", "")).strip()
        if not dag_id:
            raise ValueError(f"{metadata_path}: metadata record is missing task_id")
        if dag_id in by_id:
            raise ValueError(f"{metadata_path}: duplicate task_id: {dag_id}")
        by_id[dag_id] = record
    return by_id


def _safe_dataset_file(validation_dir: Path, value: str) -> Path:
    normalized = value.replace("\\", "/")
    relative = PurePosixPath(normalized)
    windows_path = PureWindowsPath(value)
    if (
        not value.strip()
        or relative.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in relative.parts
    ):
        raise ValueError("supplementary file path must stay within validation data")
    candidate = (validation_dir / Path(*relative.parts)).resolve(strict=False)
    try:
        candidate.relative_to(validation_dir)
    except ValueError as exc:
        raise ValueError(
            "supplementary file path must stay within validation data"
        ) from exc
    return candidate


def _file_sample(
    *,
    query_index: int,
    dag_id: str,
    question: str,
    expected: str,
    query: dict[str, object],
    metadata: dict[str, object],
    validation_dir: Path,
) -> GaiaSample:
    raw_files = query.get("dag_supplementary_files", [])
    if not isinstance(raw_files, list) or len(raw_files) != 1:
        return GaiaSample(
            query_index,
            dag_id,
            "file",
            question,
            expected,
            load_error="file workflow requires exactly one supplementary file",
        )
    file_name = str(raw_files[0]).strip()
    metadata_name = str(metadata.get("file_name", "")).strip()
    if metadata_name and Path(metadata_name).name != Path(file_name).name:
        return GaiaSample(
            query_index,
            dag_id,
            "file",
            question,
            expected,
            load_error="query and metadata supplementary file names differ",
        )
    try:
        source_file = _safe_dataset_file(validation_dir, file_name)
    except ValueError as exc:
        return GaiaSample(
            query_index,
            dag_id,
            "file",
            question,
            expected,
            load_error=str(exc),
        )
    extension = source_file.suffix.lower()
    if extension not in SUPPORTED_FILE_EXTENSIONS:
        return GaiaSample(
            query_index,
            dag_id,
            "file",
            question,
            expected,
            source_file=source_file,
            skip_reason=f"unsupported_file_type:{extension or '<none>'}",
        )
    if not source_file.is_file():
        return GaiaSample(
            query_index,
            dag_id,
            "file",
            question,
            expected,
            source_file=source_file,
            load_error=f"supplementary file is missing: {file_name}",
        )
    return GaiaSample(
        query_index,
        dag_id,
        "file",
        question,
        expected,
        source_file=source_file,
    )


def load_validation_samples(data_root: Path) -> list[GaiaSample]:
    data_root = data_root.expanduser().resolve(strict=True)
    metadata_by_id = _validation_metadata(data_root)
    validation_dir = (data_root / "2023" / "validation").resolve(strict=True)
    query_path = data_root / "gaia_query.jsonl"
    samples = []
    seen_ids: set[str] = set()
    for query_index, query in enumerate(_read_jsonl(query_path)):
        dag_id = str(query.get("dag_id", "")).strip()
        if not dag_id:
            raise ValueError(f"{query_path}:{query_index + 1}: missing dag_id")
        if dag_id in seen_ids:
            raise ValueError(f"{query_path}:{query_index + 1}: duplicate dag_id: {dag_id}")
        seen_ids.add(dag_id)
        metadata = metadata_by_id.get(dag_id)
        if metadata is None:
            continue

        workflow = str(query.get("dag_type", "")).strip()
        question_value = metadata.get("Question", "")
        expected_value = metadata.get("Final answer", "")
        question = "" if question_value is None else str(question_value).strip()
        expected = "" if expected_value is None else str(expected_value).strip()
        common_error = ""
        if not question:
            common_error = "validation metadata is missing Question"
        elif not expected:
            common_error = "validation metadata is missing Final answer"
        if str(query.get("dag_source", "gaia")) != "gaia":
            samples.append(
                GaiaSample(
                    query_index,
                    dag_id,
                    workflow,
                    question,
                    expected,
                    skip_reason="unsupported_source",
                )
            )
        elif workflow == "reason":
            samples.append(
                GaiaSample(
                    query_index,
                    dag_id,
                    workflow,
                    question,
                    expected,
                    load_error=common_error,
                )
            )
        elif workflow == "file":
            sample = _file_sample(
                query_index=query_index,
                dag_id=dag_id,
                question=question,
                expected=expected,
                query=query,
                metadata=metadata,
                validation_dir=validation_dir,
            )
            if common_error:
                sample = replace(sample, load_error=common_error)
            samples.append(sample)
        else:
            samples.append(
                GaiaSample(
                    query_index,
                    dag_id,
                    workflow,
                    question,
                    expected,
                    skip_reason=f"unsupported_workflow:{workflow or '<empty>'}",
                )
            )
    return samples


def _empty_result(sample: GaiaSample) -> dict[str, object]:
    return {
        "query_index": sample.query_index,
        "dag_id": sample.dag_id,
        "workflow": sample.workflow,
        "run_id": "",
        "status": "failed",
        "raw_answer": "",
        "prediction": "",
        "expected": sample.expected,
        "correct": False,
        "parsed": False,
        "latency": 0.0,
        "error": "",
    }


def _run_error(run: dict[str, object]) -> str:
    error = run.get("error_summary") or run.get("error")
    if not error:
        return f"run ended with status {run.get('status', '<unknown>')}"
    if isinstance(error, str):
        return error
    return json.dumps(error, ensure_ascii=False, sort_keys=True)


def _run_sample(
    sample: GaiaSample,
    *,
    template: Any,
    client: MaClient,
    config: ValidationConfig,
) -> dict[str, object]:
    result = _empty_result(sample)
    started = time.perf_counter()
    run_id = ""
    try:
        inputs: dict[str, object] = {
            "dag_id": sample.dag_id,
            "question": sample.question,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
        }
        run_kwargs: dict[str, object] = {
            "timeout_seconds": config.timeout,
            "inputs": inputs,
        }
        if sample.workflow == "file":
            if sample.source_file is None:
                raise ValueError("file sample has no source file")
            workspace = config.output_dir / "workspaces" / f"{sample.query_index:05d}"
            files_dir = workspace / "files"
            files_dir.mkdir(parents=True, exist_ok=False)
            staged_name = sample.source_file.name
            shutil.copyfile(sample.source_file, files_dir / staged_name)
            inputs["supplementary_path"] = staged_name
            run_kwargs.update(
                workspace_dir=str(workspace),
                artifact_mode=True,
            )

        run_id = template.run(**run_kwargs)
        result["run_id"] = run_id
        try:
            run = client.wait_run(run_id, timeout=config.timeout)
        except TimeoutError:
            try:
                client.cancel_run(run_id, reason="GAIA validation timeout")
            except Exception:
                pass
            result["status"] = "timed_out"
            result["error"] = f"run exceeded timeout of {config.timeout} seconds"
            return result

        status = str(run.get("status", "failed"))
        result["status"] = status
        if status != "succeeded":
            result["error"] = _run_error(run)
            return result
        summary = run.get("result_summary")
        if not isinstance(summary, dict) or not isinstance(
            summary.get("final_answer"),
            str,
        ):
            result["error"] = "succeeded run is missing result_summary.final_answer"
            return result
        raw_answer = summary["final_answer"]
        result["raw_answer"] = raw_answer
        prediction = extract_final_answer(raw_answer)
        if prediction is None:
            result["error"] = f"model output is missing a non-empty {FINAL_ANSWER_MARKER}"
            return result
        result["prediction"] = prediction
        result["parsed"] = True
        result["correct"] = question_scorer(prediction, sample.expected)
        return result
    except Exception as exc:
        result["run_id"] = run_id
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        result["latency"] = round(time.perf_counter() - started, 6)


def _template_inputs(config: ValidationConfig) -> dict[str, str]:
    return {
        "qwen_base_url": config.base_url,
        "qwen_model": config.model,
        "qwen_api_key": config.api_key,
        "deepseek_base_url": config.base_url,
        "deepseek_model": config.model,
        "deepseek_api_key": config.api_key,
    }


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def run_validation(config: ValidationConfig) -> dict[str, object]:
    config = replace(
        config,
        data_root=config.data_root.expanduser().resolve(strict=True),
        output_dir=config.output_dir.expanduser().resolve(),
    )
    config.validate()
    samples = load_validation_samples(config.data_root)
    selected = samples[: config.limit] if config.limit is not None else samples
    config.output_dir.mkdir(parents=True, exist_ok=False)

    results = []
    runnable = []
    for sample in selected:
        result = _empty_result(sample)
        if sample.skip_reason:
            result["status"] = "skipped"
            result["error"] = sample.skip_reason
            results.append(result)
        elif sample.load_error:
            result["error"] = sample.load_error
            results.append(result)
        else:
            runnable.append(sample)

    client = MaClient(config.server_url)
    template_definitions = {"reason": gaia_reason, "file": gaia_file}
    templates: dict[str, Any] = {}
    template_errors: dict[str, str] = {}
    for workflow_name in sorted({sample.workflow for sample in runnable}):
        try:
            templates[workflow_name] = client.create_workflow_from(
                template_definitions[workflow_name],
                inputs=_template_inputs(config),
            )
        except Exception as exc:
            template_errors[workflow_name] = f"template setup failed: {type(exc).__name__}: {exc}"

    pending = []
    for sample in runnable:
        error = template_errors.get(sample.workflow)
        if error:
            result = _empty_result(sample)
            result["error"] = error
            results.append(result)
        else:
            pending.append(sample)

    if pending:
        max_workers = len(pending)
        if config.max_in_flight_runs is not None:
            max_workers = min(config.max_in_flight_runs, max_workers)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    _run_sample,
                    sample,
                    template=templates[sample.workflow],
                    client=client,
                    config=config,
                ): sample
                for sample in pending
            }
            for future in as_completed(futures):
                results.append(future.result())

    results.sort(key=lambda item: int(item["query_index"]))
    skipped = sum(item["status"] == "skipped" for item in results)
    supported = len(results) - skipped
    submitted = sum(bool(item["run_id"]) for item in results)
    succeeded = sum(item["status"] == "succeeded" for item in results)
    parsed = sum(bool(item["parsed"]) for item in results)
    correct = sum(bool(item["correct"]) for item in results)
    summary: dict[str, object] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "discovered": len(samples),
        "selected": len(selected),
        "supported": supported,
        "submitted": submitted,
        "succeeded": succeeded,
        "parsed": parsed,
        "correct": correct,
        "skipped": skipped,
        "run_success_rate": _ratio(succeeded, supported),
        "parse_rate": _ratio(parsed, supported),
        "supported_subset_accuracy": _ratio(correct, supported),
        "config": {
            "server_url": config.server_url,
            "data_root": str(config.data_root.expanduser().resolve()),
            "base_url": config.base_url,
            "model": config.model,
            "limit": config.limit,
            "max_in_flight_runs": config.max_in_flight_runs,
            "timeout": config.timeout,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
        },
        "scorer": {
            "revision": GAIA_SCORER_REVISION,
            "blob": GAIA_SCORER_BLOB,
            "sha256": GAIA_SCORER_SHA256,
            "source_url": GAIA_SCORER_SOURCE_URL,
        },
    }
    _write_jsonl(config.output_dir / "results.jsonl", results)
    _write_json(config.output_dir / "summary.json", summary)
    return summary


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Maze GAIA reason/file validation and write scored reports."
    )
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--api-key",
        default="",
        help="Use env:VARIABLE_NAME for secrets so they are not persisted by Maze.",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-in-flight-runs", type=int)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=2048)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = ValidationConfig(
        server_url=args.server_url,
        data_root=args.data_root,
        output_dir=args.output_dir,
        base_url=args.base_url,
        model=args.model,
        api_key=args.api_key,
        limit=args.limit,
        max_in_flight_runs=args.max_in_flight_runs,
        timeout=args.timeout,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    )
    summary = run_validation(config)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
