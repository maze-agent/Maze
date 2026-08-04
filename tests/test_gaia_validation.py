from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from workflows.gaia.scorer import question_scorer
from workflows.gaia.validation import (
    FINAL_ANSWER_MARKER,
    PlaygroundGaiaClient,
    PlaygroundGaiaError,
    ValidationConfig,
    _parse_args,
    extract_final_answer,
    load_validation_samples,
    main,
    run_validation,
)


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _dataset(tmp_path: Path) -> Path:
    root = tmp_path / "gaia"
    validation = root / "2023" / "validation"
    validation.mkdir(parents=True)
    (validation / "document.txt").write_text("FILE_INPUT_ONLY", encoding="utf-8")
    (validation / "unsupported.xlsx").write_bytes(b"not-an-xlsx")
    _write_jsonl(
        root / "gaia_query.jsonl",
        [
            {
                "dag_id": "reason-1",
                "dag_source": "gaia",
                "dag_type": "reason",
                "dag_supplementary_files": [],
            },
            {
                "dag_id": "file-1",
                "dag_source": "gaia",
                "dag_type": "file",
                "dag_supplementary_files": ["document.txt"],
            },
            {
                "dag_id": "file-unsupported",
                "dag_source": "gaia",
                "dag_type": "file",
                "dag_supplementary_files": ["unsupported.xlsx"],
            },
            {
                "dag_id": "speech-1",
                "dag_source": "gaia",
                "dag_type": "speech",
                "dag_supplementary_files": ["audio.mp3"],
            },
            {
                "dag_id": "vision-1",
                "dag_source": "gaia",
                "dag_type": "vision",
                "dag_supplementary_files": ["image.png"],
            },
            {
                "dag_id": "test-split-record",
                "dag_source": "gaia",
                "dag_type": "reason",
                "dag_supplementary_files": [],
            },
        ],
    )
    _write_jsonl(
        validation / "metadata.jsonl",
        [
            {
                "task_id": "reason-1",
                "Question": "Reason question with INPUT_REASON_MARKER",
                "Final answer": "GOLD_REASON_SECRET",
                "file_name": "",
            },
            {
                "task_id": "file-1",
                "Question": "Read the document",
                "Final answer": "GOLD_FILE_SECRET",
                "file_name": "document.txt",
            },
            {
                "task_id": "file-unsupported",
                "Question": "Read the spreadsheet",
                "Final answer": "GOLD_XLSX_SECRET",
                "file_name": "unsupported.xlsx",
            },
            {
                "task_id": "speech-1",
                "Question": "Transcribe audio",
                "Final answer": "GOLD_SPEECH_SECRET",
                "file_name": "audio.mp3",
            },
            {
                "task_id": "vision-1",
                "Question": "Inspect image",
                "Final answer": "GOLD_VISION_SECRET",
                "file_name": "image.png",
            },
        ],
    )
    return root


def _reason_dataset(tmp_path: Path, dag_ids: list[str]) -> Path:
    root = tmp_path / "gaia"
    validation = root / "2023" / "validation"
    _write_jsonl(
        root / "gaia_query.jsonl",
        [
            {
                "dag_id": dag_id,
                "dag_source": "gaia",
                "dag_type": "reason",
                "dag_supplementary_files": [],
            }
            for dag_id in dag_ids
        ],
    )
    _write_jsonl(
        validation / "metadata.jsonl",
        [
            {
                "task_id": dag_id,
                "Question": f"Question for {dag_id}",
                "Final answer": f"ANSWER_{dag_id}",
                "file_name": "",
            }
            for dag_id in dag_ids
        ],
    )
    return root


def _required_cli_args(tmp_path: Path) -> list[str]:
    return [
        "--server-url",
        "http://maze.test",
        "--data-root",
        str(tmp_path / "gaia"),
        "--output-dir",
        str(tmp_path / "report"),
        "--base-url",
        "http://model.test/v1",
        "--model",
        "local-model",
    ]


def test_cli_defaults_to_all_selected_runs_in_flight(tmp_path):
    args = _parse_args(_required_cli_args(tmp_path))

    assert args.max_in_flight_runs is None
    assert args.max_tokens == 2048
    assert args.playground_url == "http://127.0.0.1:3001"
    assert args.playground_workspace_id == "default"


def test_cli_does_not_keep_concurrency_alias(tmp_path):
    with pytest.raises(SystemExit):
        _parse_args([*_required_cli_args(tmp_path), "--concurrency", "2"])


@pytest.mark.parametrize("max_in_flight_runs", [0, -1])
def test_config_rejects_non_positive_in_flight_limit(
    tmp_path,
    max_in_flight_runs,
):
    config = ValidationConfig(
        server_url="http://maze.test",
        data_root=tmp_path / "gaia",
        output_dir=tmp_path / "report",
        base_url="http://model.test/v1",
        model="local-model",
        max_in_flight_runs=max_in_flight_runs,
    )

    with pytest.raises(ValueError, match="max_in_flight_runs must be positive"):
        config.validate()


@pytest.mark.parametrize(
    ("prediction", "expected", "correct"),
    [
        ("$1,234%", "1234", True),
        ("1.0e3", "1000", True),
        ("1.0000001", "1", False),
        ("12%", "0.12", False),
        ("Sea-gull!", "sea gull", True),
        ("the Eiffel Tower", "Eiffel Tower", False),
        ("foo\N{EM DASH}bar", "foobar", False),
        (None, "None", True),
        ("new york,2.0", "New York; 2", True),
        ("B,A", "A,B", False),
        ("A,B", "A.,B", False),
    ],
)
def test_official_gaia_scorer_semantics(prediction, expected, correct):
    assert question_scorer(prediction, expected) is correct


def test_official_gaia_scorer_warns_for_different_list_lengths():
    with pytest.warns(UserWarning, match="different lengths"):
        assert question_scorer("A,B,C", "A,B") is False
    with pytest.warns(UserWarning, match="different lengths"):
        assert question_scorer("1,000;2", "1000;2") is False


def test_extract_final_answer_uses_last_exact_marker():
    raw = (
        "Draft FINAL ANSWER: old\n"
        "More reasoning\n"
        f"{FINAL_ANSWER_MARKER} final value\n"
    )
    assert extract_final_answer(raw) == "final value"
    assert extract_final_answer("final answer: wrong case") is None
    assert extract_final_answer(f"{FINAL_ANSWER_MARKER}   ") is None


def test_loader_selects_validation_and_classifies_unsupported_rows(tmp_path):
    samples = load_validation_samples(_dataset(tmp_path))

    assert [sample.dag_id for sample in samples] == [
        "reason-1",
        "file-1",
        "file-unsupported",
        "speech-1",
        "vision-1",
    ]
    by_id = {sample.dag_id: sample for sample in samples}
    assert by_id["reason-1"].skip_reason == ""
    assert by_id["file-1"].source_file.name == "document.txt"
    assert by_id["file-unsupported"].skip_reason == "unsupported_file_type:.xlsx"
    assert by_id["speech-1"].skip_reason == "unsupported_workflow:speech"
    assert by_id["vision-1"].skip_reason == "unsupported_workflow:vision"


def test_loader_rejects_supplementary_path_escape(tmp_path):
    root = _dataset(tmp_path)
    queries = [
        {
            "dag_id": "file-1",
            "dag_source": "gaia",
            "dag_type": "file",
            "dag_supplementary_files": ["../document.txt"],
        }
    ]
    _write_jsonl(root / "gaia_query.jsonl", queries)

    sample = load_validation_samples(root)[0]

    assert "stay within validation data" in sample.load_error
    assert sample.source_file is None


class _FakeTemplate:
    def __init__(self, workflow: str, client: "_FakeClient"):
        self.workflow = workflow
        self.client = client
        self.workflow_id = f"maze-workflow-{workflow}"
        self.final_output_refs = {
            "final_answer": {
                "__maze_output_ref__": True,
                "task_id": f"final-task-{workflow}",
                "output_key": "final_answer",
            }
        }

    def run(self, **kwargs):
        with self.client.lock:
            run_id = f"run-{len(self.client.submissions) + 1}"
            self.client.submissions.append((self.workflow, run_id, kwargs))
        return run_id


class _FakeClient:
    instances: list["_FakeClient"] = []
    current: "_FakeClient | None" = None

    def __init__(self, server_url: str, request_timeout: float | None = None):
        self.server_url = server_url
        self.request_timeout = request_timeout
        self.templates = []
        self.submissions = []
        self.lock = threading.Lock()
        type(self).instances.append(self)
        _FakeClient.current = self

    def create_workflow_from(self, definition, inputs):
        workflow = "file" if definition.__name__ == "gaia_file" else "reason"
        self.templates.append((workflow, dict(inputs)))
        return _FakeTemplate(workflow, self)

    def wait_run(self, run_id: str, timeout: float):
        with self.lock:
            workflow, _, kwargs = next(
                item for item in self.submissions if item[1] == run_id
            )
        marker = "FILE" if workflow == "file" else "REASON"
        return {
            "run_id": run_id,
            "status": "succeeded",
            "result_summary": {"final_answer": f"{FINAL_ANSWER_MARKER} GOLD_{marker}_SECRET"},
            "run_inputs": kwargs["inputs"],
            "metadata": {},
        }


class _FakePlaygroundClient:
    instances: list["_FakePlaygroundClient"] = []

    def __init__(self, base_url: str, workspace_id: str, request_timeout: float):
        self.base_url = base_url
        self.workspace_id = workspace_id
        self.request_timeout = request_timeout
        self.lock = threading.Lock()
        self.public_records = []
        self.submission_tokens = []
        self.lookup_calls = []
        self.runs_by_submission = {}
        self.finished = []
        self.cancelled = []
        type(self).instances.append(self)

    def submit_run(
        self,
        *,
        workflow,
        sample_ref,
        maze_workflow_id,
        final_output_refs,
        inputs,
        timeout_seconds,
        execution_file=None,
        submission_token=None,
    ):
        core = _FakeClient.current
        assert core is not None
        execution_path = Path(execution_file) if execution_file is not None else None
        execution_content = execution_path.read_bytes() if execution_path else None
        with self.lock, core.lock:
            ordinal = len(core.submissions) + 1
            playground_run_id = f"playground-{ordinal}"
            maze_run_id = f"run-{ordinal}"
            core.submissions.append(
                (
                    workflow,
                    maze_run_id,
                    {
                        "inputs": dict(inputs),
                        "timeout_seconds": timeout_seconds,
                        "workspace_dir": (
                            str(execution_path.parent.parent) if execution_path else None
                        ),
                        "artifact_mode": execution_path is not None,
                        "execution_file": (
                            {
                                "name": execution_path.name,
                                "contentBase64": base64.b64encode(
                                    execution_content
                                ).decode("ascii"),
                                "sha256": hashlib.sha256(execution_content).hexdigest(),
                            }
                            if execution_path
                            else None
                        ),
                        "maze_workflow_id": maze_workflow_id,
                        "final_output_refs": final_output_refs,
                    },
                )
            )
            self.public_records.append(
                {
                    "benchmark": "gaia",
                    "workflow": workflow,
                    "sample_ref": sample_ref,
                    "playground_run_id": playground_run_id,
                }
            )
            self.submission_tokens.append(submission_token)
            self.runs_by_submission[(sample_ref, submission_token)] = {
                "playgroundRunId": playground_run_id,
                "mazeRunId": maze_run_id,
                "status": "running",
            }
        return playground_run_id, maze_run_id

    def lookup_run(
        self,
        sample_ref,
        submission_token,
        *,
        require_maze_run_id=False,
    ):
        self.lookup_calls.append((sample_ref, submission_token, require_maze_run_id))
        run = self.runs_by_submission.get((sample_ref, submission_token))
        if run is None:
            raise PlaygroundGaiaError("GAIA submission not found", status_code=404)
        if require_maze_run_id and not run["mazeRunId"]:
            raise PlaygroundGaiaError(
                "GAIA submission has no Maze run",
                playground_run_id=run["playgroundRunId"],
                retryable=True,
            )
        return dict(run)

    def restore_capability(self, playground_run_id, sample_ref, submission_token):
        return None

    def _set_status(self, playground_run_id, status):
        for run in self.runs_by_submission.values():
            if run["playgroundRunId"] == playground_run_id:
                run["status"] = status

    def finish_run(self, playground_run_id, status):
        with self.lock:
            self.finished.append((playground_run_id, status))
            public_status = {
                "succeeded": "completed",
                "cancelled": "canceled",
            }.get(status, status)
            self._set_status(playground_run_id, public_status)
        return {
            "playgroundRunId": playground_run_id,
            "status": public_status,
        }

    def cancel_run(self, playground_run_id, outcome="canceled"):
        with self.lock:
            self.cancelled.append((playground_run_id, outcome))
            self._set_status(playground_run_id, outcome)
        return {"playgroundRunId": playground_run_id, "status": outcome}


