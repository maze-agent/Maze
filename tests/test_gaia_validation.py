from __future__ import annotations

import importlib
import json
from pathlib import Path
import stat
import threading
import time

import pytest

from maze.client.maze.workflow import MaWorkflow
from workflows.gaia.scorer import question_scorer
import workflows.gaia.validation as validation
from workflows.gaia.validation import (
    FINAL_ANSWER_MARKER,
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
    validation_dir = root / "2023" / "validation"
    validation_dir.mkdir(parents=True)
    (validation_dir / "document.txt").write_text(
        "FILE_INPUT_ONLY",
        encoding="utf-8",
    )
    (validation_dir / "unsupported.xlsx").write_bytes(b"not-an-xlsx")
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
                "dag_id": "test-split-record",
                "dag_source": "gaia",
                "dag_type": "reason",
                "dag_supplementary_files": [],
            },
        ],
    )
    _write_jsonl(
        validation_dir / "metadata.jsonl",
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
        ],
    )
    return root


def _reason_dataset(tmp_path: Path, dag_ids: list[str]) -> Path:
    root = tmp_path / "gaia"
    validation_dir = root / "2023" / "validation"
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
        validation_dir / "metadata.jsonl",
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


def _config(data_root: Path, output_dir: Path, **overrides) -> ValidationConfig:
    values = {
        "server_url": "http://maze.test",
        "data_root": data_root,
        "output_dir": output_dir,
        "base_url": "http://model.test/v1",
        "model": "local-model",
    }
    values.update(overrides)
    return ValidationConfig(**values)


class _FakeTemplate:
    def __init__(self, owner: "_FakeClient", workflow_id: str):
        self.owner = owner
        self.workflow_id = workflow_id
        self.final_output_refs = {
            "final_answer": {
                "__maze_output_ref__": True,
                "task_id": f"answer-{workflow_id}",
                "output_key": "final_answer",
            }
        }

    def run(self, **kwargs) -> str:
        return self.owner.submit(self.workflow_id, kwargs)


class _FakeClient:
    accepted_by_key: dict[str, str] = {}
    accepted_lock = threading.Lock()

    def __init__(self, _server_url: str, request_timeout: float | None = None):
        self.request_timeout = request_timeout
        self.templates: dict[str, _FakeTemplate] = {}
        self.submissions: list[dict[str, object]] = []
        self.cancellations: list[tuple[str, str | None]] = []
        self.create_count = 0
        self.fail_after_accept_once = False
        self.wait_timeout = False
        self.wait_status = "succeeded"
        self.wait_delay = 0.0
        self.active_waits = 0
        self.max_active_waits = 0
        self.wait_lock = threading.Lock()

    def create_workflow_from(self, _definition, inputs=None):
        self.create_count += 1
        workflow_id = f"workflow-{self.create_count}"
        template = _FakeTemplate(self, workflow_id)
        self.templates[workflow_id] = template
        return template

    def get_workflow(self, workflow_id: str):
        return self.templates.get(workflow_id) or _FakeTemplate(self, workflow_id)

    def submit(self, workflow_id: str, kwargs: dict[str, object]) -> str:
        record = {"workflow_id": workflow_id, **kwargs}
        self.submissions.append(record)
        key = str(kwargs["idempotency_key"])
        with self.accepted_lock:
            run_id = self.accepted_by_key.setdefault(
                key,
                f"core-{kwargs['inputs']['dag_id']}",
            )
        if self.fail_after_accept_once:
            self.fail_after_accept_once = False
            raise RuntimeError("lost submit response")
        return run_id

    def wait_run(self, run_id: str, timeout: float):
        if self.wait_timeout:
            raise TimeoutError(run_id)
        with self.wait_lock:
            self.active_waits += 1
            self.max_active_waits = max(self.max_active_waits, self.active_waits)
        try:
            if self.wait_delay:
                time.sleep(self.wait_delay)
        finally:
            with self.wait_lock:
                self.active_waits -= 1
        dag_id = run_id.removeprefix("core-")
        answers = {
            "reason-1": "GOLD_REASON_SECRET",
            "file-1": "GOLD_FILE_SECRET",
        }
        answer = answers.get(dag_id, f"ANSWER_{dag_id}")
        if self.wait_status != "succeeded":
            return {"status": self.wait_status, "error": "CORE_FAILURE"}
        return {
            "status": "succeeded",
            "result_summary": {
                "final_answer": f"work\n{FINAL_ANSWER_MARKER} {answer}",
            },
        }

    def cancel_run(self, run_id: str, reason: str | None = None):
        self.cancellations.append((run_id, reason))
        return {"run_id": run_id, "run_status": "cancelled"}


@pytest.fixture(autouse=True)
def _clear_fake_core():
    _FakeClient.accepted_by_key.clear()


