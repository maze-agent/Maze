from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading

import pytest

from workflows.gaia.scorer import question_scorer
from workflows.gaia.validation import (
    FINAL_ANSWER_MARKER,
    ValidationConfig,
    _parse_args,
    extract_final_answer,
    load_validation_samples,
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

    def run(self, **kwargs):
        with self.client.lock:
            run_id = f"run-{len(self.client.submissions) + 1}"
            self.client.submissions.append((self.workflow, run_id, kwargs))
        return run_id


class _FakeClient:
    instances: list["_FakeClient"] = []

    def __init__(self, server_url: str):
        self.server_url = server_url
        self.templates = []
        self.submissions = []
        self.lock = threading.Lock()
        type(self).instances.append(self)

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
    _FakeClient.instances = []
    monkeypatch.setattr("workflows.gaia.validation.MaClient", _FakeClient)
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
    _FakeClient.instances = []
    monkeypatch.setattr("workflows.gaia.validation.MaClient", _FakeClient)

    summary = run_validation(
        ValidationConfig(
            server_url="http://maze.test",
            data_root=root,
            output_dir=output,
            base_url="http://model.test/v1",
            model="local-model",
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
    assert [item[0] for item in client.templates] == ["file", "reason"]
    assert len(client.submissions) == 2
    serialized_submissions = json.dumps(client.submissions)
    assert "GOLD_REASON_SECRET" not in serialized_submissions
    assert "GOLD_FILE_SECRET" not in serialized_submissions
    assert all("metadata" not in kwargs for _, _, kwargs in client.submissions)

    file_submission = next(item for item in client.submissions if item[0] == "file")
    file_kwargs = file_submission[2]
    assert file_kwargs["artifact_mode"] is True
    workspace = Path(file_kwargs["workspace_dir"])
    assert (workspace / "files" / "document.txt").read_text() == "FILE_INPUT_ONLY"
    assert file_kwargs["inputs"]["supplementary_path"] == "document.txt"

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
    persisted_summary = json.loads((output / "summary.json").read_text())
    assert persisted_summary == summary
    assert "MODEL_KEY" not in (output / "summary.json").read_text()


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
    _MixedResultClient.instances = []
    monkeypatch.setattr("workflows.gaia.validation.MaClient", _MixedResultClient)

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
    def __init__(self, server_url: str):
        super().__init__(server_url)
        self.cancelled = []

    def wait_run(self, run_id: str, timeout: float):
        raise TimeoutError(run_id)

    def cancel_run(self, run_id: str, reason: str):
        self.cancelled.append((run_id, reason))


def test_runner_cancels_run_after_wait_timeout(tmp_path, monkeypatch):
    root = _reason_dataset(tmp_path, ["slow"])
    _TimeoutClient.instances = []
    monkeypatch.setattr("workflows.gaia.validation.MaClient", _TimeoutClient)

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
    assert client.cancelled == [("run-1", "GAIA validation timeout")]
    assert summary["submitted"] == 1
    assert summary["succeeded"] == 0
    record = json.loads((tmp_path / "report" / "results.jsonl").read_text())
    assert record["status"] == "timed_out"
    assert record["correct"] is False