def _install_fake_clients(
    monkeypatch,
    maze_client=_FakeClient,
    playground_client=_FakePlaygroundClient,
):
    maze_client.instances = []
    _FakeClient.current = None
    playground_client.instances = []
    monkeypatch.setattr("workflows.gaia.validation.MaClient", maze_client)
    monkeypatch.setattr(
        "workflows.gaia.validation.PlaygroundGaiaClient",
        playground_client,
    )


@pytest.mark.parametrize(
    ("max_in_flight_runs", "expected_workers"),
    [(None, 4), (2, 2), (10, 4)],
)
def test_runner_sizes_client_in_flight_window(
    tmp_path,
    monkeypatch,
    max_in_flight_runs,
    expected_workers,
):
    root = _reason_dataset(tmp_path, ["one", "two", "three", "four"])
    _install_fake_clients(monkeypatch)
    worker_counts = []

    def recording_executor(*args, **kwargs):
        max_workers = kwargs.get("max_workers", args[0] if args else None)
        worker_counts.append(max_workers)
        return ThreadPoolExecutor(*args, **kwargs)

    monkeypatch.setattr(
        "workflows.gaia.validation.ThreadPoolExecutor",
        recording_executor,
    )

    summary = run_validation(
        ValidationConfig(
            server_url="http://maze.test",
            data_root=root,
            output_dir=tmp_path / "report",
            base_url="http://model.test/v1",
            model="local-model",
            max_in_flight_runs=max_in_flight_runs,
        )
    )

    assert worker_counts == [expected_workers]
    assert summary["submitted"] == 4
    assert summary["config"]["max_in_flight_runs"] == max_in_flight_runs
    assert "concurrency" not in summary["config"]


def test_runner_keeps_gold_out_of_maze_and_counts_skips(tmp_path, monkeypatch):
    root = _dataset(tmp_path)
    output = tmp_path / "report"
    _install_fake_clients(monkeypatch)

    summary = run_validation(
        ValidationConfig(
            server_url="http://maze.test",
            data_root=root,
            output_dir=output,
            base_url="http://model.test/v1",
            model="local-model",
            playground_workspace_id="gaia-validation",
            api_key="env:MODEL_KEY",
            max_in_flight_runs=2,
        )
    )

    assert summary == {
        **summary,
        "discovered": 5,
        "selected": 5,
        "supported": 2,
        "submitted": 2,
        "succeeded": 2,
        "parsed": 2,
        "correct": 2,
        "skipped": 3,
        "run_success_rate": 1.0,
        "parse_rate": 1.0,
        "supported_subset_accuracy": 1.0,
    }
    client = _FakeClient.instances[0]
    assert client.request_timeout == min(float(summary["config"]["timeout"]), 60.0)
    assert [item[0] for item in client.templates] == ["file", "reason"]
    assert len(client.submissions) == 2
    serialized_submissions = json.dumps(client.submissions)
    assert "GOLD_REASON_SECRET" not in serialized_submissions
    assert "GOLD_FILE_SECRET" not in serialized_submissions
    assert all("metadata" not in kwargs for _, _, kwargs in client.submissions)

    playground = _FakePlaygroundClient.instances[0]
    assert playground.workspace_id == "gaia-validation"
    assert len(playground.public_records) == 2
    assert all(
        set(record) == {
            "benchmark",
            "workflow",
            "sample_ref",
            "playground_run_id",
        }
        for record in playground.public_records
    )
    serialized_public_records = json.dumps(playground.public_records)
    for private_value in (
        "reason-1",
        "file-1",
        "INPUT_REASON_MARKER",
        "FILE_INPUT_ONLY",
        "GOLD_REASON_SECRET",
        "GOLD_FILE_SECRET",
        FINAL_ANSWER_MARKER,
    ):
        assert private_value not in serialized_public_records

    file_submission = next(item for item in client.submissions if item[0] == "file")
    file_kwargs = file_submission[2]
    assert file_kwargs["artifact_mode"] is True
    workspace = Path(file_kwargs["workspace_dir"])
    assert (workspace / "files" / "document.txt").read_text() == "FILE_INPUT_ONLY"
    assert file_kwargs["inputs"]["supplementary_path"] == "document.txt"
    assert file_kwargs["execution_file"] == {
        "name": "document.txt",
        "contentBase64": base64.b64encode(b"FILE_INPUT_ONLY").decode("ascii"),
        "sha256": hashlib.sha256(b"FILE_INPUT_ONLY").hexdigest(),
    }

    results = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines()]
    assert [item["status"] for item in results] == [
        "succeeded",
        "succeeded",
        "skipped",
        "skipped",
        "skipped",
    ]
    assert {item["error"] for item in results[2:]} == {
        "unsupported_file_type:.xlsx",
        "unsupported_workflow:speech",
        "unsupported_workflow:vision",
    }
    for item in results[:2]:
        assert item["sample_ref"].startswith("gaia-")
        assert item["playground_run_id"].startswith("playground-")
        assert item["maze_run_id"] == item["run_id"]
        assert item["playground_run_id"] != item["maze_run_id"]
    persisted_summary = json.loads((output / "summary.json").read_text())
    assert persisted_summary == summary
    assert "MODEL_KEY" not in (output / "summary.json").read_text()
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE((output / "workspaces").stat().st_mode) == 0o700
    assert stat.S_IMODE((output / "results.jsonl").stat().st_mode) == 0o600
    assert stat.S_IMODE((output / "summary.json").stat().st_mode) == 0o600


class _SubmissionFailurePlaygroundClient(_FakePlaygroundClient):
    def submit_run(self, *, workflow, sample_ref, **_kwargs):
        playground_run_id = "playground-retained-after-submit-failure"
        self.public_records.append(
            {
                "benchmark": "gaia",
                "workflow": workflow,
                "sample_ref": sample_ref,
                "playground_run_id": playground_run_id,
            }
        )
        raise PlaygroundGaiaError(
            "Maze workflow submission failed",
            playground_run_id=playground_run_id,
        )


def test_runner_retains_playground_id_when_maze_submission_fails(
    tmp_path,
    monkeypatch,
):
    root = _reason_dataset(tmp_path, ["submit-failure"])
    _install_fake_clients(
        monkeypatch,
        playground_client=_SubmissionFailurePlaygroundClient,
    )
    output = tmp_path / "report"

    summary = run_validation(
        ValidationConfig(
            server_url="http://maze.test",
            data_root=root,
            output_dir=output,
            base_url="http://model.test/v1",
            model="local-model",
        )
    )

    assert summary["submitted"] == 0
    result = json.loads((output / "results.jsonl").read_text())
    assert result["playground_run_id"] == "playground-retained-after-submit-failure"
    assert result["maze_run_id"] == ""
    assert result["run_id"] == ""
    assert result["status"] == "failed"


class _FinalizationFailurePlaygroundClient(_FakePlaygroundClient):
    def finish_run(self, playground_run_id, status):
        raise PlaygroundGaiaError(
            "terminal trace could not be verified",
            playground_run_id=playground_run_id,
            transport_failure=True,
        )


def test_runner_surfaces_playground_finalization_failure(tmp_path, monkeypatch):
    root = _reason_dataset(tmp_path, ["finalization-failure"])
    _install_fake_clients(
        monkeypatch,
        playground_client=_FinalizationFailurePlaygroundClient,
    )
    output = tmp_path / "report"

    summary = run_validation(
        ValidationConfig(
            server_url="http://maze.test",
            data_root=root,
            output_dir=output,
            base_url="http://model.test/v1",
            model="local-model",
        )
    )

    assert summary["submitted"] == 1
    assert summary["succeeded"] == 0
    result = json.loads((output / "results.jsonl").read_text())
    assert result["status"] == "failed"
    assert "Playground trace finalization failed" in result["error"]


class _MixedResultClient(_FakeClient):
    def wait_run(self, run_id: str, timeout: float):
        with self.lock:
            _, _, kwargs = next(item for item in self.submissions if item[1] == run_id)
        dag_id = kwargs["inputs"]["dag_id"]
        if dag_id == "failed":
            return {
                "run_id": run_id,
                "status": "failed",
                "error_summary": {"error_type": "model_error"},
            }
        if dag_id == "unparsed":
            answer = "answer without the required marker"
        else:
            answer = f"{FINAL_ANSWER_MARKER} ANSWER_{dag_id}"
        return {
            "run_id": run_id,
            "status": "succeeded",
            "result_summary": {"final_answer": answer},
        }


def test_failures_and_unparsed_answers_remain_in_accuracy_denominator(
    tmp_path,
    monkeypatch,
):
    root = _reason_dataset(tmp_path, ["failed", "unparsed", "correct"])
    _install_fake_clients(monkeypatch, _MixedResultClient)

    summary = run_validation(
        ValidationConfig(
            server_url="http://maze.test",
            data_root=root,
            output_dir=tmp_path / "report",
            base_url="http://model.test/v1",
            model="local-model",
            max_in_flight_runs=3,
        )
    )

    assert summary["supported"] == 3
    assert summary["submitted"] == 3
    assert summary["succeeded"] == 2
    assert summary["parsed"] == 1
    assert summary["correct"] == 1
    assert summary["run_success_rate"] == 0.666667
    assert summary["parse_rate"] == 0.333333
    assert summary["supported_subset_accuracy"] == 0.333333