def _install_client(monkeypatch, client: _FakeClient) -> None:
    monkeypatch.setattr(validation, "MaClient", lambda *_args, **_kwargs: client)


def test_cli_has_no_playground_control_plane(tmp_path):
    args = _parse_args(
        [
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
    )
    assert args.max_in_flight_runs is None
    assert not hasattr(args, "playground_url")
    with pytest.raises(SystemExit):
        _parse_args([
            "--playground-url",
            "http://obsolete.test",
            "--server-url",
            "x",
            "--data-root",
            "x",
            "--output-dir",
            "x",
            "--base-url",
            "x",
            "--model",
            "x",
        ])


@pytest.mark.parametrize("value", [0, -1])
def test_config_rejects_non_positive_in_flight_limit(tmp_path, value):
    with pytest.raises(ValueError, match="max_in_flight_runs"):
        _config(tmp_path, tmp_path / "out", max_in_flight_runs=value).validate()


@pytest.mark.parametrize(
    ("prediction", "expected", "correct"),
    [
        ("42", "42", True),
        ("$1,000", "1000", True),
        ("Paris", "paris", True),
        ("Paris", "London", False),
    ],
)
def test_official_gaia_scorer_semantics(prediction, expected, correct):
    assert question_scorer(prediction, expected) is correct


def test_extract_final_answer_uses_last_exact_marker():
    raw = "FINAL ANSWER: first\nanalysis\nFINAL ANSWER: second"
    assert extract_final_answer(raw) == "second"
    assert extract_final_answer("Final answer: wrong case") is None
    assert extract_final_answer("FINAL ANSWER:   ") is None


def test_loader_selects_validation_and_classifies_unsupported_rows(tmp_path):
    samples = load_validation_samples(_dataset(tmp_path))
    assert [sample.dag_id for sample in samples] == [
        "reason-1",
        "file-1",
        "file-unsupported",
        "speech-1",
    ]
    assert samples[0].workflow == "reason"
    assert samples[1].workflow == "file"
    assert samples[2].skip_reason == "unsupported_file_type:.xlsx"
    assert samples[3].skip_reason == "unsupported_workflow:speech"


def test_loader_rejects_supplementary_path_escape(tmp_path):
    root = _reason_dataset(tmp_path, ["escape"])
    query = root / "gaia_query.jsonl"
    _write_jsonl(query, [{
        "dag_id": "escape",
        "dag_source": "gaia",
        "dag_type": "file",
        "dag_supplementary_files": ["../outside.txt"],
    }])
    samples = load_validation_samples(root)
    assert "stay within validation data" in samples[0].load_error


def test_runner_submits_directly_to_core_and_keeps_gold_private(tmp_path, monkeypatch):
    client = _FakeClient("unused")
    _install_client(monkeypatch, client)
    output_dir = tmp_path / "report"

    summary = run_validation(_config(_dataset(tmp_path), output_dir))

    assert summary["submitted"] == 2
    assert summary["succeeded"] == 2
    assert summary["correct"] == 2
    assert "playground_url" not in summary["config"]
    assert len(client.submissions) == 2
    serialized = json.dumps(client.submissions)
    assert "GOLD_REASON_SECRET" not in serialized
    assert "GOLD_FILE_SECRET" not in serialized
    assert all(item["metadata"] == {
        "benchmark": "gaia",
        "workflow": item["inputs"]["dag_id"].split("-")[0],
    } for item in client.submissions)
    assert all(str(item["idempotency_key"]).startswith("gaia-") for item in client.submissions)
    assert all(len(str(item["idempotency_fingerprint"])) == 64 for item in client.submissions)

    file_submit = next(item for item in client.submissions if item["inputs"]["dag_id"] == "file-1")
    file_context = file_submit["file_context"]
    assert file_context["private"] is True
    assert file_context["artifact_store"]["private"] is True
    staged = Path(file_context["workspace_dir"])
    assert (staged / "files" / "document.txt").read_text() == "FILE_INPUT_ONLY"
    assert stat.S_IMODE(staged.stat().st_mode) == 0o700
    assert stat.S_IMODE((staged / "files" / "document.txt").stat().st_mode) == 0o600

    results = [json.loads(line) for line in (output_dir / "results.jsonl").read_text().splitlines()]
    assert {item["run_id"] for item in results if item["run_id"]} == {
        "core-reason-1",
        "core-file-1",
    }
    assert all("playground_run_id" not in item for item in results)
    assert stat.S_IMODE((output_dir / "results.jsonl").stat().st_mode) == 0o600


@pytest.mark.parametrize(("limit", "expected"), [(1, 1), (2, 2)])
def test_runner_bounds_core_wait_concurrency(tmp_path, monkeypatch, limit, expected):
    client = _FakeClient("unused")
    client.wait_delay = 0.05
    _install_client(monkeypatch, client)
    data_root = _reason_dataset(tmp_path, ["a", "b", "c"])

    run_validation(_config(
        data_root,
        tmp_path / "report",
        max_in_flight_runs=limit,
    ))

    assert client.max_active_waits == expected


def test_runner_cancels_core_run_after_timeout(tmp_path, monkeypatch):
    client = _FakeClient("unused")
    client.wait_timeout = True
    _install_client(monkeypatch, client)
    output_dir = tmp_path / "report"

    summary = run_validation(_config(
        _reason_dataset(tmp_path, ["slow"]),
        output_dir,
        timeout=0.01,
    ))

    assert summary["succeeded"] == 0
    assert client.cancellations == [
        ("core-slow", "GAIA validation exceeded 0.01 seconds"),
    ]
    result = json.loads((output_dir / "results.jsonl").read_text())
    assert result["status"] == "timed_out"


def test_runner_recovers_lost_submit_response_with_core_idempotency(
    tmp_path,
    monkeypatch,
):
    data_root = _reason_dataset(tmp_path, ["resume"])
    output_dir = tmp_path / "report"
    first = _FakeClient("unused")
    first.fail_after_accept_once = True
    _install_client(monkeypatch, first)

    first_summary = run_validation(_config(data_root, output_dir))
    assert first_summary["submitted"] == 0
    assert len(_FakeClient.accepted_by_key) == 1

    second = _FakeClient("unused")
    _install_client(monkeypatch, second)
    second_summary = run_validation(_config(data_root, output_dir))

    assert second_summary["submitted"] == 1
    assert second_summary["correct"] == 1
    assert len(_FakeClient.accepted_by_key) == 1
    assert second.submissions[0]["workflow_id"] == "workflow-1"
    journal = json.loads(next((output_dir / ".gaia-validation-state" / "submissions").iterdir()).read_text())
    assert journal["run_id"] == "core-resume"
    assert "submission_token" not in journal
    assert "playground_run_id" not in journal


def test_runner_reuses_completed_private_result(tmp_path, monkeypatch):
    data_root = _reason_dataset(tmp_path, ["done"])
    output_dir = tmp_path / "report"
    first = _FakeClient("unused")
    _install_client(monkeypatch, first)
    assert run_validation(_config(data_root, output_dir))["correct"] == 1

    second = _FakeClient("unused")
    _install_client(monkeypatch, second)
    assert run_validation(_config(data_root, output_dir))["correct"] == 1
    assert second.create_count == 0
    assert second.submissions == []


def test_runner_rejects_output_directory_symlink(tmp_path, monkeypatch):
    client = _FakeClient("unused")
    _install_client(monkeypatch, client)
    target = tmp_path / "real-report"
    target.mkdir()
    link = tmp_path / "report-link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="real directory"):
        run_validation(_config(_reason_dataset(tmp_path, ["x"]), link))


@pytest.mark.parametrize("api_key", ["plaintext-secret", "env:BAD-NAME"])
def test_runner_rejects_plaintext_or_malformed_api_key(tmp_path, api_key):
    with pytest.raises(ValueError, match="api_key"):
        _config(tmp_path, tmp_path / "report", api_key=api_key).validate()


def test_cli_env_key_is_not_printed_or_persisted(tmp_path, monkeypatch, capsys):
    client = _FakeClient("unused")
    _install_client(monkeypatch, client)
    monkeypatch.setenv("GAIA_TEST_KEY", "TOP_SECRET_VALUE")
    data_root = _reason_dataset(tmp_path, ["cli"])
    output_dir = tmp_path / "report"
    exit_code = main([
        "--server-url",
        "http://maze.test",
        "--data-root",
        str(data_root),
        "--output-dir",
        str(output_dir),
        "--base-url",
        "http://model.test/v1",
        "--model",
        "local-model",
        "--api-key",
        "env:GAIA_TEST_KEY",
    ])
    assert exit_code == 0
    assert "TOP_SECRET_VALUE" not in capsys.readouterr().out
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in output_dir.rglob("*")
        if path.is_file()
    )
    assert "TOP_SECRET_VALUE" not in persisted


def test_maworkflow_forwards_core_idempotency():
    class Client:
        server_url = "http://maze.test"
        request_timeout = None

        def __init__(self):
            self.spec = None

        def _build_file_context(self, **_kwargs):
            return None

        def submit_workflow(self, spec, **_kwargs):
            self.spec = spec
            return {"workflow_id": spec["workflow_id"], "run_id": "core-run"}

    client = Client()
    workflow = MaWorkflow("workflow", client)
    workflow._nodes["task"] = {"id": "task"}
    assert workflow.run(
        idempotency_key="gaia-key",
        idempotency_fingerprint="a" * 64,
    ) == "core-run"
    assert client.spec["run"]["idempotency_key"] == "gaia-key"
    assert client.spec["run"]["idempotency_fingerprint"] == "a" * 64
