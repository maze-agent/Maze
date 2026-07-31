from pathlib import Path

import pytest

from maze.client.maze.decorator import get_task_metadata
from maze.core.files.lineage import TASK_RESULT_ENVELOPE, run_task_with_file_context
from maze.core.path.path import MaPath
from workflows.gaia.file import _process_document_file, task1_file_process


def test_file_task_reads_text_from_maze_input_dir(monkeypatch, tmp_path):
    input_dir = tmp_path / "input"
    document = input_dir / "nested" / "facts.txt"
    document.parent.mkdir(parents=True)
    document.write_text("FILE_MARKER_ALPHA", encoding="utf-8")
    monkeypatch.setenv("MAZE_INPUT_DIR", str(input_dir))

    result = task1_file_process(
        dag_id="gaia-file-1",
        question="What marker is in the document?",
        supplementary_path="nested/facts.txt",
    )

    assert result["processed_content"] == "FILE_MARKER_ALPHA"
    assert result["file_name"] == "facts.txt"
    assert len(result["content_sha256"]) == 64
    metadata = get_task_metadata(task1_file_process)
    assert metadata.max_retries == 1
    assert metadata.retry_on == [
        "node_lost",
        "resource_unavailable",
        "artifact_error",
    ]


@pytest.mark.parametrize(
    "supplementary_path",
    [
        "../outside.txt",
        "/tmp/outside.txt",
        r"..\outside.txt",
        r"C:\outside.txt",
        "",
    ],
)
def test_file_task_rejects_paths_outside_maze_input_dir(
    monkeypatch,
    tmp_path,
    supplementary_path,
):
    monkeypatch.setenv("MAZE_INPUT_DIR", str(tmp_path))

    with pytest.raises(ValueError):
        _process_document_file(supplementary_path)


def test_file_task_rejects_symlink_escape(monkeypatch, tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (input_dir / "link.txt").symlink_to(outside)
    monkeypatch.setenv("MAZE_INPUT_DIR", str(input_dir))

    with pytest.raises(ValueError, match="MAZE_INPUT_DIR"):
        _process_document_file("link.txt")


def test_artifact_submit_rejects_workspace_file_symlinks(tmp_path):
    workspace = tmp_path / "workspace"
    files_dir = workspace / "files"
    files_dir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (files_dir / "link.txt").symlink_to(outside)

    with pytest.raises(ValueError, match="symbolic links: link.txt"):
        object.__new__(MaPath)._prepare_initial_artifacts(
            {
                "enabled": True,
                "workspace_dir": str(workspace),
                "artifact_store": {
                    "type": "head_http",
                    "base_url": "http://unused.invalid",
                    "root": str(tmp_path / "artifact-cache"),
                },
            },
            "run-1",
        )


def test_artifact_submit_rejects_workspace_files_directory_symlink(tmp_path):
    external_files = tmp_path / "external-files"
    external_files.mkdir()
    (external_files / "secret.txt").write_text("secret", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "files").symlink_to(external_files, target_is_directory=True)

    with pytest.raises(ValueError, match="files directory.*symbolic link"):
        object.__new__(MaPath)._prepare_initial_artifacts(
            {
                "enabled": True,
                "workspace_dir": str(workspace),
                "artifact_store": {
                    "type": "head_http",
                    "base_url": "http://unused.invalid",
                    "root": str(tmp_path / "artifact-cache"),
                },
            },
            "run-1",
        )


def test_file_task_rejects_unsupported_document_type(monkeypatch, tmp_path):
    document = tmp_path / "facts.bin"
    document.write_bytes(b"FILE_MARKER_BINARY")
    monkeypatch.setenv("MAZE_INPUT_DIR", str(tmp_path))

    with pytest.raises(ValueError, match="Unsupported supplementary file type"):
        _process_document_file(document.name)


def test_file_task_extracts_pdf_text(monkeypatch, tmp_path):
    from PyPDF2 import PdfWriter
    from PyPDF2.generic import DecodedStreamObject, DictionaryObject, NameObject

    document = tmp_path / "facts.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    page = writer.pages[0]
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): writer._add_object(font),
        }),
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (PDF_MARKER_2026) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    with document.open("wb") as handle:
        writer.write(handle)
    monkeypatch.setenv("MAZE_INPUT_DIR", str(tmp_path))

    result = _process_document_file(document.name)

    assert result["processed_content"] == "--- Page 1 ---\nPDF_MARKER_2026"


def test_artifact_snapshot_keeps_file_stable_across_attempts(tmp_path):
    workspace = tmp_path / "workspace"
    document = workspace / "files" / "facts.txt"
    document.parent.mkdir(parents=True)
    document.write_text("ORIGINAL_FILE_CONTENT", encoding="utf-8")
    artifact_cache = tmp_path / "artifact-cache"
    context = object.__new__(MaPath)._prepare_initial_artifacts(
        {
            "enabled": True,
            "workspace_dir": str(workspace),
            "artifact_store": {
                "type": "head_http",
                "base_url": "http://unused.invalid",
                "root": str(artifact_cache),
                "cache_dir": str(artifact_cache),
            },
        },
        "run-1",
    )
    expected_sha256 = context["initial_files"][0]["sha256"]
    document.write_text("MUTATED_AFTER_SUBMIT", encoding="utf-8")

    def read_staged_file(_):
        return {"content": Path("facts.txt").read_text(encoding="utf-8")}

    results = []
    for attempt in (1, 2):
        results.append(
            run_task_with_file_context(
                read_staged_file,
                {},
                {
                    **context,
                    "task_id": "task1_file_process",
                    "attempt": attempt,
                    "dispatch_id": f"dispatch-{attempt}",
                },
            )
        )

    assert all(result[TASK_RESULT_ENVELOPE] is True for result in results)
    assert [result["result"]["content"] for result in results] == [
        "ORIGINAL_FILE_CONTENT",
        "ORIGINAL_FILE_CONTENT",
    ]
    for attempt in (1, 2):
        staged = (
            workspace
            / "runs"
            / "run-1"
            / "work"
            / "tasks"
            / "task1_file_process"
            / f"attempt-{attempt}"
            / f"dispatch-{attempt}"
            / "facts.txt"
        )
        assert staged.read_text(encoding="utf-8") == "ORIGINAL_FILE_CONTENT"
        assert context["initial_files"][0]["sha256"] == expected_sha256
