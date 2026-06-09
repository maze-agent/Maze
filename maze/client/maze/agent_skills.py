from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List


DEFAULT_MAX_SKILL_INSTRUCTIONS_CHARS = 12000
SKILL_RESOURCE_DIRS = ("scripts", "templates", "references")


class AgentSkillError(Exception):
    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        repairable: bool = True,
        details: Dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.repairable = repairable
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error_type": self.error_type,
            "message": self.message,
            "repairable": self.repairable,
            "details": self.details,
        }


@dataclass
class AgentSkillSpec:
    name: str
    path: str
    description: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    resources: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "description": self.description,
            "metadata": dict(self.metadata),
            "resources": list(self.resources),
        }


@dataclass
class AgentLoadedSkill:
    name: str
    path: str
    instructions: str
    description: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    resources: List[Dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    original_chars: int = 0
    returned_chars: int = 0

    def to_llm_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "instructions": self.instructions,
            "resources": list(self.resources),
            "metadata": dict(self.metadata),
            "truncated": self.truncated,
            "original_chars": self.original_chars,
            "returned_chars": self.returned_chars,
        }

    def to_event_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "description": self.description,
            "resources": list(self.resources),
            "metadata": dict(self.metadata),
            "truncated": self.truncated,
            "original_chars": self.original_chars,
            "returned_chars": self.returned_chars,
        }


class AgentSkillRegistry:
    def __init__(
        self,
        skill_dirs: Iterable[str | os.PathLike[str]] | None = None,
        *,
        max_instruction_chars: int = DEFAULT_MAX_SKILL_INSTRUCTIONS_CHARS,
    ):
        self.skill_dirs = _normalize_skill_dirs(skill_dirs)
        self.max_instruction_chars = max(1, int(max_instruction_chars))
        self._specs = self._discover_specs()
        self._loaded: Dict[str, AgentLoadedSkill] = {}

    @property
    def loaded(self) -> Dict[str, AgentLoadedSkill]:
        return self._loaded

    def list_skills(self) -> List[Dict[str, Any]]:
        return [
            self._specs[name].to_dict()
            for name in sorted(self._specs)
        ]

    def get_skill(self, name: str) -> AgentSkillSpec:
        if name not in self._specs:
            raise AgentSkillError(
                "skill_not_found",
                f"Skill not found: {name}",
                details={
                    "skill": name,
                    "available_skills": sorted(self._specs),
                    "skill_dirs": [str(path) for path in self.skill_dirs],
                },
            )
        return self._specs[name]

    def load_skill(self, name: str) -> AgentLoadedSkill:
        if name in self._loaded:
            return self._loaded[name]

        spec = self.get_skill(name)
        skill_path = Path(spec.path)
        skill_md = skill_path / "SKILL.md"
        try:
            raw_text = skill_md.read_text(encoding="utf-8")
        except OSError as exc:
            raise AgentSkillError(
                "skill_read_failed",
                f"Failed to read skill {name}: {exc}",
                repairable=False,
                details={"skill": name, "path": str(skill_md)},
            ) from exc

        front_matter, instructions = _split_front_matter(raw_text)
        metadata = {**spec.metadata, **front_matter}
        description = str(metadata.get("description") or spec.description or _description_from_markdown(instructions))
        compacted, truncated = _compact_instructions(instructions, self.max_instruction_chars)
        loaded = AgentLoadedSkill(
            name=name,
            path=spec.path,
            instructions=compacted,
            description=description,
            metadata=metadata,
            resources=spec.resources,
            truncated=truncated,
            original_chars=len(instructions),
            returned_chars=len(compacted),
        )
        self._loaded[name] = loaded
        return loaded

    def loaded_for_llm(self) -> List[Dict[str, Any]]:
        return [
            skill.to_llm_dict()
            for skill in self._loaded.values()
        ]

    def loaded_events(self) -> List[Dict[str, Any]]:
        return [
            skill.to_event_dict()
            for skill in self._loaded.values()
        ]

    def load_tool(self, name: str) -> Dict[str, Any]:
        loaded = self.load_skill(name)
        return {
            "skill": loaded.to_llm_dict(),
            "loaded_skills": self.loaded_for_llm(),
        }

    def _discover_specs(self) -> Dict[str, AgentSkillSpec]:
        specs: Dict[str, AgentSkillSpec] = {}
        for skill_dir in self.skill_dirs:
            if not skill_dir.exists() or not skill_dir.is_dir():
                continue
            for candidate in sorted(skill_dir.iterdir(), key=lambda path: path.name):
                if not candidate.is_dir():
                    continue
                skill_md = candidate / "SKILL.md"
                if not skill_md.is_file():
                    continue
                metadata = _read_skill_metadata(candidate)
                name = str(metadata.get("name") or candidate.name)
                if name in specs:
                    raise AgentSkillError(
                        "duplicate_skill",
                        f"Duplicate skill name: {name}",
                        repairable=False,
                        details={
                            "skill": name,
                            "paths": [specs[name].path, str(candidate.resolve())],
                        },
                    )
                description = str(metadata.get("description") or _description_from_file(skill_md))
                specs[name] = AgentSkillSpec(
                    name=name,
                    path=str(candidate.resolve()),
                    description=description,
                    metadata=metadata,
                    resources=_resource_manifest(candidate),
                )
        return specs