class _TimeoutClient(_FakeClient):
    def __init__(self, server_url: str, request_timeout: float | None = None):
        super().__init__(server_url, request_timeout=request_timeout)
        self.cancelled = []

    def wait_run(self, run_id: str, timeout: float):
        raise TimeoutError(run_id)

    def cancel_run(self, run_id: str, reason: str):
        self.cancelled.append((run_id, reason))


def test_runner_cancels_run_after_wait_timeout(tmp_path, monkeypatch):
    root = _reason_dataset(tmp_path, ["slow"])
    _install_fake_clients(monkeypatch, _TimeoutClient)

    summary = run_validation(
        ValidationConfig(
            server_url="http://maze.test",
            data_root=root,
            output_dir=tmp_path / "report",
            base_url="http://model.test/v1",
            model="local-model",
            timeout=0.01,
        )
    )

    client = _TimeoutClient.instances[0]
    assert client.cancelled == []
    playground = _FakePlaygroundClient.instances[0]
    assert playground.cancelled == [("playground-1", "timed_out")]
    assert playground.finished == []
    assert summary["submitted"] == 1
    assert summary["succeeded"] == 0
    record = json.loads((tmp_path / "report" / "results.jsonl").read_text())
    assert record["status"] == "timed_out"
    assert record["correct"] is False


class _CompletedDuringTimeoutPlaygroundClient(_FakePlaygroundClient):
    def cancel_run(self, playground_run_id, outcome="canceled"):
        self.cancelled.append((playground_run_id, outcome))
        return {"status": "completed"}


def test_runner_timeout_is_not_counted_as_success_when_core_wins_cancel_race(
    tmp_path,
    monkeypatch,
):
    root = _reason_dataset(tmp_path, ["completed-during-timeout"])
    _install_fake_clients(
        monkeypatch,
        _TimeoutClient,
        playground_client=_CompletedDuringTimeoutPlaygroundClient,
    )

    summary = run_validation(
        ValidationConfig(
            server_url="http://maze.test",
            data_root=root,
            output_dir=tmp_path / "report",
            base_url="http://model.test/v1",
            model="local-model",
            timeout=0.01,
        )
    )

    record = json.loads((tmp_path / "report" / "results.jsonl").read_text())
    assert record["status"] == "timed_out"
    assert record["correct"] is False
    assert summary["succeeded"] == 0
    assert summary["run_success_rate"] == 0.0


