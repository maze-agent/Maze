from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List

import yaml


SKILL_MAIN_FILE = "SKILL.md"


@dataclass
class SkillSpec:
    """Claude/Cursor-style skill instructions for Maze ReAct workflows.

    Skills are prompt/instruction packages. They do not register executable
    tools by themselves; ReAct tools must still be passed separately.
    """

    name: str
    description: str
    root: Path
    body: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.root = Path(self.root).expanduser().resolve()
        if not self.name:
            raise ValueError("Skill name is required")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", self.name):
            raise ValueError(
                "Skill name must be lowercase letters/numbers/hyphens, "
                "start with a letter or number, and be at most 64 chars"
            )
        if not self.description:
            raise ValueError(f"Skill {self.name!r} requires a non-empty description")

    def catalog_entry(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "files": self.list_files(),
        }

    def list_files(self) -> List[str]:
        files = []
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and not _is_hidden_or_parent_hidden(path, self.root):
                files.append(path.relative_to(self.root).as_posix())
        return files

    def read_file(self, file_name: str = SKILL_MAIN_FILE, max_chars: int | None = None) -> Dict[str, Any]:
        relative = _safe_relative_path(file_name)
        target = (self.root / relative).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"Skill file escapes skill root: {file_name}") from exc

        if not target.exists() or not target.is_file():
            raise ValueError(f"Skill file not found: {self.name}/{relative.as_posix()}")

        text = target.read_text(encoding="utf-8")
        truncated = False
        if max_chars is not None and max_chars >= 0 and len(text) > max_chars:
            text = text[:max_chars]
            truncated = True
        return {
            "skill": self.name,
            "file": relative.as_posix(),
            "content": text,
            "truncated": truncated,
            "available_files": self.list_files(),
        }


def load_skill(path: str | Path) -> SkillSpec:
    """Load a standard SKILL.md directory without Maze-specific metadata."""
    root = Path(path).expanduser().resolve()
    skill_file = root / SKILL_MAIN_FILE
    if root.is_file() and root.name == SKILL_MAIN_FILE:
        skill_file = root
        root = root.parent
    if not skill_file.exists():
        raise ValueError(f"Skill directory must contain {SKILL_MAIN_FILE}: {root}")

    raw = skill_file.read_text(encoding="utf-8")
    metadata, body = _split_frontmatter(raw)
    name = str(metadata.get("name") or "").strip()
    description = str(metadata.get("description") or "").strip()
    return SkillSpec(
        name=name,
        description=description,
        root=root,
        body=body.strip(),
        metadata=metadata,
    )


def load_skills_from_dir(path: str | Path) -> List[SkillSpec]:
    """Load every direct child directory that contains SKILL.md."""
    root = Path(path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Skills directory not found: {root}")

    skills = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / SKILL_MAIN_FILE).exists():
            skills.append(load_skill(child))
    return skills


def normalize_skills(skills: Iterable[SkillSpec | str | Path] | None) -> List[SkillSpec]:
    if not skills:
        return []

    normalized = []
    seen = set()
    for item in skills:
        skill = item if isinstance(item, SkillSpec) else load_skill(item)
        if skill.name in seen:
            raise ValueError(f"Duplicate skill name: {skill.name}")
        seen.add(skill.name)
        normalized.append(skill)
    return normalized


def build_skill_catalog(skills: Iterable[SkillSpec]) -> Dict[str, Dict[str, Any]]:
    return {skill.name: skill.catalog_entry() for skill in skills}


def build_skill_reader_tool(skills: Iterable[SkillSpec], max_chars: int = 12000):
    from maze.client.maze.decorator import task

    skill_map = {skill.name: skill for skill in skills}

    @task(resources={"cpu": 1, "cpu_mem": 64, "gpu": 0, "gpu_mem": 0})
    def read_skill_file(skill_name: str, file_name: str = SKILL_MAIN_FILE):
        """Read details from a registered skill file when the catalog is insufficient."""
        if skill_name not in skill_map:
            return {
                "error": f"Unknown skill: {skill_name}",
                "skill": skill_name,
                "file": file_name,
                "content": "",
                "truncated": False,
                "available_skills": sorted(skill_map),
                "available_files": [],
            }
        try:
            payload = skill_map[skill_name].read_file(file_name, max_chars=max_chars)
            return {
                "error": "",
                "skill": payload["skill"],
                "file": payload["file"],
                "content": payload["content"],
                "truncated": payload["truncated"],
                "available_skills": sorted(skill_map),
                "available_files": payload["available_files"],
            }
        except Exception as exc:
            return {
                "error": str(exc),
                "skill": skill_name,
                "file": file_name,
                "content": "",
                "truncated": False,
                "available_skills": sorted(skill_map),
                "available_files": skill_map[skill_name].list_files(),
            }

    return read_skill_file


def _split_frontmatter(raw: str) -> tuple[Dict[str, Any], str]:
    if not raw.startswith("---"):
        raise ValueError(f"{SKILL_MAIN_FILE} must start with YAML frontmatter")

    match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)(.*)$", raw, re.S)
    if not match:
        raise ValueError(f"{SKILL_MAIN_FILE} must contain closing YAML frontmatter marker")

    metadata = yaml.safe_load(match.group(1)) or {}
    if not isinstance(metadata, dict):
        raise ValueError(f"{SKILL_MAIN_FILE} frontmatter must be a mapping")
    return metadata, match.group(2)


def _safe_relative_path(file_name: str) -> Path:
    if not file_name or not str(file_name).strip():
        return Path(SKILL_MAIN_FILE)
    relative = Path(str(file_name).replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Invalid skill file path: {file_name}")
    return relative


def _is_hidden_or_parent_hidden(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return any(part.startswith(".") for part in relative.parts)
