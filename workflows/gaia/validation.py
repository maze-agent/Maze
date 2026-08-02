"""Minimal GAIA validation runner for Maze's reason and file workflows.

The runner expects a GAIA directory containing ``gaia_query.jsonl`` and
``2023/validation/metadata.jsonl``, plus running Maze Core and Playground
backend services. Reports contain validation answers and must not be published
with the gated GAIA dataset.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import secrets
import stat
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from maze import MaClient
from maze.client.maze.workflow import _encode_output_refs

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
API_KEY_ENV_PATTERN = re.compile(r"env:[A-Za-z_][A-Za-z0-9_]*\Z")
PRIVATE_STATE_DIR_NAME = ".gaia-validation-state"
PRIVATE_STATE_SCHEMA_VERSION = 1


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
    playground_url: str = "http://127.0.0.1:3001"
    playground_workspace_id: str = "default"
    api_key: str = ""
    limit: int | None = None
    max_in_flight_runs: int | None = None
    timeout: float = 600.0
    temperature: float = 0.0
    max_tokens: int = 2048

    def validate(self) -> None:
        if not self.server_url.strip():
            raise ValueError("server_url is required")
        if not self.playground_url.strip():
            raise ValueError("playground_url is required")
        if not self.playground_workspace_id.strip():
            raise ValueError("playground_workspace_id is required")
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
        if not isinstance(self.api_key, str) or (
            self.api_key and API_KEY_ENV_PATTERN.fullmatch(self.api_key) is None
        ):
            raise ValueError(
                "api_key must be empty or an env:VARIABLE_NAME reference"
            )


class PlaygroundGaiaError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        playground_run_id: str = "",
        maze_run_id: str = "",
        status_code: int | None = None,
        transport_failure: bool = False,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.playground_run_id = playground_run_id
        self.maze_run_id = maze_run_id
        self.status_code = status_code
        self.transport_failure = transport_failure
        self.retryable = (
            transport_failure or (status_code is not None and status_code >= 500)
            if retryable is None
            else retryable
        )


class PlaygroundGaiaClient:
    """Submit GAIA Maze runs through the Playground trace boundary."""

    def __init__(
        self,
        base_url: str,
        workspace_id: str,
        request_timeout: float = 30.0,
        retry_attempts: int = 3,
        retry_delay: float = 0.05,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.workspace_id = workspace_id
        self.request_timeout = request_timeout
        self.retry_attempts = max(1, int(retry_attempts))
        self.retry_delay = max(0.0, float(retry_delay))
        self._capabilities: dict[str, tuple[str, str]] = {}
        self._capabilities_lock = threading.Lock()

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urlopen(request, timeout=self.request_timeout) as response:
                raw_response = response.read()
                value = json.loads(raw_response.decode("utf-8"))
        except HTTPError as exc:
            try:
                value = json.loads(exc.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = {}
            if not isinstance(value, dict):
                value = {}
            raise PlaygroundGaiaError(
                str(value.get("error") or f"Playground request failed with HTTP {exc.code}"),
                playground_run_id=str(value.get("playgroundRunId") or ""),
                maze_run_id=str(value.get("mazeRunId") or ""),
                status_code=exc.code,
            ) from exc
        except (TimeoutError, URLError, OSError, HTTPException) as exc:
            reason = getattr(exc, "reason", exc)
            raise PlaygroundGaiaError(
                f"Playground request failed: {reason}",
                transport_failure=True,
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlaygroundGaiaError(
                "Playground returned a malformed response",
                transport_failure=True,
            ) from exc
        if not isinstance(value, dict):
            raise PlaygroundGaiaError(
                "Playground returned a non-object response",
                transport_failure=True,
            )
        return value

    def _pause_before_retry(self, attempt: int) -> None:
        if self.retry_delay:
            time.sleep(self.retry_delay * (attempt + 1))

    def _remember_capability(
        self,
        playground_run_id: str,
        sample_ref: str,
        submission_token: str,
    ) -> None:
        if not playground_run_id:
            return
        with self._capabilities_lock:
            self._capabilities[playground_run_id] = (sample_ref, submission_token)

    def _capability_for(self, playground_run_id: str) -> tuple[str, str]:
        with self._capabilities_lock:
            capability = self._capabilities.get(playground_run_id)
        if capability is None:
            raise PlaygroundGaiaError(
                "No GAIA submission capability is available for this Playground run",
                playground_run_id=playground_run_id,
            )
        return capability

    def _lookup_run(
        self,
        sample_ref: str,
        submission_token: str,
        *,
        attempts: int | None = None,
        require_maze_run_id: bool = False,
    ) -> dict[str, object]:
        last_error: PlaygroundGaiaError | None = None
        for attempt in range(attempts or self.retry_attempts):
            try:
                response = self._request(
                    "POST",
                    "/api/benchmarks/gaia/runs/lookup",
                    {
                        "sampleRef": sample_ref,
                        "submissionToken": submission_token,
                        "playgroundWorkspaceId": self.workspace_id,
                    },
                )
                playground_run_id = str(response.get("playgroundRunId") or "")
                maze_run_id = str(response.get("mazeRunId") or "")
                self._remember_capability(
                    playground_run_id,
                    sample_ref,
                    submission_token,
                )
                if not playground_run_id or (require_maze_run_id and not maze_run_id):
                    raise PlaygroundGaiaError(
                        "Playground lookup response is missing run ids",
                        playground_run_id=playground_run_id,
                        maze_run_id=maze_run_id,
                        retryable=True,
                    )
                return response
            except PlaygroundGaiaError as exc:
                last_error = exc
                if not exc.retryable and exc.status_code != 404:
                    raise
            if attempt + 1 < (attempts or self.retry_attempts):
                self._pause_before_retry(attempt)
        if last_error is not None:
            raise last_error
        raise PlaygroundGaiaError("Playground lookup failed")

    @staticmethod
    def _public_terminal_status(status: str) -> str:
        normalized = status.strip().lower()
        return {
            "succeeded": "completed",
            "cancelled": "canceled",
        }.get(normalized, normalized)

    @staticmethod
    def _is_terminal_public_status(status: object) -> bool:
        return str(status or "").strip().lower() in {
            "completed",
            "failed",
            "canceled",
            "timed_out",
            "interrupted",
        }

    def submit_run(
        self,
        *,
        workflow: str,
        sample_ref: str,
        maze_workflow_id: str,
        final_output_refs: dict[str, object],
        inputs: dict[str, object],
        timeout_seconds: float,
        execution_file: Path | None = None,
        submission_token: str | None = None,
    ) -> tuple[str, str]:
        submission_token = submission_token or secrets.token_hex(32)
        if re.fullmatch(r"[0-9a-f]{64}", submission_token) is None:
            raise ValueError(
                "submission_token must be 64 lowercase hexadecimal characters"
            )
        payload: dict[str, object] = {
            "workflow": workflow,
            "sampleRef": sample_ref,
            "mazeWorkflowId": maze_workflow_id,
            "finalOutputRefs": final_output_refs,
            "inputs": inputs,
            "timeoutSeconds": timeout_seconds,
            "playgroundWorkspaceId": self.workspace_id,
            "submissionToken": submission_token,
        }
        if execution_file is not None:
            content = Path(execution_file).read_bytes()
            payload["executionFile"] = {
                "name": Path(execution_file).name,
                "contentBase64": base64.b64encode(content).decode("ascii"),
                "sha256": hashlib.sha256(content).hexdigest(),
            }

        last_error: PlaygroundGaiaError | None = None
        best_playground_run_id = ""
        best_maze_run_id = ""
        for attempt in range(self.retry_attempts):
            try:
                response = self._request(
                    "POST",
                    "/api/benchmarks/gaia/runs",
                    payload,
                )
                playground_run_id = str(response.get("playgroundRunId") or "")
                maze_run_id = str(response.get("mazeRunId") or "")
                if not playground_run_id or not maze_run_id:
                    raise PlaygroundGaiaError(
                        "Playground submission response is missing run ids",
                        playground_run_id=playground_run_id,
                        maze_run_id=maze_run_id,
                        retryable=True,
                    )
                self._remember_capability(
                    playground_run_id,
                    sample_ref,
                    submission_token,
                )
                return playground_run_id, maze_run_id
            except PlaygroundGaiaError as exc:
                last_error = exc
                best_playground_run_id = (
                    exc.playground_run_id or best_playground_run_id
                )
                best_maze_run_id = exc.maze_run_id or best_maze_run_id
                self._remember_capability(
                    best_playground_run_id,
                    sample_ref,
                    submission_token,
                )
                if not exc.retryable:
                    raise
            if attempt + 1 < self.retry_attempts:
                self._pause_before_retry(attempt)

        try:
            response = self._lookup_run(
                sample_ref,
                submission_token,
                require_maze_run_id=True,
            )
            return (
                str(response.get("playgroundRunId") or ""),
                str(response.get("mazeRunId") or ""),
            )
        except PlaygroundGaiaError as lookup_error:
            best_playground_run_id = (
                lookup_error.playground_run_id or best_playground_run_id
            )
            best_maze_run_id = lookup_error.maze_run_id or best_maze_run_id
            if last_error is not None:
                last_error.playground_run_id = (
                    best_playground_run_id or last_error.playground_run_id
                )
                last_error.maze_run_id = best_maze_run_id or last_error.maze_run_id
                raise last_error
            raise

    def lookup_run(
        self,
        sample_ref: str,
        submission_token: str,
        *,
        attempts: int | None = None,
        require_maze_run_id: bool = False,
    ) -> dict[str, object]:
        """Recover a persisted GAIA submission without issuing another submit."""

        return self._lookup_run(
            sample_ref,
            submission_token,
            attempts=attempts,
            require_maze_run_id=require_maze_run_id,
        )

    def restore_capability(
        self,
        playground_run_id: str,
        sample_ref: str,
        submission_token: str,
    ) -> None:
        self._remember_capability(
            playground_run_id,
            sample_ref,
            submission_token,
        )

    def _confirm_finalization(
        self,
        playground_run_id: str,
        sample_ref: str,
        submission_token: str,
        expected_status: str | None,
    ) -> dict[str, object] | None:
        try:
            response = self._lookup_run(
                sample_ref,
                submission_token,
                attempts=1,
            )
        except PlaygroundGaiaError:
            return None
        status = str(response.get("status") or "").strip().lower()
        if expected_status is not None:
            if status != self._public_terminal_status(expected_status):
                return None
        elif not self._is_terminal_public_status(status):
            return None
        if str(response.get("playgroundRunId") or "") != playground_run_id:
            return None
        return response

    def _finalize_run(
        self,
        playground_run_id: str,
        action: str,
        payload: dict[str, object],
        *,
        expected_status: str | None,
    ) -> dict[str, object]:
        sample_ref, submission_token = self._capability_for(playground_run_id)
        request_payload = {
            **payload,
            "submissionToken": submission_token,
            "playgroundWorkspaceId": self.workspace_id,
        }
        last_error: PlaygroundGaiaError | None = None
        for attempt in range(self.retry_attempts):
            try:
                response = self._request(
                    "POST",
                    (
                        "/api/benchmarks/gaia/runs/"
                        f"{quote(playground_run_id, safe='')}/{action}"
                    ),
                    request_payload,
                )
                response_status = str(response.get("status") or "").strip().lower()
                if expected_status is not None:
                    if response_status != self._public_terminal_status(expected_status):
                        raise PlaygroundGaiaError(
                            "Playground persisted an unexpected terminal status",
                            playground_run_id=playground_run_id,
                        )
                elif not self._is_terminal_public_status(response_status):
                    raise PlaygroundGaiaError(
                        "Playground did not persist a terminal status",
                        playground_run_id=playground_run_id,
                        retryable=True,
                    )
                return response
            except PlaygroundGaiaError as exc:
                last_error = exc
                if not exc.retryable:
                    raise
                confirmed = self._confirm_finalization(
                    playground_run_id,
                    sample_ref,
                    submission_token,
                    expected_status,
                )
                if confirmed is not None:
                    return confirmed
            if attempt + 1 < self.retry_attempts:
                self._pause_before_retry(attempt)

        confirmed = self._confirm_finalization(
            playground_run_id,
            sample_ref,
            submission_token,
            expected_status,
        )
        if confirmed is not None:
            return confirmed
        if last_error is not None:
            raise last_error
        raise PlaygroundGaiaError(
            "Playground finalization failed",
            playground_run_id=playground_run_id,
        )

    def finish_run(
        self,
        playground_run_id: str,
        status: str,
    ) -> dict[str, object]:
        return self._finalize_run(
            playground_run_id,
            "finish",
            {"status": status},
            expected_status=status,
        )

    def cancel_run(
        self,
        playground_run_id: str,
        outcome: str = "canceled",
    ) -> dict[str, object]:
        return self._finalize_run(
            playground_run_id,
            "cancel",
            {"outcome": outcome},
            expected_status=None,
        )


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


@dataclass(frozen=True)
class _SampleOutcome:
    result: dict[str, object]
    complete: bool


def _stable_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    try:
        file_stat = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"private state directory disappeared: {path.name}") from exc
    if not stat.S_ISDIR(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
        raise ValueError(f"private state path must be a real directory: {path.name}")
    os.chmod(path, 0o700)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        existing = None
        try:
            existing = path.lstat()
        except FileNotFoundError:
            pass
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise ValueError(f"refusing to replace symbolic link: {path.name}")
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _atomic_write_json(path: Path, value: object) -> None:
    _atomic_write_bytes(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        + b"\n",
    )


def _atomic_write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    content = b"".join(_stable_json_bytes(record) + b"\n" for record in records)
    _atomic_write_bytes(path, content)


def _read_private_json(path: Path) -> dict[str, object] | None:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
        raise ValueError(f"private state path must be a regular file: {path.name}")
    os.chmod(path, 0o600)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"private state file is invalid: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"private state file must contain an object: {path.name}")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sample_identity(sample: GaiaSample) -> str:
    source = None
    if sample.source_file is not None and sample.source_file.is_file():
        source = {
            "name": sample.source_file.name,
            "sha256": _file_sha256(sample.source_file),
        }
    payload = {
        "query_index": sample.query_index,
        "dag_id": sample.dag_id,
        "workflow": sample.workflow,
        "question": sample.question,
        "expected": sample.expected,
        "source": source,
        "skip_reason": sample.skip_reason,
        "load_error": sample.load_error,
    }
    return hashlib.sha256(_stable_json_bytes(payload)).hexdigest()


def _validation_identity(
    config: ValidationConfig,
    selected: list[GaiaSample],
) -> tuple[str, dict[int, str]]:
    sample_identities = {
        sample.query_index: _sample_identity(sample) for sample in selected
    }
    payload = {
        "schema_version": PRIVATE_STATE_SCHEMA_VERSION,
        "config": {
            "server_url": config.server_url,
            "playground_url": config.playground_url,
            "playground_workspace_id": config.playground_workspace_id,
            "data_root": str(config.data_root),
            "base_url": config.base_url,
            "model": config.model,
            "api_key_reference": config.api_key,
            "limit": config.limit,
            "max_in_flight_runs": config.max_in_flight_runs,
            "timeout": config.timeout,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
        },
        "samples": [
            {
                "query_index": sample.query_index,
                "identity_sha256": sample_identities[sample.query_index],
            }
            for sample in selected
        ],
        "scorer_revision": GAIA_SCORER_REVISION,
        "scorer_sha256": GAIA_SCORER_SHA256,
    }
    return hashlib.sha256(_stable_json_bytes(payload)).hexdigest(), sample_identities


class _ValidationState:
    def __init__(
        self,
        output_dir: Path,
        identity_sha256: str,
        sample_identities: dict[int, str],
    ) -> None:
        self.output_dir = output_dir
        self.state_dir = output_dir / PRIVATE_STATE_DIR_NAME
        self.submissions_dir = self.state_dir / "submissions"
        self.sample_results_dir = self.state_dir / "results"
        self.sample_identities = sample_identities
        self.manifest_path = self.state_dir / "manifest.json"

        output_existed = output_dir.exists()
        if output_existed:
            output_stat = output_dir.lstat()
            if not stat.S_ISDIR(output_stat.st_mode) or stat.S_ISLNK(output_stat.st_mode):
                raise ValueError("output_dir must be a real directory")
        else:
            output_dir.mkdir(mode=0o700, parents=True)
        os.chmod(output_dir, 0o700)

        if not self.state_dir.exists() and output_existed:
            existing_entries = list(output_dir.iterdir())
            if existing_entries:
                raise ValueError(
                    "output_dir contains data without a GAIA resume manifest"
                )

        _ensure_private_directory(self.state_dir)
        _ensure_private_directory(self.submissions_dir)
        _ensure_private_directory(self.sample_results_dir)
        manifest = _read_private_json(self.manifest_path)
        if manifest is None:
            _atomic_write_json(
                self.manifest_path,
                {
                    "schema": "gaia_validation_private_state",
                    "schema_version": PRIVATE_STATE_SCHEMA_VERSION,
                    "identity_sha256": identity_sha256,
                    "selected_count": len(sample_identities),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        elif (
            manifest.get("schema") != "gaia_validation_private_state"
            or manifest.get("schema_version") != PRIVATE_STATE_SCHEMA_VERSION
            or manifest.get("identity_sha256") != identity_sha256
            or manifest.get("selected_count") != len(sample_identities)
        ):
            raise ValueError(
                "output_dir belongs to a different GAIA validation configuration or sample set"
            )

    def _sample_path(self, directory: Path, sample: GaiaSample) -> Path:
        return directory / f"{sample.query_index:05d}.json"

    def _validate_sample_wrapper(
        self,
        sample: GaiaSample,
        payload: dict[str, object],
        *,
        kind: str,
    ) -> None:
        if (
            payload.get("schema_version") != PRIVATE_STATE_SCHEMA_VERSION
            or payload.get("kind") != kind
            or payload.get("query_index") != sample.query_index
            or payload.get("sample_identity_sha256")
            != self.sample_identities[sample.query_index]
        ):
            raise ValueError(
                f"private {kind} state conflicts with the selected GAIA sample"
            )

    def load_result(
        self,
        sample: GaiaSample,
    ) -> tuple[dict[str, object], bool] | None:
        payload = _read_private_json(
            self._sample_path(self.sample_results_dir, sample)
        )
        if payload is None:
            return None
        self._validate_sample_wrapper(sample, payload, kind="sample_result")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ValueError("private sample_result state is missing its result")
        if (
            result.get("query_index") != sample.query_index
            or result.get("dag_id") != sample.dag_id
            or result.get("workflow") != sample.workflow
        ):
            raise ValueError("private sample_result identity is invalid")
        return dict(result), payload.get("complete") is True

    def save_result(
        self,
        sample: GaiaSample,
        outcome: _SampleOutcome,
    ) -> dict[str, object]:
        existing = self.load_result(sample)
        if existing is not None and existing[1]:
            return existing[0]
        _atomic_write_json(
            self._sample_path(self.sample_results_dir, sample),
            {
                "schema_version": PRIVATE_STATE_SCHEMA_VERSION,
                "kind": "sample_result",
                "query_index": sample.query_index,
                "sample_identity_sha256": self.sample_identities[sample.query_index],
                "complete": outcome.complete,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "result": outcome.result,
            },
        )
        return outcome.result

    def load_or_create_submission(
        self,
        sample: GaiaSample,
        *,
        maze_workflow_id: str,
        final_output_refs: dict[str, object],
    ) -> tuple[dict[str, object], bool]:
        path = self._sample_path(self.submissions_dir, sample)
        journal = _read_private_json(path)
        if journal is not None:
            self._validate_sample_wrapper(sample, journal, kind="submission")
            token = str(journal.get("submission_token") or "")
            sample_ref = str(journal.get("sample_ref") or "")
            if (
                re.fullmatch(r"[0-9a-f]{64}", token) is None
                or re.fullmatch(r"gaia-[0-9a-f]{32}", sample_ref) is None
                or journal.get("workflow") != sample.workflow
                or not str(journal.get("maze_workflow_id") or "")
                or not isinstance(journal.get("final_output_refs"), dict)
            ):
                raise ValueError("private submission journal is invalid")
            return journal, True

        now = datetime.now(timezone.utc).isoformat()
        journal = {
            "schema_version": PRIVATE_STATE_SCHEMA_VERSION,
            "kind": "submission",
            "query_index": sample.query_index,
            "sample_identity_sha256": self.sample_identities[sample.query_index],
            "workflow": sample.workflow,
            "sample_ref": f"gaia-{secrets.token_hex(16)}",
            "submission_token": secrets.token_hex(32),
            "maze_workflow_id": maze_workflow_id,
            "final_output_refs": final_output_refs,
            "submission_state": "prepared",
            "playground_run_id": "",
            "maze_run_id": "",
            "last_public_status": "",
            "created_at": now,
            "updated_at": now,
        }
        _atomic_write_json(path, journal)
        return journal, False

    def update_submission(
        self,
        sample: GaiaSample,
        *,
        submission_state: str | None = None,
        playground_run_id: str = "",
        maze_run_id: str = "",
        public_status: str = "",
    ) -> dict[str, object]:
        path = self._sample_path(self.submissions_dir, sample)
        journal = _read_private_json(path)
        if journal is None:
            raise RuntimeError("submission journal is missing")
        self._validate_sample_wrapper(sample, journal, kind="submission")
        if submission_state is not None:
            journal["submission_state"] = submission_state
        if playground_run_id:
            journal["playground_run_id"] = playground_run_id
        if maze_run_id:
            journal["maze_run_id"] = maze_run_id
        if public_status:
            journal["last_public_status"] = public_status
        journal["updated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(path, journal)
        return journal


def _empty_result(sample: GaiaSample) -> dict[str, object]:
    return {
        "query_index": sample.query_index,
        "dag_id": sample.dag_id,
        "workflow": sample.workflow,
        "sample_ref": "",
        "playground_run_id": "",
        "maze_run_id": "",
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


def _core_status_from_public(status: object) -> str:
    normalized = str(status or "").strip().lower()
    return {
        "completed": "succeeded",
        "canceled": "cancelled",
    }.get(normalized, normalized)


def _stage_validation_execution_file(
    sample: GaiaSample,
    output_dir: Path,
) -> Path:
    if sample.source_file is None:
        raise ValueError("file sample has no source file")
    workspace = output_dir / "workspaces" / f"{sample.query_index:05d}"
    if not workspace.exists():
        workspace.mkdir(mode=0o700)
    _ensure_private_directory(workspace)
    files_dir = workspace / "files"
    _ensure_private_directory(files_dir)
    staged_path = files_dir / sample.source_file.name
    source_content = sample.source_file.read_bytes()
    try:
        staged_stat = staged_path.lstat()
    except FileNotFoundError:
        _atomic_write_bytes(staged_path, source_content)
    else:
        if not stat.S_ISREG(staged_stat.st_mode) or stat.S_ISLNK(staged_stat.st_mode):
            raise ValueError("staged GAIA input must be a regular file")
        if staged_path.read_bytes() != source_content:
            raise ValueError("staged GAIA input conflicts with the selected sample")
        os.chmod(staged_path, 0o600)
    return staged_path


def _run_sample(
    sample: GaiaSample,
    *,
    template: Any,
    client: MaClient,
    playground_client: PlaygroundGaiaClient,
    config: ValidationConfig,
    validation_state: _ValidationState,
) -> _SampleOutcome:
    result = _empty_result(sample)
    started = time.perf_counter()
    playground_run_id = ""
    maze_run_id = ""
    trace_finalized = False
    journal: dict[str, object] | None = None

    def update_trace_ids(
        *,
        candidate_playground_run_id: object = "",
        candidate_maze_run_id: object = "",
        public_status: object = "",
        submission_state: str | None = None,
    ) -> None:
        nonlocal journal, playground_run_id, maze_run_id
        candidate_playground = str(candidate_playground_run_id or "")
        candidate_maze = str(candidate_maze_run_id or "")
        playground_run_id = candidate_playground or playground_run_id
        maze_run_id = candidate_maze or maze_run_id
        result["playground_run_id"] = playground_run_id
        result["maze_run_id"] = maze_run_id
        result["run_id"] = maze_run_id
        if journal is not None:
            journal = validation_state.update_submission(
                sample,
                submission_state=submission_state,
                playground_run_id=playground_run_id,
                maze_run_id=maze_run_id,
                public_status=str(public_status or ""),
            )

    def restore_capability() -> None:
        if not playground_run_id or journal is None:
            return
        restore = getattr(playground_client, "restore_capability", None)
        if restore is not None:
            restore(
                playground_run_id,
                str(journal["sample_ref"]),
                str(journal["submission_token"]),
            )

    def lookup_submission() -> dict[str, object] | None:
        if journal is None:
            return None
        lookup = getattr(playground_client, "lookup_run", None)
        if lookup is None:
            return None
        response = lookup(
            str(journal["sample_ref"]),
            str(journal["submission_token"]),
            require_maze_run_id=False,
        )
        update_trace_ids(
            candidate_playground_run_id=response.get("playgroundRunId"),
            candidate_maze_run_id=response.get("mazeRunId"),
            public_status=response.get("status"),
            submission_state=("bound" if response.get("mazeRunId") else None),
        )
        restore_capability()
        return response

    def terminal_without_maze_run(
        response: dict[str, object] | None,
        error: str,
    ) -> _SampleOutcome | None:
        if response is None or maze_run_id:
            return None
        status = _core_status_from_public(response.get("status"))
        if status not in {
            "succeeded",
            "failed",
            "cancelled",
            "timed_out",
            "interrupted",
        }:
            return None
        result["status"] = "failed" if status == "succeeded" else status
        result["error"] = error
        update_trace_ids(public_status=response.get("status"), submission_state="terminal")
        return _SampleOutcome(result, complete=True)

    try:
        maze_workflow_id = str(getattr(template, "workflow_id", "")).strip()
        final_output_refs = getattr(template, "final_output_refs", None)
        if not maze_workflow_id or not isinstance(final_output_refs, dict):
            raise ValueError("GAIA workflow template is missing its submission contract")
        encoded_final_output_refs = _encode_output_refs(final_output_refs)
        journal, resumed = validation_state.load_or_create_submission(
            sample,
            maze_workflow_id=maze_workflow_id,
            final_output_refs=encoded_final_output_refs,
        )
        result["sample_ref"] = str(journal["sample_ref"])
        update_trace_ids(
            candidate_playground_run_id=journal.get("playground_run_id"),
            candidate_maze_run_id=journal.get("maze_run_id"),
        )
        restore_capability()

        inputs: dict[str, object] = {
            "dag_id": sample.dag_id,
            "question": sample.question,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
        }
        execution_file = None
        if sample.workflow == "file":
            execution_file = _stage_validation_execution_file(
                sample,
                config.output_dir,
            )
            inputs["supplementary_path"] = execution_file.name

        if resumed and (
            journal.get("submission_state") != "prepared"
            or playground_run_id
            or maze_run_id
        ):
            try:
                recovered = lookup_submission()
            except PlaygroundGaiaError as lookup_error:
                update_trace_ids(
                    candidate_playground_run_id=lookup_error.playground_run_id,
                    candidate_maze_run_id=lookup_error.maze_run_id,
                    submission_state="indeterminate",
                )
            else:
                terminal = terminal_without_maze_run(
                    recovered,
                    "persisted Playground submission ended without a Maze run",
                )
                if terminal is not None:
                    return terminal

        if not maze_run_id:
            update_trace_ids(submission_state="submitting")
            try:
                submitted_playground_run_id, submitted_maze_run_id = (
                    playground_client.submit_run(
                        workflow=sample.workflow,
                        sample_ref=str(journal["sample_ref"]),
                        maze_workflow_id=str(journal["maze_workflow_id"]),
                        final_output_refs=dict(journal["final_output_refs"]),
                        inputs=inputs,
                        timeout_seconds=config.timeout,
                        execution_file=execution_file,
                        submission_token=str(journal["submission_token"]),
                    )
                )
            except PlaygroundGaiaError as exc:
                update_trace_ids(
                    candidate_playground_run_id=exc.playground_run_id,
                    candidate_maze_run_id=exc.maze_run_id,
                    submission_state="indeterminate",
                )
                restore_capability()
                recovered = None
                recovery_error = None
                try:
                    recovered = lookup_submission()
                except Exception as lookup_error:
                    recovery_error = lookup_error
                    update_trace_ids(
                        candidate_playground_run_id=getattr(
                            lookup_error,
                            "playground_run_id",
                            "",
                        ),
                        candidate_maze_run_id=getattr(
                            lookup_error,
                            "maze_run_id",
                            "",
                        ),
                        submission_state="indeterminate",
                    )
                terminal = terminal_without_maze_run(
                    recovered,
                    f"{type(exc).__name__}: {exc}",
                )
                if terminal is not None:
                    return terminal
                if not maze_run_id and playground_run_id:
                    restore_capability()
                    try:
                        canceled = playground_client.cancel_run(
                            playground_run_id,
                            outcome="canceled",
                        )
                        trace_finalized = True
                        update_trace_ids(
                            public_status=canceled.get("status"),
                            submission_state="terminal",
                        )
                    except Exception as cancel_error:
                        recovery_error = recovery_error or cancel_error
                if not maze_run_id:
                    result["status"] = "failed"
                    result["error"] = f"{type(exc).__name__}: {exc}"
                    if recovery_error is not None:
                        result["error"] += (
                            "; submission recovery remains incomplete: "
                            f"{type(recovery_error).__name__}: {recovery_error}"
                        )
                    return _SampleOutcome(result, complete=trace_finalized)
            else:
                update_trace_ids(
                    candidate_playground_run_id=submitted_playground_run_id,
                    candidate_maze_run_id=submitted_maze_run_id,
                    submission_state="bound",
                )
                restore_capability()

        try:
            run = client.wait_run(maze_run_id, timeout=config.timeout)
        except TimeoutError:
            try:
                finalized = playground_client.cancel_run(
                    playground_run_id,
                    outcome="timed_out",
                )
                trace_finalized = True
            except Exception as exc:
                result["status"] = "failed"
                result["error"] = (
                    f"run exceeded timeout of {config.timeout} seconds; "
                    "Playground timeout finalization failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                update_trace_ids(submission_state="indeterminate")
                return _SampleOutcome(result, complete=False)
            # The benchmark deadline was exceeded even if Core completed while
            # the cancellation request was in flight. Keep the authoritative
            # Core/Playground status in its trace, but do not count this sample
            # as a successful benchmark run.
            result["status"] = "timed_out"
            result["error"] = f"run exceeded timeout of {config.timeout} seconds"
            update_trace_ids(
                public_status=finalized.get("status"),
                submission_state="terminal",
            )
            return _SampleOutcome(result, complete=True)

        status = str(run.get("status", "failed"))
        result["status"] = status
        try:
            playground_client.finish_run(playground_run_id, status)
            trace_finalized = True
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = (
                "Playground trace finalization failed: "
                f"{type(exc).__name__}: {exc}"
            )
            update_trace_ids(submission_state="indeterminate")
            return _SampleOutcome(result, complete=False)
        update_trace_ids(submission_state="terminal")
        if status != "succeeded":
            result["error"] = _run_error(run)
            return _SampleOutcome(result, complete=True)
        summary = run.get("result_summary")
        if not isinstance(summary, dict) or not isinstance(
            summary.get("final_answer"),
            str,
        ):
            result["error"] = "succeeded run is missing result_summary.final_answer"
            return _SampleOutcome(result, complete=True)
        raw_answer = summary["final_answer"]
        result["raw_answer"] = raw_answer
        prediction = extract_final_answer(raw_answer)
        if prediction is None:
            result["error"] = f"model output is missing a non-empty {FINAL_ANSWER_MARKER}"
            return _SampleOutcome(result, complete=True)
        result["prediction"] = prediction
        result["parsed"] = True
        result["correct"] = question_scorer(prediction, sample.expected)
        return _SampleOutcome(result, complete=True)
    except PlaygroundGaiaError as exc:
        playground_run_id = playground_run_id or exc.playground_run_id
        maze_run_id = maze_run_id or exc.maze_run_id
        result["playground_run_id"] = playground_run_id
        result["maze_run_id"] = maze_run_id
        result["run_id"] = maze_run_id
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        if journal is not None:
            update_trace_ids(submission_state="indeterminate")
        return _SampleOutcome(result, complete=False)
    except Exception as exc:
        result["playground_run_id"] = playground_run_id
        result["maze_run_id"] = maze_run_id
        result["run_id"] = maze_run_id
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        cancellation_verified = False
        if playground_run_id and not trace_finalized:
            restore_capability()
            try:
                canceled = playground_client.cancel_run(
                    playground_run_id,
                    outcome="canceled",
                )
                cancellation_verified = True
                if journal is not None:
                    update_trace_ids(
                        public_status=canceled.get("status"),
                        submission_state="terminal",
                    )
            except Exception as finalize_exc:
                result["error"] += (
                    "; Playground cancellation failed: "
                    f"{type(finalize_exc).__name__}: {finalize_exc}"
                )
        if journal is not None and not cancellation_verified:
            update_trace_ids(submission_state="indeterminate")
        return _SampleOutcome(
            result,
            complete=cancellation_verified or not playground_run_id,
        )
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
    _atomic_write_json(path, value)


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    _atomic_write_jsonl(path, records)


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def run_validation(config: ValidationConfig) -> dict[str, object]:
    config = replace(
        config,
        data_root=config.data_root.expanduser(),
        output_dir=Path(os.path.abspath(config.output_dir.expanduser())),
    )
    config.validate()
    config = replace(config, data_root=config.data_root.resolve(strict=True))
    samples = load_validation_samples(config.data_root)
    selected = samples[: config.limit] if config.limit is not None else samples
    identity_sha256, sample_identities = _validation_identity(config, selected)
    validation_state = _ValidationState(
        config.output_dir,
        identity_sha256,
        sample_identities,
    )
    private_workspaces_dir = config.output_dir / "workspaces"
    _ensure_private_directory(private_workspaces_dir)

    results: list[dict[str, object]] = []
    runnable: list[GaiaSample] = []
    incomplete_results: dict[int, dict[str, object]] = {}
    for sample in selected:
        persisted = validation_state.load_result(sample)
        if persisted is not None and persisted[1]:
            results.append(persisted[0])
            continue
        if persisted is not None:
            incomplete_results[sample.query_index] = persisted[0]

        result = _empty_result(sample)
        if sample.skip_reason:
            result["status"] = "skipped"
            result["error"] = sample.skip_reason
            results.append(
                validation_state.save_result(
                    sample,
                    _SampleOutcome(result, complete=True),
                )
            )
        elif sample.load_error:
            result["error"] = sample.load_error
            results.append(
                validation_state.save_result(
                    sample,
                    _SampleOutcome(result, complete=True),
                )
            )
        else:
            runnable.append(sample)

    if runnable:
        client = MaClient(
            config.server_url,
            request_timeout=min(config.timeout, 60.0),
        )
        playground_client = PlaygroundGaiaClient(
            config.playground_url,
            config.playground_workspace_id,
            request_timeout=min(config.timeout, 60.0),
        )
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
                template_errors[workflow_name] = (
                    f"template setup failed: {type(exc).__name__}: {exc}"
                )

        pending: list[GaiaSample] = []
        for sample in runnable:
            error = template_errors.get(sample.workflow)
            if not error:
                pending.append(sample)
                continue
            result = _empty_result(sample)
            prior_result = incomplete_results.get(sample.query_index)
            if prior_result is not None:
                for key in (
                    "sample_ref",
                    "playground_run_id",
                    "maze_run_id",
                    "run_id",
                ):
                    result[key] = prior_result.get(key, result[key])
            result["error"] = error
            results.append(
                validation_state.save_result(
                    sample,
                    _SampleOutcome(result, complete=False),
                )
            )

    if runnable and pending:
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
                    playground_client=playground_client,
                    config=config,
                    validation_state=validation_state,
                ): sample
                for sample in pending
            }
            for future in as_completed(futures):
                sample = futures[future]
                results.append(
                    validation_state.save_result(sample, future.result())
                )

    results.sort(key=lambda item: int(item["query_index"]))
    skipped = sum(item["status"] == "skipped" for item in results)
    supported = len(results) - skipped
    submitted = sum(bool(item["maze_run_id"]) for item in results)
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
            "playground_url": config.playground_url,
            "playground_workspace_id": config.playground_workspace_id,
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
    parser.add_argument("--playground-url", default="http://127.0.0.1:3001")
    parser.add_argument("--playground-workspace-id", default="default")
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
        playground_url=args.playground_url,
        playground_workspace_id=args.playground_workspace_id,
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
    public_summary = {
        key: summary[key]
        for key in (
            "discovered",
            "selected",
            "supported",
            "submitted",
            "succeeded",
            "parsed",
            "correct",
            "skipped",
            "run_success_rate",
            "parse_rate",
            "supported_subset_accuracy",
        )
    }
    scorer = summary.get("scorer")
    if isinstance(scorer, dict):
        public_summary["scorer_revision"] = scorer.get("revision")
    print(json.dumps(public_summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