def create_skill_registry(
    *,
    skills: Iterable[str] | None = None,
    skill_dirs: Iterable[str | os.PathLike[str]] | None = None,
    max_instruction_chars: int = DEFAULT_MAX_SKILL_INSTRUCTIONS_CHARS,
) -> AgentSkillRegistry | None:
    if not skills and not skill_dirs:
        return None

    registry = AgentSkillRegistry(
        skill_dirs=skill_dirs,
        max_instruction_chars=max_instruction_chars,
    )
    for skill_name in skills or []:
        registry.load_skill(str(skill_name))
    return registry


def _normalize_skill_dirs(skill_dirs: Iterable[str | os.PathLike[str]] | None) -> List[Path]:
    values = list(skill_dirs or [])
    env_dirs = os.environ.get("MAZE_SKILL_DIRS")
    if env_dirs:
        values.extend(item for item in env_dirs.split(os.pathsep) if item)
    if not values:
        cwd = Path.cwd()
        values = [
            cwd / "skills",
            cwd / ".maze" / "skills",
            Path.home() / ".maze" / "skills",
        ]

    normalized = []
    seen = set()
    for value in values:
        path = Path(value).expanduser().resolve()
        if str(path) in seen:
            continue
        seen.add(str(path))
        normalized.append(path)
    return normalized


def _read_skill_metadata(skill_path: Path) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    metadata_file = skill_path / "skill.json"
    if metadata_file.is_file():
        try:
            loaded = json.loads(metadata_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                metadata.update(loaded)
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentSkillError(
                "skill_metadata_invalid",
                f"Invalid skill metadata file: {metadata_file}",
                repairable=False,
                details={"path": str(metadata_file), "error": str(exc)},
            ) from exc

    skill_md = skill_path / "SKILL.md"
    if skill_md.is_file():
        try:
            front_matter, _ = _split_front_matter(skill_md.read_text(encoding="utf-8"))
            metadata.update(front_matter)
        except OSError:
            pass
    return metadata


def _split_front_matter(text: str) -> tuple[Dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    lines = text.splitlines()
    end_index = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end_index = index
            break
    if end_index is None:
        return {}, text

    metadata: Dict[str, Any] = {}
    for line in lines[1:end_index]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            metadata[key] = value
    body = "\n".join(lines[end_index + 1:]).lstrip("\n")
    if text.endswith("\n"):
        body += "\n"
    return metadata, body


def _description_from_file(path: Path) -> str:
    try:
        return _description_from_markdown(path.read_text(encoding="utf-8"))
    except OSError:
        return ""


def _description_from_markdown(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
        return stripped[:200]
    return ""


def _resource_manifest(skill_path: Path) -> List[Dict[str, Any]]:
    resources = []
    for dirname in SKILL_RESOURCE_DIRS:
        root = skill_path / dirname
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            resources.append({
                "kind": dirname.rstrip("s"),
                "path": str(path.relative_to(skill_path)),
                "size": size,
            })
    return resources


def _compact_instructions(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    omitted = len(text) - max_chars
    return text[:max_chars] + f"\n... [skill truncated {omitted} chars]", True
