import pytest

from maze.client.maze.decorator import task
from maze.client.maze.react import ReActWorkflow
from maze.client.maze.skills import (
    build_skill_catalog,
    build_skill_reader_tool,
    load_skill,
    load_skills_from_dir,
    normalize_skills,
)


class FakeDynamicRun:
    def __init__(self):
        self.run_id = "fake-run"
        self.registered = {}

    def register_task_spec(self, task_func, task_spec_id=None, task_name=None):
        spec = type(
            "FakeSpec",
            (),
            {
                "task_spec_id": task_spec_id,
                "task_name": task_name or task_spec_id,
            },
        )()
        self.registered[task_spec_id] = task_func
        return spec


def write_skill(root, name="data-analysis", description="Analyze tabular data."):
    skill_dir = root / name
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {name}
description: {description}
---
# {name}

Use registered tools in this order.
""",
        encoding="utf-8",
    )
    (skill_dir / "reference.md").write_text("Use pandas through exec_code.\n", encoding="utf-8")
    (skill_dir / ".hidden.md").write_text("hidden\n", encoding="utf-8")
    return skill_dir


def test_load_skill_preserves_standard_skill_markdown(tmp_path):
    skill_dir = write_skill(tmp_path)

    skill = load_skill(skill_dir)

    assert skill.name == "data-analysis"
    assert skill.description == "Analyze tabular data."
    assert "Use registered tools" in skill.body
    assert skill.catalog_entry() == {
        "name": "data-analysis",
        "description": "Analyze tabular data.",
        "files": ["SKILL.md", "reference.md"],
    }


def test_load_skills_from_dir_and_duplicate_detection(tmp_path):
    write_skill(tmp_path, "data-analysis")
    write_skill(tmp_path, "code-review", "Review code changes.")

    skills = load_skills_from_dir(tmp_path)
    assert [skill.name for skill in skills] == ["code-review", "data-analysis"]

    with pytest.raises(ValueError, match="Duplicate skill name"):
        normalize_skills([skills[0], skills[0]])


def test_skill_file_reader_supports_progressive_disclosure(tmp_path):
    skill = load_skill(write_skill(tmp_path))
    reader = build_skill_reader_tool([skill], max_chars=10)

    result = reader("data-analysis", "reference.md")

    assert result["error"] == ""
    assert result["skill"] == "data-analysis"
    assert result["file"] == "reference.md"
    assert result["content"] == "Use pandas"
    assert result["truncated"] is True
    assert result["available_skills"] == ["data-analysis"]
    assert result["available_files"] == ["SKILL.md", "reference.md"]


def test_skill_file_reader_rejects_path_escape(tmp_path):
    skill = load_skill(write_skill(tmp_path))
    reader = build_skill_reader_tool([skill])

    result = reader("data-analysis", "../secret.md")

    assert "Invalid skill file path" in result["error"]
    assert result["content"] == ""


def test_skill_catalog_is_passed_to_react_llm_inputs(tmp_path):
    skill = load_skill(write_skill(tmp_path))

    @task
    def decide(prompt: str, history: list, tools: dict, skills: dict, step: int):
        return {"action": {"final": "done"}}

    @task
    def echo(text: str):
        return {"result": text}

    workflow = ReActWorkflow(
        dynamic_run=FakeDynamicRun(),
        llm_task=decide,
        tools=[echo],
        skills=[skill],
        progressive_skills=True,
    )

    inputs = workflow._build_llm_inputs(1, "analyze data")

    assert "data-analysis" in inputs["skills"]["catalog"]
    assert inputs["skills"]["progressive_disclosure"] is True
    assert "read_skill_file" in workflow.tool_specs
    assert workflow.tool_specs["read_skill_file"]["required_inputs"] == ["skill_name"]


def test_non_progressive_skills_embed_details_without_reader_tool(tmp_path):
    skill = load_skill(write_skill(tmp_path))

    @task
    def decide(prompt: str, history: list, tools: dict, skills: dict, step: int):
        return {"action": {"final": "done"}}

    @task
    def echo(text: str):
        return {"result": text}

    workflow = ReActWorkflow(
        dynamic_run=FakeDynamicRun(),
        llm_task=decide,
        tools=[echo],
        skills=[skill],
        progressive_skills=False,
    )

    inputs = workflow._build_llm_inputs(1, "analyze data")

    assert "details" in inputs["skills"]
    assert "Use registered tools" in inputs["skills"]["details"]["data-analysis"]["body"]
    assert "read_skill_file" not in workflow.tool_specs


def test_build_skill_catalog(tmp_path):
    skill = load_skill(write_skill(tmp_path))

    assert build_skill_catalog([skill]) == {
        "data-analysis": {
            "name": "data-analysis",
            "description": "Analyze tabular data.",
            "files": ["SKILL.md", "reference.md"],
        }
    }