def test_runner_journals_submission_before_post_and_uses_private_modes(
    tmp_path,
    monkeypatch,
):
    root = _reason_dataset(tmp_path, ["journaled"])
    output = tmp_path / "report"

    class InspectingPlaygroundClient(_FakePlaygroundClient):
        observed_journal = None

        def submit_run(self, *, sample_ref, submission_token=None, **kwargs):
            journal_path = (
                output
                / ".gaia-validation-state"
                / "submissions"
                / "00000.json"
            )
            assert journal_path.is_file()
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            assert journal["sample_ref"] == sample_ref
            assert journal["submission_token"] == submission_token
            assert journal["submission_state"] == "submitting"
            type(self).observed_journal = journal
            return super().submit_run(
                sample_ref=sample_ref,
                submission_token=submission_token,
                **kwargs,
            )

    _install_fake_clients(
        monkeypatch,
        playground_client=InspectingPlaygroundClient,
    )

    run_validation(
        ValidationConfig(
            server_url="http://maze.test",
            data_root=root,
            output_dir=output,
            base_url="http://model.test/v1",
            model="local-model",
        )
    )

    playground = InspectingPlaygroundClient.instances[0]
    assert InspectingPlaygroundClient.observed_journal is not None
    assert playground.submission_tokens == [
        InspectingPlaygroundClient.observed_journal["submission_token"]
    ]
    private_state = output / ".gaia-validation-state"
    for directory in (
        output,
        output / "workspaces",
        private_state,
        private_state / "submissions",
        private_state / "results",
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for private_file in (
        output / "results.jsonl",
        output / "summary.json",
        private_state / "manifest.json",
        private_state / "submissions" / "00000.json",
        private_state / "results" / "00000.json",
    ):
        assert stat.S_IMODE(private_file.stat().st_mode) == 0o600


def test_runner_persists_indeterminate_submission_for_resume(tmp_path, monkeypatch):
    root = _reason_dataset(tmp_path, ["indeterminate"])
    output = tmp_path / "report"

    class IndeterminatePlaygroundClient(_FakePlaygroundClient):
        submit_calls = 0
        lookup_attempts = 0

        def submit_run(self, **_kwargs):
            type(self).submit_calls += 1
            raise PlaygroundGaiaError(
                "submit response remained unavailable",
                playground_run_id="playground-indeterminate",
                transport_failure=True,
            )

        def lookup_run(self, *_args, **_kwargs):
            type(self).lookup_attempts += 1
            raise PlaygroundGaiaError(
                "lookup remained unavailable",
                playground_run_id="playground-indeterminate",
                transport_failure=True,
            )

        def cancel_run(self, playground_run_id, outcome="canceled"):
            raise PlaygroundGaiaError(
                "cancel remained unavailable",
                playground_run_id=playground_run_id,
                transport_failure=True,
            )

    _install_fake_clients(
        monkeypatch,
        playground_client=IndeterminatePlaygroundClient,
    )

    summary = run_validation(
        ValidationConfig(
            server_url="http://maze.test",
            data_root=root,
            output_dir=output,
            base_url="http://model.test/v1",
            model="local-model",
        )
    )

    result = json.loads((output / "results.jsonl").read_text(encoding="utf-8"))
    journal = json.loads(
        (
            output
            / ".gaia-validation-state"
            / "submissions"
            / "00000.json"
        ).read_text(encoding="utf-8")
    )
    private_result = json.loads(
        (
            output
            / ".gaia-validation-state"
            / "results"
            / "00000.json"
        ).read_text(encoding="utf-8")
    )
    assert summary["submitted"] == 0
    assert result["playground_run_id"] == "playground-indeterminate"
    assert journal["playground_run_id"] == "playground-indeterminate"
    assert journal["submission_state"] == "indeterminate"
    assert private_result["complete"] is False
    assert IndeterminatePlaygroundClient.submit_calls == 1
    assert IndeterminatePlaygroundClient.lookup_attempts == 1


def test_runner_restart_recovers_by_lookup_and_does_not_rerun_completion(
    tmp_path,
    monkeypatch,
):
    root = _reason_dataset(tmp_path, ["restart"])
    output = tmp_path / "report"

    class RestartPlaygroundClient(_FakePlaygroundClient):
        remote_runs = {}
        submit_calls = 0
        lookup_available = False

        def submit_run(self, *, sample_ref, submission_token=None, **kwargs):
            type(self).submit_calls += 1
            playground_run_id, maze_run_id = super().submit_run(
                sample_ref=sample_ref,
                submission_token=submission_token,
                **kwargs,
            )
            type(self).remote_runs[(sample_ref, submission_token)] = {
                "playgroundRunId": playground_run_id,
                "mazeRunId": maze_run_id,
                "status": "running",
                "workflow": kwargs["workflow"],
                "inputs": dict(kwargs["inputs"]),
            }
            raise PlaygroundGaiaError(
                "submit response was lost",
                playground_run_id=playground_run_id,
                transport_failure=True,
            )

        def lookup_run(
            self,
            sample_ref,
            submission_token,
            *,
            require_maze_run_id=False,
        ):
            self.lookup_calls.append(
                (sample_ref, submission_token, require_maze_run_id)
            )
            if not type(self).lookup_available:
                raise PlaygroundGaiaError(
                    "lookup was temporarily unavailable",
                    transport_failure=True,
                )
            remote = type(self).remote_runs[(sample_ref, submission_token)]
            public_run = {
                "playgroundRunId": remote["playgroundRunId"],
                "mazeRunId": remote["mazeRunId"],
                "status": remote["status"],
            }
            self.runs_by_submission[(sample_ref, submission_token)] = dict(public_run)
            core = _FakeClient.current
            assert core is not None
            with core.lock:
                if not any(
                    item[1] == remote["mazeRunId"] for item in core.submissions
                ):
                    core.submissions.append(
                        (
                            remote["workflow"],
                            remote["mazeRunId"],
                            {"inputs": dict(remote["inputs"])},
                        )
                    )
            return public_run

        def cancel_run(self, playground_run_id, outcome="canceled"):
            if not type(self).lookup_available:
                raise PlaygroundGaiaError(
                    "cancel was temporarily unavailable",
                    playground_run_id=playground_run_id,
                    transport_failure=True,
                )
            return super().cancel_run(playground_run_id, outcome=outcome)

    _install_fake_clients(
        monkeypatch,
        playground_client=RestartPlaygroundClient,
    )
    config = ValidationConfig(
        server_url="http://maze.test",
        data_root=root,
        output_dir=output,
        base_url="http://model.test/v1",
        model="local-model",
    )

    first_summary = run_validation(config)
    assert first_summary["succeeded"] == 0
    assert RestartPlaygroundClient.submit_calls == 1

    RestartPlaygroundClient.lookup_available = True
    second_summary = run_validation(config)
    assert second_summary["succeeded"] == 1
    assert RestartPlaygroundClient.submit_calls == 1
    assert len(RestartPlaygroundClient.instances[1].lookup_calls) == 1

    private_result_path = (
        output / ".gaia-validation-state" / "results" / "00000.json"
    )
    completed_record = private_result_path.read_bytes()
    aggregate_results = (output / "results.jsonl").read_bytes()
    client_count = len(_FakeClient.instances)
    playground_count = len(RestartPlaygroundClient.instances)

    third_summary = run_validation(config)

    assert third_summary["succeeded"] == 1
    assert RestartPlaygroundClient.submit_calls == 1
    assert len(_FakeClient.instances) == client_count
    assert len(RestartPlaygroundClient.instances) == playground_count
    assert private_result_path.read_bytes() == completed_record
    assert (output / "results.jsonl").read_bytes() == aggregate_results


@pytest.mark.parametrize("conflict", ["config", "sample"])
def test_runner_rejects_resume_identity_conflicts(
    tmp_path,
    monkeypatch,
    conflict,
):
    root = _reason_dataset(tmp_path, ["identity"])
    output = tmp_path / "report"
    _install_fake_clients(monkeypatch)
    config = ValidationConfig(
        server_url="http://maze.test",
        data_root=root,
        output_dir=output,
        base_url="http://model.test/v1",
        model="local-model",
    )
    run_validation(config)
    initial_client_count = len(_FakeClient.instances)

    if conflict == "config":
        conflicting_config = ValidationConfig(
            server_url=config.server_url,
            data_root=root,
            output_dir=output,
            base_url=config.base_url,
            model=config.model,
            max_tokens=config.max_tokens + 1,
        )
    else:
        _write_jsonl(
            root / "2023" / "validation" / "metadata.jsonl",
            [
                {
                    "task_id": "identity",
                    "Question": "Question changed after the first run",
                    "Final answer": "ANSWER_identity",
                    "file_name": "",
                }
            ],
        )
        conflicting_config = config

    with pytest.raises(ValueError, match="different GAIA validation"):
        run_validation(conflicting_config)
    assert len(_FakeClient.instances) == initial_client_count


def test_template_failure_is_persisted_as_resumable(tmp_path, monkeypatch):
    root = _reason_dataset(tmp_path, ["template-retry"])
    output = tmp_path / "report"

    class TemplateFailureClient(_FakeClient):
        def create_workflow_from(self, definition, inputs):
            raise RuntimeError("template service unavailable")

    _install_fake_clients(monkeypatch, maze_client=TemplateFailureClient)
    config = ValidationConfig(
        server_url="http://maze.test",
        data_root=root,
        output_dir=output,
        base_url="http://model.test/v1",
        model="local-model",
    )

    first_summary = run_validation(config)
    private_result_path = (
        output / ".gaia-validation-state" / "results" / "00000.json"
    )
    first_result = json.loads(private_result_path.read_text(encoding="utf-8"))
    assert first_summary["submitted"] == 0
    assert first_result["complete"] is False
    assert "template setup failed" in first_result["result"]["error"]

    _install_fake_clients(monkeypatch)
    second_summary = run_validation(config)
    second_result = json.loads(private_result_path.read_text(encoding="utf-8"))
    assert second_summary["succeeded"] == 1
    assert second_result["complete"] is True


def test_runner_rejects_output_directory_symlink(tmp_path, monkeypatch):
    root = _reason_dataset(tmp_path, ["symlink"])
    actual_output = tmp_path / "actual-output"
    actual_output.mkdir()
    output_link = tmp_path / "report"
    output_link.symlink_to(actual_output, target_is_directory=True)
    _install_fake_clients(monkeypatch)

    with pytest.raises(ValueError, match="output_dir must be a real directory"):
        run_validation(
            ValidationConfig(
                server_url="http://maze.test",
                data_root=root,
                output_dir=output_link,
                base_url="http://model.test/v1",
                model="local-model",
            )
        )
    assert not any(actual_output.iterdir())
    assert _FakeClient.instances == []


@pytest.mark.parametrize(
    "api_key",
    ["plaintext-secret", "env:", "env:9INVALID", "env:INVALID-NAME"],
)
def test_runner_rejects_plaintext_or_malformed_api_key_before_clients(
    tmp_path,
    monkeypatch,
    api_key,
):
    root = _reason_dataset(tmp_path, ["secret"])
    output = tmp_path / "report"
    _install_fake_clients(monkeypatch)

    with pytest.raises(ValueError, match="api_key must be empty or an env"):
        run_validation(
            ValidationConfig(
                server_url="http://maze.test",
                data_root=root,
                output_dir=output,
                base_url="http://model.test/v1",
                model="local-model",
                api_key=api_key,
            )
        )
    assert _FakeClient.instances == []
    assert _FakePlaygroundClient.instances == []
    assert not output.exists()


def test_cli_accepts_env_key_without_printing_or_persisting_reference(
    tmp_path,
    monkeypatch,
    capsys,
):
    root = _reason_dataset(tmp_path, ["env-key"])
    output = tmp_path / "report"
    _install_fake_clients(monkeypatch)
    api_key_reference = "env:SUPER_PRIVATE_MODEL_KEY"

    assert main(
        [
            "--server-url",
            "http://maze-private.test",
            "--playground-url",
            "http://playground-private.test",
            "--playground-workspace-id",
            "private-workspace",
            "--data-root",
            str(root),
            "--output-dir",
            str(output),
            "--base-url",
            "http://model-private.test/v1",
            "--model",
            "private-model",
            "--api-key",
            api_key_reference,
        ]
    ) == 0

    stdout = capsys.readouterr().out
    printed = json.loads(stdout)
    assert printed["submitted"] == 1
    assert printed["scorer_revision"]
    for private_value in (
        api_key_reference,
        "SUPER_PRIVATE_MODEL_KEY",
        "maze-private.test",
        "playground-private.test",
        "model-private.test",
        "private-workspace",
        "private-model",
        str(root),
        str(output),
    ):
        assert private_value not in stdout
    for persisted_file in output.rglob("*.json*"):
        persisted = persisted_file.read_text(encoding="utf-8")
        assert api_key_reference not in persisted
        assert "SUPER_PRIVATE_MODEL_KEY" not in persisted


def test_playground_client_recovers_lost_submit_response_with_lookup(monkeypatch):
    client = PlaygroundGaiaClient(
        "http://playground.test",
        "gaia-validation",
        retry_attempts=1,
        retry_delay=0,
    )
    requests = []

    def fake_request(method, path, payload):
        requests.append((method, path, dict(payload)))
        if path == "/api/benchmarks/gaia/runs":
            raise PlaygroundGaiaError(
                "connection closed",
                transport_failure=True,
            )
        assert path == "/api/benchmarks/gaia/runs/lookup"
        return {
            "playgroundRunId": "playground-recovered",
            "mazeRunId": "maze-recovered",
            "status": "running",
        }

    monkeypatch.setattr(client, "_request", fake_request)

    assert client.submit_run(
        workflow="reason",
        sample_ref="gaia-11111111111111111111111111111111",
        maze_workflow_id="workflow-id",
        final_output_refs={"answer": "ref"},
        inputs={"question": "private"},
        timeout_seconds=10,
    ) == ("playground-recovered", "maze-recovered")
    submit_token = requests[0][2]["submissionToken"]
    lookup_token = requests[1][2]["submissionToken"]
    assert submit_token == lookup_token
    assert len(submit_token) == 64
    assert set(submit_token) <= set("0123456789abcdef")


@pytest.mark.parametrize(
    ("action", "lookup_status"),
    [("finish", "completed"), ("cancel", "timed_out")],
)
def test_playground_client_confirms_lost_finalization_response(
    monkeypatch,
    action,
    lookup_status,
):
    client = PlaygroundGaiaClient(
        "http://playground.test",
        "gaia-validation",
        retry_attempts=1,
        retry_delay=0,
    )
    token = "a" * 64
    client._remember_capability("playground-run", "gaia-" + "2" * 32, token)
    requests = []

    def fake_request(method, path, payload):
        requests.append((path, dict(payload)))
        if path.endswith(f"/{action}"):
            raise PlaygroundGaiaError(
                "response was lost",
                transport_failure=True,
            )
        assert path == "/api/benchmarks/gaia/runs/lookup"
        return {
            "playgroundRunId": "playground-run",
            "mazeRunId": "maze-run",
            "status": lookup_status,
        }

    monkeypatch.setattr(client, "_request", fake_request)
    if action == "finish":
        response = client.finish_run("playground-run", "succeeded")
    else:
        response = client.cancel_run("playground-run", outcome="timed_out")
    assert response["status"] == lookup_status
    assert requests[0][1]["submissionToken"] == token
    assert requests[1][1]["submissionToken"] == token


def test_playground_client_retries_accepted_pending_cancellation(monkeypatch):
    client = PlaygroundGaiaClient(
        "http://playground.test",
        "gaia-validation",
        retry_attempts=2,
        retry_delay=0,
    )
    token = "b" * 64
    client._remember_capability("playground-run", "gaia-" + "3" * 32, token)
    requests = []
    cancel_attempts = 0

    def fake_request(method, path, payload):
        nonlocal cancel_attempts
        requests.append((path, dict(payload)))
        if path.endswith("/cancel"):
            cancel_attempts += 1
            return {
                "playgroundRunId": "playground-run",
                "status": "running" if cancel_attempts == 1 else "canceled",
            }
        assert path == "/api/benchmarks/gaia/runs/lookup"
        return {
            "playgroundRunId": "playground-run",
            "mazeRunId": "maze-run",
            "status": "running",
        }

    monkeypatch.setattr(client, "_request", fake_request)

    response = client.cancel_run("playground-run")

    assert response["status"] == "canceled"
    assert [path.rsplit("/", 1)[-1] for path, _ in requests] == [
        "cancel",
        "lookup",
        "cancel",
    ]
    assert all(payload["submissionToken"] == token for _, payload in requests)


def test_playground_backend_persists_public_gaia_trace_before_submission(
    tmp_path,
):
    node_bin = shutil.which("node") or str(Path(sys.executable).with_name("node"))
    backend_dir = Path("web/maze_playground/backend").resolve()
    if not Path(node_bin).is_file() or not (backend_dir / "node_modules/express").is_dir():
        pytest.skip("Playground Node runtime is not installed")

    core_requests = []
    core_get_requests = []
    core_runs = {}
    core_idempotency = {}
    file_observations = {}
    gaia_staging_root = tmp_path / "gaia-staging"
    core_list_failures = {"remaining": 0}
    drop_once = {"remaining": 1}
    file_drop_once = {"remaining": 1}
    cancel_pending_once = {"remaining": 1}
    artifact_body = b"NORMAL_ARTIFACT_BODY"
    artifact_sha = hashlib.sha256(artifact_body).hexdigest()
    hanging_artifact_sha = "f" * 64
    disconnect_artifact_sha = "e" * 64
    hanging_artifact_started = threading.Event()
    release_hanging_artifacts = threading.Event()
    hanging_artifact_requests = {"count": 0}
    disconnect_artifact_started = threading.Event()
    upstream_artifact_disconnected = threading.Event()
    core_lock = threading.Lock()

    class FakeCoreHandler(BaseHTTPRequestHandler):
        def _respond(self, status_code, payload):
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            with core_lock:
                core_requests.append((self.path, payload))
                ordinal = len(
                    [path for path, _ in core_requests if path == "/run_workflow"]
                )
            if self.path == "/run_workflow":
                workflow_id = payload.get("workflow_id")
                idempotency_key = payload.get("idempotency_key")
                idempotency_fingerprint = payload.get("idempotency_fingerprint")
                assert idempotency_key
                assert idempotency_fingerprint
                if workflow_id == "maze-workflow-fail":
                    self._respond(
                        500,
                        {"detail": "CORE_FAILURE_SECRET must not enter Playground"},
                    )
                    return
                if workflow_id == "maze-workflow-drop-once" and drop_once["remaining"]:
                    drop_once["remaining"] -= 1
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                if (
                    workflow_id == "maze-workflow-file-drop-once"
                    and file_drop_once["remaining"]
                ):
                    file_drop_once["remaining"] -= 1
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return

                with core_lock:
                    existing_run_id = core_idempotency.get(idempotency_key)
                    existing_run = core_runs.get(existing_run_id)
                if existing_run is not None:
                    if (
                        existing_run["workflow_id"] != workflow_id
                        or existing_run["idempotency_fingerprint"]
                        != idempotency_fingerprint
                    ):
                        self._respond(
                            409,
                            {"detail": {"code": "workflow_idempotency_conflict"}},
                        )
                        return
                    self._respond(
                        200,
                        {
                            "status": "success",
                            "run_id": existing_run_id,
                            "idempotency_key": idempotency_key,
                            "idempotency_fingerprint": idempotency_fingerprint,
                        },
                    )
                    return

                file_context = payload.get("file_context")
                if file_context:
                    workspace = Path(file_context["workspace_dir"])
                    files_dir = workspace / "files"
                    staged_files = list(files_dir.iterdir())
                    is_file_workflow = workflow_id in {
                        "maze-workflow-file",
                        "maze-workflow-file-drop-once",
                    }
                    assert len(staged_files) == (1 if is_file_workflow else 0)
                    assert file_context["enabled"] is True
                    assert file_context["private"] is True
                    assert file_context["artifact_store"]["private"] is True
                    workspace.resolve().relative_to(gaia_staging_root.resolve())
                    observation = {
                        "workspace": workspace,
                        "workspace_mode": stat.S_IMODE(workspace.stat().st_mode),
                        "files_mode": stat.S_IMODE(files_dir.stat().st_mode),
                        "private": file_context["private"],
                        "artifact_private": file_context["artifact_store"]["private"],
                        "staged_file_count": len(staged_files),
                    }
                    if staged_files:
                        staged_file = staged_files[0]
                        observation.update(
                            {
                                "file_mode": stat.S_IMODE(staged_file.stat().st_mode),
                                "file_name": staged_file.name,
                                "content": staged_file.read_bytes(),
                            }
                        )
                    file_observations[workflow_id] = observation

                if workflow_id == "maze-workflow-hang":
                    time.sleep(1)

                run_id = f"maze-{ordinal}"
                if workflow_id == "maze-workflow-already-cancelled":
                    run_status = "cancelled"
                elif workflow_id in {
                        "maze-workflow-cancel",
                        "maze-workflow-timeout",
                        "maze-workflow-running",
                        "maze-workflow-cancel-race-success",
                        "maze-workflow-cancel-pending",
                }:
                    run_status = "running"
                else:
                    run_status = "succeeded"
                run = {
                    "run_id": run_id,
                    "kind": "static",
                    "status": run_status,
                    "metadata": dict(payload.get("metadata") or {}),
                    "workflow_id": workflow_id,
                    "idempotency_key": idempotency_key,
                    "idempotency_fingerprint": idempotency_fingerprint,
                    "run_inputs": payload.get("inputs"),
                    "result_summary": {
                        "final_answer": "RAW_MODEL_SECRET"
                    },
                    "tasks": [{"result": "TASK_RESULT_SECRET"}],
                }
                with core_lock:
                    core_runs[run_id] = run
                    core_idempotency[idempotency_key] = run_id
                self._respond(
                    200,
                    {
                        "status": "success",
                        "run_id": run_id,
                        "idempotency_key": idempotency_key,
                        "idempotency_fingerprint": idempotency_fingerprint,
                    },
                )
                return
            if self.path.startswith("/runs/") and self.path.endswith("/cancel"):
                run_id = self.path.split("/")[2]
                with core_lock:
                    run = core_runs[run_id]
                    if run["workflow_id"] == "maze-workflow-cancel-race-success":
                        run["status"] = "succeeded"
                    elif (
                        run["workflow_id"] == "maze-workflow-cancel-pending"
                        and cancel_pending_once["remaining"]
                    ):
                        cancel_pending_once["remaining"] -= 1
                    elif run["status"] in {"created", "running"}:
                        run["status"] = "cancelled"
                self._respond(
                    200,
                    {
                        "status": "success",
                        "run_id": run_id,
                        "run_status": "cancelled",
                    },
                )
                return
            self._respond(404, {"detail": "not found"})

        def do_GET(self):
            route = self.path.split("?", 1)[0]
            if route == f"/artifacts/sha256/{artifact_sha}":
                with core_lock:
                    core_get_requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(artifact_body)))
                self.end_headers()
                self.wfile.write(artifact_body)
                return
            if route == f"/artifacts/sha256/{hanging_artifact_sha}":
                with core_lock:
                    core_get_requests.append(self.path)
                    hanging_artifact_requests["count"] += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", "1024")
                self.end_headers()
                try:
                    self.wfile.write(b"partial")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                hanging_artifact_started.set()
                release_hanging_artifacts.wait(timeout=5)
                return
            if route == f"/artifacts/sha256/{disconnect_artifact_sha}":
                with core_lock:
                    core_get_requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", "1024")
                self.end_headers()
                try:
                    self.wfile.write(b"partial")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    upstream_artifact_disconnected.set()
                    return
                disconnect_artifact_started.set()
                self.connection.settimeout(0.05)
                while not release_hanging_artifacts.is_set():
                    try:
                        if self.connection.recv(1) == b"":
                            upstream_artifact_disconnected.set()
                            return
                    except socket.timeout:
                        continue
                    except (ConnectionResetError, OSError):
                        upstream_artifact_disconnected.set()
                        return
                return
            with core_lock:
                core_get_requests.append(self.path)
                if route == "/runs":
                    if core_list_failures["remaining"]:
                        core_list_failures["remaining"] -= 1
                        self._respond(500, {"detail": "temporary list failure"})
                        return
                    runs = [dict(run) for run in core_runs.values()]
                    self._respond(200, {"status": "success", "runs": runs})
                    return
                if route == "/cluster/queues":
                    gaia_run_id = next(iter(core_runs), "")
                    self._respond(
                        200,
                        {
                            "counts": {"running": 2},
                            "running_tasks": [
                                {"workflow_id": gaia_run_id, "task_id": "gaia-task"},
                                {"workflow_id": "ordinary-run", "task_id": "ordinary-task"},
                            ],
                        },
                    )
                    return
                if route.startswith("/runs/"):
                    parts = route.strip("/").split("/")
                    run = core_runs.get(parts[1]) if len(parts) >= 2 else None
                    if run is None:
                        self._respond(500, {"detail": "run not found"})
                        return
                    if len(parts) == 2:
                        self._respond(
                            200,
                            {"status": "success", "run": dict(run)},
                        )
                        return
                    self._respond(
                        200,
                        {
                            "status": "success",
                            "run_id": run["run_id"],
                            "tasks": [{"result": "TASK_RESULT_SECRET"}],
                            "events": [{"answer": "RAW_MODEL_SECRET"}],
                            "lines": ["API_KEY_SECRET"],
                            "artifacts": [{"name": "PRIVATE_FILE_NAME.txt"}],
                        },
                    )
                    return
            self._respond(404, {"detail": "not found"})

        def log_message(self, *_args):
            return

    core_server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCoreHandler)
    core_thread = threading.Thread(target=core_server.serve_forever, daemon=True)
    core_thread.start()

    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        backend_port = port_socket.getsockname()[1]

    workspaces_dir = tmp_path / "playground-workspaces"
    env = os.environ.copy()
    env.update(
        {
            "PORT": str(backend_port),
            "MAZE_CORE_URL": (
                f"http://127.0.0.1:{core_server.server_address[1]}"
            ),
            "MAZE_WORKSPACE_ROOT_DIR": str(workspaces_dir),
            "MAZE_WORKSPACES_DIR": str(workspaces_dir),
            "MAZE_DEFAULT_WORKSPACE_ID": "default",
            "MAZE_GAIA_STAGING_ROOT": str(gaia_staging_root),
            "MAZE_GAIA_MAX_FILE_BYTES": "16",
            "MAZE_CORE_REQUEST_TIMEOUT_MS": "200",
            "PYTHON_BIN": sys.executable,
        }
    )
    backend = subprocess.Popen(
        [node_bin, "src/server.js"],
        cwd=backend_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{backend_port}"

    def fetch_run(playground_run_id):
        with urlopen(
            f"{base_url}/api/workflow-runs/static/{playground_run_id}"
            "?workspaceId=gaia-validation",
            timeout=5,
        ) as response:
            return json.loads(response.read())["run"]

    def request_json(path, *, method="GET", payload=None):
        request = Request(
            f"{base_url}{path}",
            data=(
                json.dumps(payload).encode("utf-8")
                if payload is not None
                else None
            ),
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read() or b"{}")
        except HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def request_bytes(path):
        with urlopen(f"{base_url}{path}", timeout=5) as response:
            return response.status, response.headers, response.read()

    def assert_artifact_body_timeout(path, *, method="GET", payload=None):
        started = time.monotonic()
        status_code, response_payload = request_json(
            path,
            method=method,
            payload=payload,
        )
        elapsed = time.monotonic() - started
        assert status_code == 504
        assert "timed out" in response_payload["error"]
        assert elapsed < 0.9

    def stop_playground_backend():
        nonlocal backend
        if backend.poll() is None:
            backend.terminate()
            try:
                backend.wait(timeout=5)
            except subprocess.TimeoutExpired:
                backend.kill()
                backend.wait(timeout=5)
        if backend.stdout and not backend.stdout.closed:
            backend.stdout.close()

    def restart_playground_backend():
        nonlocal backend, base_url
        stop_playground_backend()
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            next_port = port_socket.getsockname()[1]
        env["PORT"] = str(next_port)
        backend = subprocess.Popen(
            [node_bin, "src/server.js"],
            cwd=backend_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        base_url = f"http://127.0.0.1:{next_port}"
        deadline = time.monotonic() + 10
        while True:
            if backend.poll() is not None:
                output = backend.stdout.read() if backend.stdout else ""
                pytest.fail(f"Playground backend exited during restart:\n{output}")
            try:
                with urlopen(f"{base_url}/health", timeout=0.2):
                    return
            except (OSError, URLError):
                if time.monotonic() >= deadline:
                    pytest.fail("Playground backend did not become healthy after restart")
                time.sleep(0.05)

    def gaia_submit_payload(
        *,
        sample_ref,
        submission_token,
        maze_workflow_id,
        workspace_id="gaia-validation",
        workflow="reason",
    ):
        return {
            "workflow": workflow,
            "sampleRef": sample_ref,
            "submissionToken": submission_token,
            "mazeWorkflowId": maze_workflow_id,
            "finalOutputRefs": {"answer": "private-ref"},
            "inputs": {"question": "PRIVATE_QUESTION"},
            "timeoutSeconds": 30,
            "playgroundWorkspaceId": workspace_id,
        }

    try:
        deadline = time.monotonic() + 10
        while True:
            if backend.poll() is not None:
                output = backend.stdout.read() if backend.stdout else ""
                pytest.fail(f"Playground backend exited during startup:\n{output}")
            try:
                with urlopen(f"{base_url}/health", timeout=0.2):
                    break
            except (OSError, URLError):
                if time.monotonic() >= deadline:
                    pytest.fail("Playground backend did not become healthy")
                time.sleep(0.05)

        status_code, headers, downloaded = request_bytes(
            f"/api/artifacts/sha256/{artifact_sha}"
        )
        assert status_code == 200
        assert headers.get_content_type() == "application/octet-stream"
        assert downloaded == artifact_body

        status_code, promoted = request_json(
            "/api/artifacts/promote",
            method="POST",
            payload={
                "workspaceId": "artifact-tests",
                "sha256": artifact_sha,
                "targetPath": "promoted.bin",
            },
        )
        assert status_code == 200
        assert promoted["file"]["relativePath"] == "promoted.bin"
        artifact_workspace = workspaces_dir / "artifact-tests"
        assert (artifact_workspace / "files" / "promoted.bin").read_bytes() == (
            artifact_body
        )

        artifact_run_dir = artifact_workspace / "runs" / "artifact-run"
        artifact_run_dir.mkdir(parents=True)
        (artifact_run_dir / "run.json").write_text(
            json.dumps(
                {
                    "run_id": "artifact-run",
                    "status": "completed",
                    "events": {"count": 0, "last_seq": 0},
                    "task_nodes": {
                        "artifact-task": {
                            "task_id": "artifact-task",
                            "artifacts": [
                                {
                                    "name": "normal.bin",
                                    "path": "normal.bin",
                                    "sha256": artifact_sha,
                                    "mime": "application/octet-stream",
                                },
                                {
                                    "name": "hanging.bin",
                                    "path": "hanging.bin",
                                    "sha256": hanging_artifact_sha,
                                    "mime": "application/octet-stream",
                                },
                            ],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        static_download = (
            "/api/workflow-runs/static/artifact-run/artifacts/download"
            "?workspaceId=artifact-tests&taskId=artifact-task&path=normal.bin"
        )
        status_code, _, downloaded = request_bytes(static_download)
        assert status_code == 200
        assert downloaded == artifact_body

        assert_artifact_body_timeout(
            f"/api/artifacts/sha256/{hanging_artifact_sha}"
        )
        assert hanging_artifact_started.is_set()
        assert_artifact_body_timeout(
            "/api/artifacts/promote",
            method="POST",
            payload={
                "workspaceId": "artifact-tests",
                "sha256": hanging_artifact_sha,
                "targetPath": "must-not-exist.bin",
            },
        )
        assert not (
            artifact_workspace / "files" / "must-not-exist.bin"
        ).exists()
        assert_artifact_body_timeout(
            "/api/workflow-runs/static/artifact-run/artifacts/download"
            "?workspaceId=artifact-tests&taskId=artifact-task&path=hanging.bin"
        )
        with core_lock:
            assert hanging_artifact_requests["count"] == 3

        env["MAZE_CORE_REQUEST_TIMEOUT_MS"] = "5000"
        restart_playground_backend()
        disconnected_client = socket.create_connection(
            ("127.0.0.1", int(env["PORT"])),
            timeout=2,
        )
        try:
            disconnected_client.sendall(
                (
                    f"GET /api/artifacts/sha256/{disconnect_artifact_sha} "
                    "HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{env['PORT']}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
            )
            assert disconnect_artifact_started.wait(timeout=2)
        finally:
            disconnected_client.close()
        assert upstream_artifact_disconnected.wait(timeout=1.5)
        env["MAZE_CORE_REQUEST_TIMEOUT_MS"] = "200"
        restart_playground_backend()

        client = PlaygroundGaiaClient(
            base_url,
            "gaia-validation",
            request_timeout=5,
        )
        private_values = (
            "DAG_ID_SECRET",
            "QUESTION_SECRET",
            "GOLD_SECRET",
            "RAW_MODEL_SECRET",
            "SCORER_SECRET",
            "API_KEY_SECRET",
            "FINAL_TASK_SECRET",
            "CORE_FAILURE_SECRET",
            "TASK_RESULT_SECRET",
        )
        final_refs = {
            "final_answer": {
                "__maze_output_ref__": True,
                "task_id": "FINAL_TASK_SECRET",
                "output_key": "final_answer",
            }
        }
        sample_ref = "gaia-0123456789abcdef0123456789abcdef"
        playground_run_id, maze_run_id = client.submit_run(
            workflow="reason",
            sample_ref=sample_ref,
            maze_workflow_id="maze-workflow-success",
            final_output_refs=final_refs,
            inputs={
                "dag_id": "DAG_ID_SECRET",
                "question": "QUESTION_SECRET",
                "api_key": "API_KEY_SECRET",
                "temperature": 0.0,
                "max_tokens": 16,
            },
            timeout_seconds=30,
        )
        assert playground_run_id != maze_run_id
        finish_response = client.finish_run(playground_run_id, "succeeded")
        assert "mazeRunId" not in finish_response
        assert "maze_run_id" not in json.dumps(finish_response)

        run = fetch_run(playground_run_id)
        assert run["status"] == "completed"
        assert "maze_run_id" not in run
        assert "gaia_private" not in run
        assert run["task_nodes"] == {}
        assert run["error"] is None
        assert run["metadata"] == {
            "benchmark": "gaia",
            "workflow": "reason",
            "sample_ref": sample_ref,
            "playground_run_id": playground_run_id,
        }
        assert "workspace_dir" not in run
        injected_run_path = (
            workspaces_dir
            / "gaia-validation"
            / "runs"
            / playground_run_id
            / "run.json"
        )
        original_snapshot = injected_run_path.read_text(encoding="utf-8")
        injected_snapshot = json.loads(original_snapshot)
        injected_snapshot["workspace_dir"] = "/PRIVATE/ABSOLUTE/PATH"
        injected_snapshot["error"] = "PRIVATE_ERROR_SENTINEL"
        injected_snapshot["final_result"] = {"answer": "PRIVATE_FINAL_SENTINEL"}
        injected_snapshot["task_nodes"] = {
            "private": {"result_summary": "PRIVATE_TASK_SENTINEL"}
        }
        injected_run_path.write_text(json.dumps(injected_snapshot), encoding="utf-8")
        injected_public = fetch_run(playground_run_id)
        injected_serialized = json.dumps(injected_public)
        for sentinel in (
            "/PRIVATE/ABSOLUTE/PATH",
            "PRIVATE_ERROR_SENTINEL",
            "PRIVATE_FINAL_SENTINEL",
            "PRIVATE_TASK_SENTINEL",
        ):
            assert sentinel not in injected_serialized
        injected_run_path.write_text(original_snapshot, encoding="utf-8")
        os.chmod(injected_run_path, 0o600)

        status_code, listed = request_json(
            "/api/workflow-runs/static?workspaceId=gaia-validation"
        )
        assert status_code == 200
        assert "workspaceDir" not in listed
        status_code, event_payload = request_json(
            f"/api/workflow-runs/static/{playground_run_id}/events"
            "?workspaceId=gaia-validation"
        )
        assert status_code == 200
        assert "workspaceDir" not in event_payload
        status_code, detail_payload = request_json(
            f"/api/workflow-runs/static/{playground_run_id}"
            "?workspaceId=gaia-validation"
        )
        assert status_code == 200
        assert "workspaceDir" not in detail_payload
        for public_payload in (run, listed, event_payload):
            serialized = json.dumps(public_payload)
            assert maze_run_id not in serialized
            assert "maze_run_id" not in serialized
            assert "gaia_private" not in serialized
            for private_value in private_values:
                assert private_value not in serialized

        status_code, core_list = request_json("/api/runs?kind=static&detail=true")
        assert status_code == 200
        assert maze_run_id not in json.dumps(core_list)
        status_code, core_detail = request_json(f"/api/runs/{maze_run_id}")
        assert status_code == 404
        assert "RAW_MODEL_SECRET" not in json.dumps(core_detail)
        status_code, core_tasks = request_json(f"/api/runs/{maze_run_id}/tasks")
        assert status_code == 404
        assert "TASK_RESULT_SECRET" not in json.dumps(core_tasks)
        status_code, public_queues = request_json("/api/cluster/queues")
        assert status_code == 200
        assert maze_run_id not in json.dumps(public_queues)
        assert "ordinary-run" in json.dumps(public_queues)
        assert public_queues["counts"]["running"] == 2

        run_dir = workspaces_dir / "gaia-validation" / "runs" / playground_run_id
        public_storage = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (run_dir / "run.json", run_dir / "events.jsonl")
        )
        for private_value in private_values:
            assert private_value not in public_storage
        submission_token = client._capability_for(playground_run_id)[1]
        assert submission_token not in public_storage
        assert stat.S_IMODE((run_dir / "run.json").stat().st_mode) == 0o600
        assert stat.S_IMODE((run_dir / "events.jsonl").stat().st_mode) == 0o600

        with core_lock:
            successful_core_payload = core_requests[0][1]
        assert successful_core_payload["inputs"]["question"] == "QUESTION_SECRET"
        assert successful_core_payload["final_output_refs"] == final_refs
        assert successful_core_payload["metadata"] == {
            "benchmark": "gaia",
            "workflow": "reason",
            "sample_ref": sample_ref,
            "playground_run_id": playground_run_id,
            "maze_run_id": None,
        }
        assert successful_core_payload["idempotency_key"] == (
            "gaia-" + hashlib.sha256(submission_token.encode()).hexdigest()
        )
        assert len(successful_core_payload["idempotency_fingerprint"]) == 64
        assert successful_core_payload["file_context"]["private"] is True
        assert successful_core_payload["file_context"]["artifact_store"]["private"] is True
        reason_observation = file_observations["maze-workflow-success"]
        assert reason_observation["workspace"].resolve().is_relative_to(
            gaia_staging_root.resolve()
        )
        assert {key: value for key, value in reason_observation.items() if key != "workspace"} == {
            "workspace_mode": 0o700,
            "files_mode": 0o700,
            "private": True,
            "artifact_private": True,
            "staged_file_count": 0,
        }

        private_execution_file = tmp_path / "PRIVATE_FILE_NAME.txt"
        private_execution_file.write_bytes(b"FILE_INPUT_ONLY")
        file_playground_run_id, _ = client.submit_run(
            workflow="file",
            sample_ref="gaia-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            maze_workflow_id="maze-workflow-file",
            final_output_refs=final_refs,
            inputs={
                "dag_id": "DAG_ID_SECRET",
                "question": "QUESTION_SECRET",
                "supplementary_path": "PRIVATE_FILE_NAME.txt",
            },
            timeout_seconds=30,
            execution_file=private_execution_file,
        )
        client.finish_run(file_playground_run_id, "succeeded")
        with core_lock:
            file_core_payload = next(
                payload
                for path, payload in core_requests
                if path == "/run_workflow"
                and payload.get("workflow_id") == "maze-workflow-file"
            )
        managed_execution_workspace = Path(
            file_core_payload["file_context"]["workspace_dir"]
        )
        assert managed_execution_workspace.resolve().is_relative_to(
            gaia_staging_root.resolve()
        )
        assert file_core_payload["file_context"]["artifact_store"] == {
            "type": "head_http",
            "base_url": f"http://127.0.0.1:{core_server.server_address[1]}",
            "private": True,
        }
        assert file_core_payload["file_context"]["private"] is True
        observation = file_observations["maze-workflow-file"]
        assert observation["workspace"] == managed_execution_workspace
        assert {key: value for key, value in observation.items() if key != "workspace"} == {
            "workspace_mode": 0o700,
            "files_mode": 0o700,
            "private": True,
            "artifact_private": True,
            "staged_file_count": 1,
            "file_mode": 0o600,
            "file_name": "PRIVATE_FILE_NAME.txt",
            "content": b"FILE_INPUT_ONLY",
        }
        assert not (managed_execution_workspace / "files").exists()
        file_public_run = fetch_run(file_playground_run_id)
        _, file_public_list = request_json(
            "/api/workflow-runs/static?workspaceId=gaia-validation"
        )
        for public_payload in (file_public_run, file_public_list):
            serialized = json.dumps(public_payload)
            assert "FILE_INPUT_ONLY" not in serialized
            assert "PRIVATE_FILE_NAME.txt" not in serialized
            assert "maze_run_id" not in serialized
        file_run_dir = (
            workspaces_dir / "gaia-validation" / "runs" / file_playground_run_id
        )
        file_public_storage = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                file_run_dir / "run.json",
                file_run_dir / "events.jsonl",
            )
        )
        assert str(private_execution_file) not in file_public_storage
        assert "PRIVATE_FILE_NAME.txt" not in file_public_storage
        stale_files_dir = managed_execution_workspace / "files"
        stale_files_dir.mkdir(parents=True)
        stale_file = stale_files_dir / "stale.txt"
        stale_file.write_text("STALE_PRIVATE_INPUT", encoding="utf-8")
        os.chmod(managed_execution_workspace, 0o700)
        os.chmod(stale_files_dir, 0o700)
        os.chmod(stale_file, 0o600)
        restart_playground_backend()
        client.base_url = base_url
        fetch_run(file_playground_run_id)
        assert not managed_execution_workspace.exists()

        with pytest.raises(PlaygroundGaiaError) as failed_submission:
            client.submit_run(
                workflow="reason",
                sample_ref="gaia-fedcba9876543210fedcba9876543210",
                maze_workflow_id="maze-workflow-fail",
                final_output_refs=final_refs,
                inputs={
                    "dag_id": "DAG_ID_SECRET",
                    "question": "QUESTION_SECRET",
                    "api_key": "API_KEY_SECRET",
                },
                timeout_seconds=30,
            )
        failed_playground_run_id = failed_submission.value.playground_run_id
        assert failed_playground_run_id
        assert failed_submission.value.maze_run_id == ""
        assert "CORE_FAILURE_SECRET" not in str(failed_submission.value)
        failed_run = fetch_run(failed_playground_run_id)
        assert failed_run["status"] == "failed"
        assert "maze_run_id" not in failed_run
        assert failed_run["error"] is None
        failed_run_dir = (
            workspaces_dir
            / "gaia-validation"
            / "runs"
            / failed_playground_run_id
        )
        failed_public_storage = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                failed_run_dir / "run.json",
                failed_run_dir / "events.jsonl",
            )
        )
        for private_value in private_values:
            assert private_value not in failed_public_storage

        cancel_playground_run_id, cancel_maze_run_id = client.submit_run(
            workflow="reason",
            sample_ref="gaia-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            maze_workflow_id="maze-workflow-cancel",
            final_output_refs=final_refs,
            inputs={"dag_id": "DAG_ID_SECRET", "question": "QUESTION_SECRET"},
            timeout_seconds=30,
        )
        cancel_response = client.cancel_run(cancel_playground_run_id)
        assert "mazeRunId" not in cancel_response
        assert "maze_run_id" not in json.dumps(cancel_response)
        canceled_run = fetch_run(cancel_playground_run_id)
        assert canceled_run["status"] == "canceled"
        assert "maze_run_id" not in canceled_run
        with core_lock:
            assert any(
                path == f"/runs/{cancel_maze_run_id}/cancel"
                for path, _ in core_requests
            )

        legacy_body = gaia_submit_payload(
            sample_ref="gaia-33333333333333333333333333333333",
            submission_token="3" * 64,
            maze_workflow_id="maze-workflow-legacy-path",
        )
        for field in ("executionWorkspaceDir", "playgroundWorkspaceDir"):
            status_code, _ = request_json(
                "/api/benchmarks/gaia/runs",
                method="POST",
                payload={**legacy_body, field: str(tmp_path)},
            )
            assert status_code == 400

        valid_content = b"FILE_INPUT_ONLY"
        upload_body = gaia_submit_payload(
            sample_ref="gaia-44444444444444444444444444444444",
            submission_token="4" * 64,
            maze_workflow_id="maze-workflow-invalid-upload",
            workflow="file",
        )
        upload_body["inputs"] = {
            "question": "PRIVATE_QUESTION",
            "supplementary_path": "input.txt",
        }
        upload_body["executionFile"] = {
            "name": "input.txt",
            "contentBase64": base64.b64encode(valid_content).decode("ascii"),
            "sha256": hashlib.sha256(valid_content).hexdigest(),
        }
        invalid_uploads = [
            {**upload_body, "executionFile": {**upload_body["executionFile"], "name": "../input.txt"}},
            {**upload_body, "executionFile": {**upload_body["executionFile"], "name": "input.exe"}},
            {**upload_body, "executionFile": {**upload_body["executionFile"], "contentBase64": "not-base64"}},
            {**upload_body, "executionFile": {**upload_body["executionFile"], "contentBase64": "Zh=="}},
            {**upload_body, "executionFile": {**upload_body["executionFile"], "sha256": upload_body["executionFile"]["sha256"].upper()}},
            {**upload_body, "executionFile": {**upload_body["executionFile"], "sha256": "0" * 64}},
            {
                **upload_body,
                "executionFile": {
                    "name": "input.txt",
                    "contentBase64": base64.b64encode(b"x" * 17).decode("ascii"),
                    "sha256": hashlib.sha256(b"x" * 17).hexdigest(),
                },
            },
        ]
        invalid_statuses = []
        for invalid_upload in invalid_uploads:
            status_code, _ = request_json(
                "/api/benchmarks/gaia/runs",
                method="POST",
                payload=invalid_upload,
            )
            invalid_statuses.append(status_code)
        assert invalid_statuses == [400, 400, 400, 400, 400, 400, 413]

        unsafe_staging_body = {
            **upload_body,
            "sampleRef": "gaia-22222222222222222222222222222222",
            "submissionToken": "2" * 64,
            "mazeWorkflowId": "maze-workflow-file-drop-once",
        }
        status_code, unsafe_staging_submit = request_json(
            "/api/benchmarks/gaia/runs",
            method="POST",
            payload=unsafe_staging_body,
        )
        assert status_code == 502
        unsafe_run_dir = (
            workspaces_dir
            / "gaia-validation"
            / "runs"
            / unsafe_staging_submit["playgroundRunId"]
        )
        unsafe_target = tmp_path / "unsafe-staging-target"
        unsafe_target.mkdir()
        unsafe_staging_link = unsafe_run_dir / "gaia_execution"
        os.symlink(unsafe_target, unsafe_staging_link)
        restart_playground_backend()
        status_code, recovered_staging_submit = request_json(
            "/api/benchmarks/gaia/runs",
            method="POST",
            payload=unsafe_staging_body,
        )
        assert status_code == 200
        assert recovered_staging_submit["mazeRunId"]
        assert list(unsafe_target.iterdir()) == []
        unsafe_staging_link.unlink()

        linked_target = tmp_path / "linked-workspace-target"
        linked_target.mkdir()
        os.symlink(linked_target, workspaces_dir / "linked")
        status_code, _ = request_json(
            "/api/benchmarks/gaia/runs",
            method="POST",
            payload=gaia_submit_payload(
                sample_ref="gaia-55555555555555555555555555555555",
                submission_token="5" * 64,
                maze_workflow_id="maze-workflow-symlink",
                workspace_id="linked",
            ),
        )
        assert status_code == 400
        assert list(linked_target.iterdir()) == []

        idempotent_body = gaia_submit_payload(
            sample_ref="gaia-66666666666666666666666666666666",
            submission_token="6" * 64,
            maze_workflow_id="maze-workflow-idempotent",
        )
        status_code, first_submit = request_json(
            "/api/benchmarks/gaia/runs",
            method="POST",
            payload=idempotent_body,
        )
        assert status_code == 201
        assert first_submit["playgroundRunId"]
        assert first_submit["mazeRunId"]
        status_code, duplicate_submit = request_json(
            "/api/benchmarks/gaia/runs",
            method="POST",
            payload=idempotent_body,
        )
        assert status_code == 200
        assert duplicate_submit["idempotent"] is True
        assert duplicate_submit["playgroundRunId"] == first_submit["playgroundRunId"]
        assert duplicate_submit["mazeRunId"] == first_submit["mazeRunId"]
        with core_lock:
            assert sum(
                payload.get("workflow_id") == "maze-workflow-idempotent"
                for path, payload in core_requests
                if path == "/run_workflow"
            ) == 1
        status_code, _ = request_json(
            "/api/benchmarks/gaia/runs",
            method="POST",
            payload={**idempotent_body, "submissionToken": "7" * 64},
        )
        assert status_code == 403
        status_code, _ = request_json(
            "/api/benchmarks/gaia/runs",
            method="POST",
            payload={**idempotent_body, "inputs": {"question": "changed"}},
        )
        assert status_code == 409
        status_code, other_workspace_submit = request_json(
            "/api/benchmarks/gaia/runs",
            method="POST",
            payload={**idempotent_body, "playgroundWorkspaceId": "other"},
        )
        assert status_code == 409
        assert other_workspace_submit["playgroundRunId"] != first_submit["playgroundRunId"]
        with core_lock:
            assert sum(
                run["workflow_id"] == "maze-workflow-idempotent"
                for run in core_runs.values()
            ) == 1
        status_code, replayed_sample = request_json(
            "/api/benchmarks/gaia/runs",
            method="POST",
            payload={
                **idempotent_body,
                "sampleRef": "gaia-67676767676767676767676767676767",
            },
        )
        assert status_code == 409
        assert replayed_sample["playgroundRunId"] != first_submit["playgroundRunId"]

        dropped_body = gaia_submit_payload(
            sample_ref="gaia-70707070707070707070707070707070",
            submission_token="0" * 64,
            maze_workflow_id="maze-workflow-drop-once",
        )
        status_code, dropped_submit = request_json(
            "/api/benchmarks/gaia/runs",
            method="POST",
            payload=dropped_body,
        )
        assert status_code == 502
        assert dropped_submit["playgroundRunId"]
        status_code, recovered_drop = request_json(
            "/api/benchmarks/gaia/runs",
            method="POST",
            payload=dropped_body,
        )
        assert status_code == 200
        assert recovered_drop["idempotent"] is True
        assert recovered_drop["playgroundRunId"] == dropped_submit["playgroundRunId"]
        assert recovered_drop["mazeRunId"]
        with core_lock:
            assert sum(
                payload.get("workflow_id") == "maze-workflow-drop-once"
                for path, payload in core_requests
                if path == "/run_workflow"
            ) == 2
            assert sum(
                run["workflow_id"] == "maze-workflow-drop-once"
                for run in core_runs.values()
            ) == 1

        hung_body = gaia_submit_payload(
            sample_ref="gaia-f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0",
            submission_token="f" * 64,
            maze_workflow_id="maze-workflow-hang",
        )
        hung_started = time.monotonic()
        status_code, hung_submit = request_json(
            "/api/benchmarks/gaia/runs",
            method="POST",
            payload=hung_body,
        )
        assert status_code == 504
        assert time.monotonic() - hung_started < 0.9
        assert hung_submit["playgroundRunId"]
        time.sleep(1.1)
        status_code, recovered_hung = request_json(
            "/api/benchmarks/gaia/runs",
            method="POST",
            payload=hung_body,
        )
        assert status_code == 200
        assert recovered_hung["playgroundRunId"] == hung_submit["playgroundRunId"]
        assert recovered_hung["mazeRunId"]
        with core_lock:
            assert sum(
                payload.get("workflow_id") == "maze-workflow-hang"
                for path, payload in core_requests
                if path == "/run_workflow"
            ) == 1

        timeout_token = "8" * 64
        timeout_body = gaia_submit_payload(
            sample_ref="gaia-88888888888888888888888888888888",
            submission_token=timeout_token,
            maze_workflow_id="maze-workflow-timeout",
        )
        status_code, timeout_submit = request_json(
            "/api/benchmarks/gaia/runs",
            method="POST",
            payload=timeout_body,
        )
        assert status_code == 201
        status_code, forged_finish = request_json(
            f"/api/benchmarks/gaia/runs/{timeout_submit['playgroundRunId']}/finish",
            method="POST",
            payload={
                "status": "succeeded",
                "submissionToken": timeout_token,
                "playgroundWorkspaceId": "gaia-validation",
            },
        )
        assert status_code == 409
        status_code, timeout_response = request_json(
            f"/api/benchmarks/gaia/runs/{timeout_submit['playgroundRunId']}/cancel",
            method="POST",
            payload={
                "outcome": "timed_out",
                "submissionToken": timeout_token,
                "playgroundWorkspaceId": "gaia-validation",
            },
        )
        assert status_code == 200
        assert timeout_response["status"] == "timed_out"
        assert "mazeRunId" not in timeout_response
        status_code, timeout_events = request_json(
            f"/api/workflow-runs/static/{timeout_submit['playgroundRunId']}/events"
            "?workspaceId=gaia-validation"
        )
        assert status_code == 200
        assert [
            event["type"]
            for event in timeout_events["events"]
            if event["type"].startswith("benchmark_run_")
        ] == ["benchmark_run_timed_out"]

        pending_cancel_token = "1" * 64
        pending_cancel_body = gaia_submit_payload(
            sample_ref="gaia-11111111111111111111111111111112",
            submission_token=pending_cancel_token,
            maze_workflow_id="maze-workflow-cancel-pending",
        )
        status_code, pending_cancel_submit = request_json(
            "/api/benchmarks/gaia/runs",
            method="POST",
            payload=pending_cancel_body,
        )
        assert status_code == 201
        pending_cancel_path = (
            "/api/benchmarks/gaia/runs/"
            f"{pending_cancel_submit['playgroundRunId']}/cancel"
        )
        pending_cancel_payload = {
            "outcome": "canceled",
            "submissionToken": pending_cancel_token,
            "playgroundWorkspaceId": "gaia-validation",
        }
        status_code, pending_cancel_response = request_json(
            pending_cancel_path,
            method="POST",
            payload=pending_cancel_payload,
        )
        assert status_code == 202
        assert pending_cancel_response["status"] == "running"
        status_code, pending_cancel_response = request_json(
            pending_cancel_path,
            method="POST",
            payload=pending_cancel_payload,
        )
        assert status_code == 200
        assert pending_cancel_response["status"] == "canceled"

        already_canceled_token = "d" * 64
        already_canceled_body = gaia_submit_payload(
            sample_ref="gaia-ddddddddddddddddddddddddddddddde",
            submission_token=already_canceled_token,
            maze_workflow_id="maze-workflow-already-cancelled",
        )
        _, already_canceled_submit = request_json(
            "/api/benchmarks/gaia/runs",
            method="POST",
            payload=already_canceled_body,
        )
        status_code, already_canceled_response = request_json(
            f"/api/benchmarks/gaia/runs/"
            f"{already_canceled_submit['playgroundRunId']}/cancel",
            method="POST",
            payload={
                "outcome": "timed_out",
                "submissionToken": already_canceled_token,
                "playgroundWorkspaceId": "gaia-validation",
            },
        )
        assert status_code == 200
        assert already_canceled_response["status"] == "canceled"

        cancel_race_token = "9" * 64
        cancel_race_body = gaia_submit_payload(
            sample_ref="gaia-99999999999999999999999999999999",
            submission_token=cancel_race_token,
            maze_workflow_id="maze-workflow-cancel-race-success",
        )
        _, cancel_race_submit = request_json(
            "/api/benchmarks/gaia/runs",
            method="POST",
            payload=cancel_race_body,
        )
        status_code, cancel_race_response = request_json(
            f"/api/benchmarks/gaia/runs/{cancel_race_submit['playgroundRunId']}/cancel",
            method="POST",
            payload={
                "submissionToken": cancel_race_token,
                "playgroundWorkspaceId": "gaia-validation",
            },
        )
        assert status_code == 200
        assert cancel_race_response["status"] == "completed"

        terminal_race_token = "a" * 64
        terminal_race_body = gaia_submit_payload(
            sample_ref="gaia-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaab",
            submission_token=terminal_race_token,
            maze_workflow_id="maze-workflow-terminal-race",
        )
        _, terminal_race_submit = request_json(
            "/api/benchmarks/gaia/runs",
            method="POST",
            payload=terminal_race_body,
        )
        barrier = threading.Barrier(3)
        race_responses = []

        def race_terminal(action, payload):
            barrier.wait()
            race_responses.append(
                request_json(
                    f"/api/benchmarks/gaia/runs/"
                    f"{terminal_race_submit['playgroundRunId']}/{action}",
                    method="POST",
                    payload=payload,
                )
            )

        finish_thread = threading.Thread(
            target=race_terminal,
            args=(
                "finish",
                {
                    "status": "succeeded",
                    "submissionToken": terminal_race_token,
                    "playgroundWorkspaceId": "gaia-validation",
                },
            ),
        )
        cancel_thread = threading.Thread(
            target=race_terminal,
            args=(
                "cancel",
                {
                    "submissionToken": terminal_race_token,
                    "playgroundWorkspaceId": "gaia-validation",
                },
            ),
        )
        finish_thread.start()
        cancel_thread.start()
        barrier.wait()
        finish_thread.join(timeout=5)
        cancel_thread.join(timeout=5)
        assert not finish_thread.is_alive()
        assert not cancel_thread.is_alive()
        assert sorted(status for status, _ in race_responses) == [200, 200]
        _, terminal_race_events = request_json(
            f"/api/workflow-runs/static/{terminal_race_submit['playgroundRunId']}/events"
            "?workspaceId=gaia-validation"
        )
        assert [
            event["type"]
            for event in terminal_race_events["events"]
            if event["type"].startswith("benchmark_run_")
        ] == ["benchmark_run_succeeded"]

        mismatch_token = "e" * 64
        mismatch_body = gaia_submit_payload(
            sample_ref="gaia-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeef",
            submission_token=mismatch_token,
            maze_workflow_id="maze-workflow-running",
        )
        _, mismatch_submit = request_json(
            "/api/benchmarks/gaia/runs",
            method="POST",
            payload=mismatch_body,
        )
        with core_lock:
            core_runs[mismatch_submit["mazeRunId"]]["workflow_id"] = (
                "forged-maze-workflow"
            )
        status_code, mismatch_finish = request_json(
            f"/api/benchmarks/gaia/runs/{mismatch_submit['playgroundRunId']}/finish",
            method="POST",
            payload={
                "status": "succeeded",
                "submissionToken": mismatch_token,
                "playgroundWorkspaceId": "gaia-validation",
            },
        )
        assert status_code == 409
        assert "binding does not match" in mismatch_finish["error"]

        journal_token = "b" * 64
        journal_body = gaia_submit_payload(
            sample_ref="gaia-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbc",
            submission_token=journal_token,
            maze_workflow_id="maze-workflow-running",
        )
        _, journal_submit = request_json(
            "/api/benchmarks/gaia/runs",
            method="POST",
            payload=journal_body,
        )
        journal_dir = (
            workspaces_dir
            / "gaia-validation"
            / "runs"
            / journal_submit["playgroundRunId"]
        )
        stop_playground_backend()
        journal_snapshot = json.loads((journal_dir / "run.json").read_text())
        journal_snapshot["maze_run_id"] = None
        journal_snapshot["metadata"]["maze_run_id"] = None
        journal_snapshot["gaia_private"]["submission_state"] = "submitting"
        journal_snapshot["events"] = {"count": 2, "last_seq": 2}
        (journal_dir / "run.json").write_text(json.dumps(journal_snapshot))
        restart_playground_backend()
        status_code, journal_lookup = request_json(
            "/api/benchmarks/gaia/runs/lookup",
            method="POST",
            payload={
                "sampleRef": journal_body["sampleRef"],
                "submissionToken": journal_token,
                "playgroundWorkspaceId": "gaia-validation",
            },
        )
        assert status_code == 200
        assert journal_lookup["mazeRunId"] == journal_submit["mazeRunId"]
        repaired_snapshot = json.loads((journal_dir / "run.json").read_text())
        assert repaired_snapshot["maze_run_id"] == journal_submit["mazeRunId"]
        journal_events = [
            json.loads(line)
            for line in (journal_dir / "events.jsonl").read_text().splitlines()
        ]
        assert [event["seq"] for event in journal_events] == [1, 2, 3]
        assert sum(event["type"] == "maze_run_created" for event in journal_events) == 1

        crash_token = "c" * 64
        crash_body = gaia_submit_payload(
            sample_ref="gaia-cccccccccccccccccccccccccccccccd",
            submission_token=crash_token,
            maze_workflow_id="maze-workflow-running",
        )
        _, crash_submit = request_json(
            "/api/benchmarks/gaia/runs",
            method="POST",
            payload=crash_body,
        )
        crash_dir = (
            workspaces_dir
            / "gaia-validation"
            / "runs"
            / crash_submit["playgroundRunId"]
        )
        stop_playground_backend()
        crash_snapshot = json.loads((crash_dir / "run.json").read_text())
        crash_snapshot["maze_run_id"] = None
        crash_snapshot["metadata"]["maze_run_id"] = None
        crash_snapshot["gaia_private"]["submission_state"] = "submitting"
        crash_snapshot["events"] = {"count": 2, "last_seq": 2}
        (crash_dir / "run.json").write_text(json.dumps(crash_snapshot))
        crash_events = [
            json.loads(line)
            for line in (crash_dir / "events.jsonl").read_text().splitlines()
            if json.loads(line)["type"] != "maze_run_created"
        ]
        (crash_dir / "events.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in crash_events)
        )
        core_list_failures["remaining"] = 1
        restart_playground_backend()
        first_recovery_view = fetch_run(crash_submit["playgroundRunId"])
        assert first_recovery_view["status"] == "running"
        recovered_snapshot = json.loads((crash_dir / "run.json").read_text())
        assert recovered_snapshot["maze_run_id"] == crash_submit["mazeRunId"]
        recovered_events = [
            json.loads(line)
            for line in (crash_dir / "events.jsonl").read_text().splitlines()
        ]
        assert [event["seq"] for event in recovered_events] == [1, 2, 3]
        assert sum(event["type"] == "maze_run_created" for event in recovered_events) == 1

    finally:
        release_hanging_artifacts.set()
        stop_playground_backend()
        core_server.shutdown()
        core_server.server_close()
        core_thread.join(timeout=5)
