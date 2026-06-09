from __future__ import annotations

import fnmatch
import os
import posixpath
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List


class AgentPermissionAction(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass
class AgentPermissionRule:
    permission: str
    pattern: str
    action: AgentPermissionAction

    @classmethod
    def from_value(cls, permission: str, pattern: str, action: Any) -> "AgentPermissionRule":
        return cls(
            permission=str(permission or "").strip(),
            pattern=str(pattern or "*").strip() or "*",
            action=AgentPermissionAction(str(action or AgentPermissionAction.DENY.value).strip().lower()),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "permission": self.permission,
            "pattern": self.pattern,
            "action": self.action.value,
        }


@dataclass
class AgentPermissionDecision:
    permission: str
    target: str
    action: AgentPermissionAction
    allowed: bool
    pattern: str | None = None
    reason: str | None = None
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "permission": self.permission,
            "target": self.target,
            "action": self.action.value,
            "allowed": self.allowed,
            "pattern": self.pattern,
            "reason": self.reason,
            "details": dict(self.details),
        }


class AgentPermissionDenied(PermissionError):
    def __init__(self, decision: AgentPermissionDecision):
        self.decision = decision
        super().__init__(
            f"Agent permission denied for {decision.permission}: "
            f"{decision.target} ({decision.reason or decision.action.value})"
        )


class AgentSandboxPathError(ValueError):
    def __init__(self, message: str, *, target: str = ""):
        self.target = target
        super().__init__(message)


class AgentPermissionPolicy:
    def __init__(self, rules: Iterable[AgentPermissionRule] | None = None):
        self.rules = list(rules or [])

    @classmethod
    def default(cls) -> "AgentPermissionPolicy":
        rules: List[AgentPermissionRule] = []
        for permission in ("read", "write"):
            rules.append(AgentPermissionRule.from_value(permission, "*", "allow"))
            for pattern in _sensitive_path_patterns():
                rules.append(AgentPermissionRule.from_value(permission, pattern, "deny"))
        rules.extend([
            AgentPermissionRule.from_value("exec_code", "*", "allow"),
            AgentPermissionRule.from_value("external_directory", "*", "deny"),
            AgentPermissionRule.from_value("mcp", "*", "ask"),
            AgentPermissionRule.from_value("skill", "*", "allow"),
        ])
        return cls(rules)

    @classmethod
    def from_dict(cls, data: Dict[str, Any] | None) -> "AgentPermissionPolicy":
        if not data:
            return cls.default()

        permission_config = data.get("permission", data)
        if not isinstance(permission_config, dict):
            return cls.default()

        rules: List[AgentPermissionRule] = []
        for permission, section in permission_config.items():
            if isinstance(section, dict):
                for pattern, action in section.items():
                    try:
                        rules.append(AgentPermissionRule.from_value(permission, pattern, action))
                    except ValueError:
                        continue
            elif isinstance(section, list):
                for item in section:
                    if not isinstance(item, dict):
                        continue
                    try:
                        rules.append(AgentPermissionRule.from_value(
                            permission,
                            item.get("pattern", "*"),
                            item.get("action", "deny"),
                        ))
                    except ValueError:
                        continue
        return cls(rules) if rules else cls.default()

    @classmethod
    def load_for_workspace(cls, workspace_dir: str | os.PathLike[str] | None) -> "AgentPermissionPolicy":
        if not workspace_dir:
            return cls.default()
        workspace_path = Path(workspace_dir).expanduser()
        policy_paths = [
            workspace_path / "policies" / "sandbox_policy.json",
            workspace_path / "sandbox_policy.json",
        ]
        policy_path = next((candidate for candidate in policy_paths if candidate.exists()), None)
        if policy_path is None:
            return cls.default()
        try:
            import json

            payload = json.loads(policy_path.read_text(encoding="utf-8"))
            return cls.from_dict(payload)
        except Exception:
            return cls.default()

    def decide(
        self,
        permission: str,
        target: str,
        *,
        ask_default: str | None = None,
    ) -> AgentPermissionDecision:
        normalized_permission = str(permission or "").strip()
        normalized_target = str(target or "").strip() or "*"
        matched_rule: AgentPermissionRule | None = None
        for rule in self.rules:
            if rule.permission != normalized_permission:
                continue
            if fnmatch.fnmatchcase(normalized_target, rule.pattern):
                matched_rule = rule

        if matched_rule is None:
            matched_rule = AgentPermissionRule.from_value(normalized_permission, "*", "ask")

        action = matched_rule.action
        if action == AgentPermissionAction.ALLOW:
            allowed = True
            reason = "allowed"
        elif action == AgentPermissionAction.DENY:
            allowed = False
            reason = "denied"
        else:
            effective = _effective_ask_default(ask_default)
            allowed = effective == AgentPermissionAction.ALLOW
            reason = f"ask_default_{effective.value}"

        return AgentPermissionDecision(
            permission=normalized_permission,
            target=normalized_target,
            action=action,
            allowed=allowed,
            pattern=matched_rule.pattern,
            reason=reason,
            details={"rule": matched_rule.to_dict()},
        )

    def require_allowed(
        self,
        permission: str,
        target: str,
        *,
        ask_default: str | None = None,
    ) -> AgentPermissionDecision:
        decision = self.decide(permission, target, ask_default=ask_default)
        if not decision.allowed:
            raise AgentPermissionDenied(decision)
        return decision

    def to_dict(self) -> Dict[str, Any]:
        return {"rules": [rule.to_dict() for rule in self.rules]}


def normalize_workspace_relative_path(path: str) -> str:
    raw = str(path or "").strip()
    if not raw:
        raise AgentSandboxPathError("path is required", target=raw)

    candidate = raw.replace("\\", "/")
    if os.path.isabs(raw) or candidate.startswith("/") or _looks_like_windows_absolute(candidate):
        raise AgentSandboxPathError("path must be relative to workspace/files", target=raw)

    normalized = posixpath.normpath(candidate)
    if normalized in {"", "."}:
        raise AgentSandboxPathError("path is required", target=raw)
    if normalized == ".." or normalized.startswith("../") or "/../" in normalized:
        raise AgentSandboxPathError("path must stay inside workspace/files", target=raw)
    return normalized


def permission_error_payload(exc: Exception) -> Dict[str, Any]:
    if isinstance(exc, AgentPermissionDenied):
        return {
            "error_type": "permission_denied",
            "permission": exc.decision.to_dict(),
        }
    if isinstance(exc, AgentSandboxPathError):
        return {
            "error_type": "sandbox_path_error",
            "target": exc.target,
        }
    return {"error_type": type(exc).__name__}


def _sensitive_path_patterns() -> List[str]:
    base = [
        ".env",
        ".env.*",
        "*secret*",
        "*credential*",
        "*token*",
        "api_key*",
    ]
    nested = []
    for pattern in base:
        nested.append(f"*/{pattern}")
    return base + nested


def _looks_like_windows_absolute(value: str) -> bool:
    return len(value) >= 3 and value[1] == ":" and value[2] == "/" and value[0].isalpha()


def _effective_ask_default(value: str | None) -> AgentPermissionAction:
    requested = str(value or os.environ.get("MAZE_AGENT_PERMISSION_ASK_DEFAULT") or "allow").strip().lower()
    if requested == AgentPermissionAction.DENY.value:
        return AgentPermissionAction.DENY
    return AgentPermissionAction.ALLOW
