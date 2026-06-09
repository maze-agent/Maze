from __future__ import annotations

import base64
import cloudpickle
import hashlib
import inspect
import json
import mimetypes
import textwrap
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List

from maze.client.maze.decorator import get_task_metadata
from maze.client.maze.dynamic import DynamicRun, DynamicTaskInvocation, DynamicTaskSpec
from maze.client.maze.agent_permissions import (
    AgentPermissionAction,
    AgentPermissionPolicy,
    normalize_workspace_relative_path,
)


DEFAULT_MAX_OBSERVATION_CHARS = 12000
DEFAULT_CALLABLE_RESOURCES = {"cpu": 1, "cpu_mem": 64, "gpu": 0, "gpu_mem": 0}
EXEC_CODE_RESOURCE_KEYS = ("cpu", "cpu_mem", "gpu", "gpu_mem")
EXEC_CODE_TARGET_NODE_KEYS = ("target_node_id", "node_id")


@dataclass
class AgentToolError:
    error_type: str
    message: str
    repairable: bool = True
    details: Dict[str, Any] = field(default_factory=dict)

    def to_observation(
        self,
        *,
        tool: str | None = None,
        args: Dict[str, Any] | None = None,
        available_tools: List[str] | None = None,
        action: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        observation = {
            "error": self.message,
            "error_type": self.error_type,
            "repairable": self.repairable,
            **self.details,
        }
        if tool is not None:
            observation["tool"] = tool
        if args is not None:
            observation["args"] = args
        if available_tools is not None:
            observation["available_tools"] = available_tools
        if action is not None:
            observation["action"] = action
        return observation

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error_type": self.error_type,
            "message": self.message,
            "repairable": self.repairable,
            "details": self.details,
        }


@dataclass
class AgentToolSpec:
    name: str
    description: str
    inputs: List[str]
    required_inputs: List[str]
    outputs: List[str]
    data_types: Dict[str, str]
    resources: Dict[str, Any]
    task_spec: DynamicTaskSpec | None
    timeout_seconds: float | None = None
    source: str = "task"
    handler: Any = None

    @property
    def input_schema(self) -> Dict[str, Any]:
        properties = {}
        for name in self.inputs:
            properties[name] = {
                "type": _json_type_for_data_type(self.data_types.get(name, "str")),
            }
        return {
            "type": "object",
            "properties": properties,
            "required": list(self.required_inputs),
            "additionalProperties": False,
        }

    def to_llm_spec(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputs": list(self.inputs),
            "required_inputs": list(self.required_inputs),
            "outputs": list(self.outputs),
            "data_types": dict(self.data_types),
            "resources": dict(self.resources),
            "timeout_seconds": self.timeout_seconds,
            "source": self.source,
            "input_schema": self.input_schema,
        }


@dataclass
class AgentToolResult:
    content: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    error: Dict[str, Any] | None = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "content": self.content,
            "metadata": self.metadata,
            "artifacts": self.artifacts,
            "attachments": self.attachments,
            "truncated": self.truncated,
            "error": self.error,
        }


@dataclass
class AgentToolCallResult:
    spec: AgentToolSpec
    task: DynamicTaskInvocation | "AgentToolLocalInvocation"
    finish_event: Dict[str, Any]
    raw_result: Any
    result: Any
    tool_result: AgentToolResult
    timings: Dict[str, Any]

    @property
    def observation(self) -> Dict[str, Any]:
        return {
            "tool": self.spec.name,
            "task_id": self.task.task_id,
            "result": self.result,
            "tool_result": self.tool_result.to_dict(),
            "artifacts": self.tool_result.artifacts,
        }


@dataclass
class AgentToolLocalInvocation:
    task_id: str
    task_name: str


class AgentToolRegistry:
    def __init__(self, dynamic_run: DynamicRun):
        self.dynamic_run = dynamic_run
        self._specs: Dict[str, AgentToolSpec] = {}

    @property
    def specs(self) -> Dict[str, AgentToolSpec]:
        return self._specs

    @property
    def task_specs(self) -> Dict[str, DynamicTaskSpec]:
        return {
            name: spec.task_spec
            for name, spec in self._specs.items()
            if spec.task_spec is not None
        }

    def register_task_tool(self, tool: Callable[..., Dict[str, Any]]) -> AgentToolSpec:
        if not hasattr(tool, "_maze_task_metadata"):
            return self.register_callable_tool(tool)

        metadata = get_task_metadata(tool)
        tool_name = metadata.func_name
        if tool_name in self._specs:
            raise ValueError(f"Duplicate agent tool name: {tool_name}")

        task_spec = self.dynamic_run.register_task_spec(
            tool,
            task_spec_id=tool_name,
            task_name=tool_name,
        )
        required_inputs = _required_inputs(tool, metadata.inputs)
        spec = AgentToolSpec(
            name=tool_name,
            description=(getattr(tool, "__doc__", None) or "").strip(),
            inputs=list(metadata.inputs),
            required_inputs=required_inputs,
            outputs=list(metadata.outputs),
            data_types=dict(metadata.data_types),
            resources=dict(metadata.resources),
            task_spec=task_spec,
            timeout_seconds=metadata.timeout_seconds,
            source="task",
        )
        self._specs[tool_name] = spec
        return spec

    def register_mcp_tool(self, tool: Any) -> AgentToolSpec:
        tool_name = tool.agent_tool_name
        if tool_name in self._specs:
            raise ValueError(f"Duplicate agent tool name: {tool_name}")

        inputs = sorted((tool.input_schema.get("properties") or {}).keys())
        required_inputs = sorted(tool.input_schema.get("required") or [])
        data_types = {
            name: _json_schema_type_to_data_type((tool.input_schema.get("properties") or {}).get(name) or {})
            for name in inputs
        }
        spec = AgentToolSpec(
            name=tool_name,
            description=tool.description,
            inputs=inputs,
            required_inputs=required_inputs,
            outputs=["structured_content", "content", "is_error"],
            data_types=data_types,
            resources=dict(DEFAULT_CALLABLE_RESOURCES),
            task_spec=None,
            source="mcp",
            handler=tool,
        )
        self._specs[tool_name] = spec
        return spec

    def register_skill_loader(self, skill_registry: Any) -> AgentToolSpec:
        tool_name = "load_skill"
        if tool_name in self._specs:
            raise ValueError(f"Duplicate agent tool name: {tool_name}")

        spec = AgentToolSpec(
            name=tool_name,
            description="Load a local Maze skill into the current agent context.",
            inputs=["name"],
            required_inputs=["name"],
            outputs=["skill", "loaded_skills"],
            data_types={"name": "str"},
            resources=dict(DEFAULT_CALLABLE_RESOURCES),
            task_spec=None,
            source="skill",
            handler=skill_registry,
        )
        self._specs[tool_name] = spec
        return spec

    def register_callable_tool(
        self,
        tool: Callable[..., Dict[str, Any]],
        *,
        name: str | None = None,
        description: str | None = None,
        outputs: List[str] | None = None,
        data_types: Dict[str, str] | None = None,
        resources: Dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> AgentToolSpec:
        tool_name = name or getattr(tool, "__name__", None) or tool.__class__.__name__
        if not tool_name or tool_name == "<lambda>":
            raise ValueError("Callable agent tools require a stable function name")
        if tool_name in self._specs:
            raise ValueError(f"Duplicate agent tool name: {tool_name}")

        task_resources = _normalize_callable_resources(resources)
        wrapped = _callable_to_task(
            tool,
            name=tool_name,
            outputs=outputs,
            data_types=data_types,
            resources=task_resources,
            timeout_seconds=timeout_seconds,
        )
        metadata = get_task_metadata(wrapped)
        task_spec = self.dynamic_run.register_task_spec(
            wrapped,
            task_spec_id=tool_name,
            task_name=tool_name,
        )
        spec = AgentToolSpec(
            name=tool_name,
            description=(description if description is not None else (getattr(tool, "__doc__", None) or "")).strip(),
            inputs=list(metadata.inputs),
            required_inputs=_required_inputs(tool, metadata.inputs),
            outputs=list(metadata.outputs),
            data_types=dict(metadata.data_types),
            resources=dict(metadata.resources),
            task_spec=task_spec,
            timeout_seconds=metadata.timeout_seconds,
            source="callable",
        )
        self._specs[tool_name] = spec
        return spec

    def require_tool(self, tool_name: str) -> AgentToolSpec:
        if tool_name not in self._specs:
            raise KeyError(tool_name)
        return self._specs[tool_name]

    def available_tool_names(self) -> List[str]:
        return sorted(self._specs)

    def tool_specs_for_llm(self) -> Dict[str, Dict[str, Any]]:
        return {
            name: spec.to_llm_spec()
            for name, spec in self._specs.items()
        }

    def validate_tool_call(self, tool_name: str, args: Dict[str, Any] | None) -> AgentToolError | None:
        if tool_name not in self._specs:
            return AgentToolError(
                error_type="tool_not_allowed",
                message=f"Agent tool is not allowed: {tool_name}",
                details={
                    "tool": tool_name,
                    "available_tools": self.available_tool_names(),
                },
            )

        spec = self._specs[tool_name]
        call_args = args or {}
        missing_inputs = sorted(set(spec.required_inputs) - set(call_args))
        unknown_inputs = sorted(set(call_args) - set(spec.inputs))

        if missing_inputs:
            return AgentToolError(
                error_type="missing_args",
                message=f"Missing required args for {tool_name}: {missing_inputs}",
                details={
                    "tool": tool_name,
                    "args": call_args,
                    "missing_args": missing_inputs,
                    "expected_args": sorted(spec.inputs),
                    "required_args": sorted(spec.required_inputs),
                },
            )

        if unknown_inputs:
            return AgentToolError(
                error_type="unknown_args",
                message=f"Unknown args for {tool_name}: {unknown_inputs}",
                details={
                    "tool": tool_name,
                    "args": call_args,
                    "unknown_args": unknown_inputs,
                    "expected_args": sorted(spec.inputs),
                    "required_args": sorted(spec.required_inputs),
                },
            )

        return None


class AgentToolRuntime:
    def __init__(
        self,
        dynamic_run: DynamicRun,
        registry: AgentToolRegistry,
        task_timeout: float | None = None,
        max_observation_chars: int = DEFAULT_MAX_OBSERVATION_CHARS,
        permission_policy: AgentPermissionPolicy | Dict[str, Any] | None = None,
    ):
        self.dynamic_run = dynamic_run
        self.registry = registry
        self.task_timeout = task_timeout
        self.max_observation_chars = max(1, int(max_observation_chars))
        if isinstance(permission_policy, AgentPermissionPolicy):
            self.permission_policy = permission_policy
        else:
            self.permission_policy = AgentPermissionPolicy.from_dict(permission_policy)

    def validate_tool_call(self, tool_name: str, args: Dict[str, Any] | None) -> AgentToolError | None:
        error = self.registry.validate_tool_call(tool_name, args)
        if error is not None:
            return error
        return self._validate_permission(tool_name, args or {})

    def repair_observation(
        self,
        error: AgentToolError,
        *,
        tool_name: str | None,
        args: Dict[str, Any] | None,
        action: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return error.to_observation(
            tool=tool_name,
            args=args or {},
            available_tools=self.registry.available_tool_names(),
            action=action,
        )

    def execute_task_tool(
        self,
        *,
        step: int,
        tool_name: str,
        args: Dict[str, Any] | None,
        mode: str,
        parents: List[DynamicTaskInvocation | str] | None = None,
        request_id: str | None = None,
        decision_task_id: str | None = None,
    ) -> AgentToolCallResult:
        spec = self.registry.require_tool(tool_name)
        call_args = dict(args or {})
        resource_override = exec_code_resource_override_from_args(spec, call_args)
        started_time = time.time()
        base_event = {
            "mode": mode,
            "step": step,
            "tool": tool_name,
            "args": call_args,
            "decision_task_id": decision_task_id,
        }
        if resource_override is not None:
            base_event["resource_override"] = resource_override
        self._emit_best_effort("agent_tool_call_started", base_event)

        try:
            if spec.source == "mcp":
                return self._execute_mcp_tool(
                    spec=spec,
                    step=step,
                    call_args=call_args,
                    mode=mode,
                    decision_task_id=decision_task_id,
                    base_event=base_event,
                    started_time=started_time,
                )
            if spec.source == "skill":
                return self._execute_skill_tool(
                    spec=spec,
                    step=step,
                    call_args=call_args,
                    mode=mode,
                    decision_task_id=decision_task_id,
                    base_event=base_event,
                    started_time=started_time,
                )

            append_kwargs = {
                "inputs": call_args,
                "parents": parents or [],
                "request_id": request_id,
            }
            if resource_override is not None:
                append_kwargs["resources"] = resource_override
            task = self.dynamic_run.append_task(
                spec.task_spec,
                **append_kwargs,
            )
            finish_event = self.dynamic_run.wait_for_task(task, timeout=self.task_timeout)
            finished_time = time.time()
            raw_result = finish_event.get("data", {}).get("result", {})
            compacted_result, truncations = compact_agent_tool_value(
                raw_result,
                max_chars=self.max_observation_chars,
            )
            artifacts = self._persist_truncated_output_artifacts(
                mode=mode,
                step=step,
                tool_name=tool_name,
                task_id=task.task_id,
                decision_task_id=decision_task_id,
                server_url=getattr(self.dynamic_run, "server_url", None),
                raw_result=raw_result,
                truncations=truncations,
            )
            tool_result = AgentToolResult(
                content=compacted_result,
                metadata={
                    "task_id": task.task_id,
                    "truncations": truncations,
                    "artifact_count": len(artifacts),
                },
                artifacts=artifacts,
                truncated=bool(truncations),
            )
            timings = {
                "tool_started_time": started_time,
                "tool_finished_time": finished_time,
                "tool_seconds": round(finished_time - started_time, 6),
            }

            if truncations:
                self._emit_best_effort("agent_tool_output_truncated", {
                    **base_event,
                    "task_id": task.task_id,
                    "truncations": truncations,
                    "artifacts": artifacts,
                })

            self._emit_best_effort("agent_tool_call_finished", {
                **base_event,
                "task_id": task.task_id,
                "result": compacted_result,
                "tool_result": tool_result.to_dict(),
                "timings": {
                    "tool_seconds": timings["tool_seconds"],
                },
            })

            return AgentToolCallResult(
                spec=spec,
                task=task,
                finish_event=finish_event,
                raw_result=raw_result,
                result=compacted_result,
                tool_result=tool_result,
                timings=timings,
            )
        except Exception as exc:
            self._emit_best_effort("agent_tool_call_failed", {
                **base_event,
                "error": {
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "repairable": False,
                },
                "timings": {
                    "tool_seconds": round(time.time() - started_time, 6),
                },
            })
            raise

    def _execute_mcp_tool(
        self,
        *,
        spec: AgentToolSpec,
        step: int,
        call_args: Dict[str, Any],
        mode: str,
        decision_task_id: str | None,
        base_event: Dict[str, Any],
        started_time: float,
    ) -> AgentToolCallResult:
        from maze.client.maze.agent_mcp import normalize_mcp_tool_result

        invocation = AgentToolLocalInvocation(
            task_id=f"mcp-{uuid.uuid4()}",
            task_name=spec.name,
        )
        self._emit_best_effort("agent_mcp_tool_call_started", {
            **base_event,
            "task_id": invocation.task_id,
            "server": getattr(spec.handler, "server_name", None),
            "mcp_tool": getattr(spec.handler, "tool_name", None),
        })

        try:
            raw_mcp_result = spec.handler(**call_args)
            raw_result = normalize_mcp_tool_result(raw_mcp_result)
            error = None
            if raw_result.get("is_error"):
                error = {
                    "error_type": "mcp_tool_error",
                    "message": _mcp_error_message(raw_result),
                    "repairable": True,
                }
                raw_result = {
                    **raw_result,
                    "error": error["message"],
                    "error_type": error["error_type"],
                    "repairable": True,
                }
        except Exception as exc:
            raw_result = {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "repairable": True,
            }
            error = {
                "error_type": type(exc).__name__,
                "message": str(exc),
                "repairable": True,
            }
            self._emit_best_effort("agent_tool_call_failed", {
                **base_event,
                "task_id": invocation.task_id,
                "error": error,
                "timings": {
                    "tool_seconds": round(time.time() - started_time, 6),
                },
            })

        finished_time = time.time()
        compacted_result, truncations = compact_agent_tool_value(
            raw_result,
            max_chars=self.max_observation_chars,
        )
        artifacts = self._persist_truncated_output_artifacts(
            mode=mode,
            step=step,
            tool_name=spec.name,
            task_id=invocation.task_id,
            decision_task_id=decision_task_id,
            server_url=getattr(self.dynamic_run, "server_url", None),
            raw_result=raw_result,
            truncations=truncations,
        )
        tool_result = AgentToolResult(
            content=compacted_result,
            metadata={
                "task_id": invocation.task_id,
                "truncations": truncations,
                "artifact_count": len(artifacts),
                "server": getattr(spec.handler, "server_name", None),
                "mcp_tool": getattr(spec.handler, "tool_name", None),
            },
            artifacts=artifacts,
            attachments=_mcp_content_attachments(compacted_result),
            truncated=bool(truncations),
            error=error,
        )
        timings = {
            "tool_started_time": started_time,
            "tool_finished_time": finished_time,
            "tool_seconds": round(finished_time - started_time, 6),
        }

        if truncations:
            self._emit_best_effort("agent_tool_output_truncated", {
                **base_event,
                "task_id": invocation.task_id,
                "truncations": truncations,
                "artifacts": artifacts,
            })

        self._emit_best_effort("agent_mcp_tool_call_finished", {
            **base_event,
            "task_id": invocation.task_id,
            "server": getattr(spec.handler, "server_name", None),
            "mcp_tool": getattr(spec.handler, "tool_name", None),
            "timings": {
                "tool_seconds": timings["tool_seconds"],
            },
        })
        self._emit_best_effort("agent_tool_call_finished", {
            **base_event,
            "task_id": invocation.task_id,
            "result": compacted_result,
            "tool_result": tool_result.to_dict(),
            "timings": {
                "tool_seconds": timings["tool_seconds"],
            },
        })

        return AgentToolCallResult(
            spec=spec,
            task=invocation,
            finish_event={
                "type": "finish_mcp_tool",
                "data": {
                    "task_id": invocation.task_id,
                    "result": compacted_result,
                },
            },
            raw_result=raw_result,
            result=compacted_result,
            tool_result=tool_result,
            timings=timings,
        )

    def _execute_skill_tool(
        self,
        *,
        spec: AgentToolSpec,
        step: int,
        call_args: Dict[str, Any],
        mode: str,
        decision_task_id: str | None,
        base_event: Dict[str, Any],
        started_time: float,
    ) -> AgentToolCallResult:
        invocation = AgentToolLocalInvocation(
            task_id=f"skill-{uuid.uuid4()}",
            task_name=spec.name,
        )
        skill_name = str(call_args.get("name") or "")
        error = None

        try:
            raw_result = spec.handler.load_tool(skill_name)
            loaded_skill = raw_result.get("skill") or {}
            self._emit_best_effort("agent_skill_loaded", {
                "mode": mode,
                "step": step,
                "task_id": invocation.task_id,
                "decision_task_id": decision_task_id,
                "skill": _skill_event_payload(loaded_skill),
                "source": "tool",
            })
        except Exception as exc:
            error = _skill_exception_payload(exc)
            raw_result = {
                "error": error["message"],
                "error_type": error["error_type"],
                "repairable": error["repairable"],
                "details": error.get("details", {}),
            }
            self._emit_best_effort("agent_skill_load_failed", {
                "mode": mode,
                "step": step,
                "task_id": invocation.task_id,
                "decision_task_id": decision_task_id,
                "skill": skill_name,
                "error": error,
                "source": "tool",
            })

        finished_time = time.time()
        compacted_result, truncations = compact_agent_tool_value(
            raw_result,
            max_chars=self.max_observation_chars,
        )
        artifacts = self._persist_truncated_output_artifacts(
            mode=mode,
            step=step,
            tool_name=spec.name,
            task_id=invocation.task_id,
            decision_task_id=decision_task_id,
            server_url=getattr(self.dynamic_run, "server_url", None),
            raw_result=raw_result,
            truncations=truncations,
        )
        tool_result = AgentToolResult(
            content=compacted_result,
            metadata={
                "task_id": invocation.task_id,
                "truncations": truncations,
                "artifact_count": len(artifacts),
                "skill": skill_name,
            },
            artifacts=artifacts,
            truncated=bool(truncations),
            error=error,
        )
        timings = {
            "tool_started_time": started_time,
            "tool_finished_time": finished_time,
            "tool_seconds": round(finished_time - started_time, 6),
        }

        if truncations:
            self._emit_best_effort("agent_tool_output_truncated", {
                **base_event,
                "task_id": invocation.task_id,
                "truncations": truncations,
                "artifacts": artifacts,
            })

        self._emit_best_effort("agent_tool_call_finished", {
            **base_event,
            "task_id": invocation.task_id,
            "result": compacted_result,
            "tool_result": tool_result.to_dict(),
            "timings": {
                "tool_seconds": timings["tool_seconds"],
            },
        })

        return AgentToolCallResult(
            spec=spec,
            task=invocation,
            finish_event={
                "type": "finish_skill_tool",
                "data": {
                    "task_id": invocation.task_id,
                    "result": compacted_result,
                },
            },
            raw_result=raw_result,
            result=compacted_result,
            tool_result=tool_result,
            timings=timings,
        )

    def emit_repair(
        self,
        *,
        mode: str,
        step: int,
        tool_name: str | None,
        args: Dict[str, Any] | None,
        observation: Dict[str, Any],
        decision_task_id: str | None = None,
    ) -> None:
        self._emit_best_effort("agent_tool_call_repair", {
            "mode": mode,
            "step": step,
            "tool": tool_name,
            "args": args or {},
            "decision_task_id": decision_task_id,
            "result": observation,
        })

    def _emit_best_effort(self, event_type: str, data: Dict[str, Any]) -> None:
        try:
            self.dynamic_run.emit_event(event_type, data)
        except Exception:
            pass

    def _validate_permission(self, tool_name: str, args: Dict[str, Any]) -> AgentToolError | None:
        try:
            spec = self.registry.require_tool(tool_name)
        except KeyError:
            return None

        permission, target = _permission_target_for_tool(spec, args)
        if not permission:
            return None

        decision = self.permission_policy.decide(permission, target)
        self._emit_best_effort("agent_permission_checked", {
            "tool": tool_name,
            "permission": decision.to_dict(),
            "args": args,
        })
        if decision.action == AgentPermissionAction.ASK:
            self._emit_best_effort("agent_permission_ask", {
                "tool": tool_name,
                "permission": decision.to_dict(),
                "args": args,
            })
        if decision.allowed:
            self._emit_best_effort("agent_permission_allowed", {
                "tool": tool_name,
                "permission": decision.to_dict(),
            })
            return None

        self._emit_best_effort("agent_permission_denied", {
            "tool": tool_name,
            "permission": decision.to_dict(),
            "args": args,
        })
        return AgentToolError(
            error_type="permission_denied",
            message=f"Agent permission denied for {permission}: {target}",
            repairable=True,
            details={
                "permission": decision.to_dict(),
            },
        )

    def _persist_truncated_output_artifacts(
        self,
        *,
        mode: str,
        step: int,
        tool_name: str,
        task_id: str,
        decision_task_id: str | None,
        server_url: str | None,
        raw_result: Any,
        truncations: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if not truncations:
            return []

        try:
            artifact = _write_agent_tool_output_artifact(
                run_id=self.dynamic_run.run_id,
                task_id=task_id,
                tool_name=tool_name,
                server_url=server_url,
                raw_result=raw_result,
                truncations=truncations,
            )
        except Exception as exc:
            self._emit_best_effort("agent_tool_output_artifact_failed", {
                "mode": mode,
                "step": step,
                "tool": tool_name,
                "task_id": task_id,
                "decision_task_id": decision_task_id,
                "error": {
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "repairable": False,
                },
            })
            return []

        self._emit_best_effort("agent_tool_output_artifact", {
            "mode": mode,
            "step": step,
            "tool": tool_name,
            "task_id": task_id,
            "decision_task_id": decision_task_id,
            "artifact": artifact,
            "truncations": truncations,
        })
        return [artifact]


def exec_code_resource_override_from_args(
    spec: AgentToolSpec,
    args: Dict[str, Any],
) -> Dict[str, Any] | None:
    if spec.name != "exec_code" or spec.task_spec is None:
        return None

    input_names = set(spec.inputs)
    override: Dict[str, Any] = {}
    for key in EXEC_CODE_RESOURCE_KEYS:
        if key in input_names and key in args and args.get(key) not in (None, ""):
            override[key] = _agent_resource_number(args.get(key), default=spec.resources.get(key, 0))

    target_node_id = None
    for key in EXEC_CODE_TARGET_NODE_KEYS:
        if key in input_names and args.get(key):
            target_node_id = str(args.get(key))
            break
    if target_node_id:
        override["target_node_id"] = target_node_id

    backend = str(args.get("backend") or "").strip().lower().replace("-", "_")
    if backend in {"docker", "ray_docker"}:
        override["required_capability"] = "docker_sandbox"
    elif backend in {"", "local", "workspace_sandbox"}:
        override["required_capability"] = "workspace_sandbox"

    if override.get("cpu", 1) < 1:
        override["cpu"] = 1
    if override.get("cpu_mem", 0) < 0:
        override["cpu_mem"] = 0
    if override.get("gpu", 0) < 0:
        override["gpu"] = 0
    if override.get("gpu", 0) > 1:
        override["gpu"] = 1
    if override.get("gpu_mem", 0) < 0:
        override["gpu_mem"] = 0
    if override.get("gpu_mem", 0) > 0 and override.get("gpu", 0) < 1:
        override["gpu"] = 1

    return override or None


def _agent_resource_number(value: Any, default: Any = 0) -> int | float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed.is_integer():
        return int(parsed)
    return parsed


def compact_agent_tool_value(value: Any, max_chars: int = DEFAULT_MAX_OBSERVATION_CHARS):
    truncations: List[Dict[str, Any]] = []

    def compact(item: Any, path: str) -> Any:
        if isinstance(item, str):
            if len(item) <= max_chars:
                return item
            omitted = len(item) - max_chars
            truncations.append({
                "path": path or "$",
                "original_chars": len(item),
                "returned_chars": max_chars,
                "omitted_chars": omitted,
            })
            return item[:max_chars] + f"\n... [truncated {omitted} chars]"

        if isinstance(item, dict):
            return {
                key: compact(child, f"{path}.{key}" if path else str(key))
                for key, child in item.items()
            }

        if isinstance(item, list):
            return [
                compact(child, f"{path}[{index}]")
                for index, child in enumerate(item)
            ]

        if isinstance(item, tuple):
            return tuple(
                compact(child, f"{path}[{index}]")
                for index, child in enumerate(item)
            )

        return item

    return compact(value, ""), truncations


def _callable_to_task(
    tool: Callable[..., Dict[str, Any]],
    *,
    name: str,
    outputs: List[str] | None,
    data_types: Dict[str, str] | None,
    resources: Dict[str, Any] | None,
    timeout_seconds: float | None,
) -> Callable[..., Dict[str, Any]]:
    signature = inspect.signature(tool)
    parameters = [
        parameter.replace(annotation=inspect.Signature.empty)
        for parameter in signature.parameters.values()
    ]
    wrapper_signature = inspect.Signature(parameters=parameters)
    output_names = outputs or _infer_callable_outputs(tool)

    def callable_tool_wrapper(**kwargs):
        result = tool(**kwargs)
        if isinstance(result, dict):
            return result
        if len(output_names) == 1:
            return {output_names[0]: result}
        raise TypeError(
            f"Callable agent tool {name} returned {type(result).__name__}; "
            "non-dict results require exactly one output key."
        )

    callable_tool_wrapper.__name__ = name
    callable_tool_wrapper.__qualname__ = name
    callable_tool_wrapper.__signature__ = wrapper_signature
    _attach_callable_task_metadata(
        callable_tool_wrapper,
        tool,
        name=name,
        outputs=output_names,
        data_types=data_types,
        resources=resources,
        timeout_seconds=timeout_seconds,
    )
    return callable_tool_wrapper


def _attach_callable_task_metadata(
    wrapper: Callable[..., Dict[str, Any]],
    tool: Callable[..., Any],
    *,
    name: str,
    outputs: List[str],
    data_types: Dict[str, str] | None,
    resources: Dict[str, Any],
    timeout_seconds: float | None,
) -> None:
    from maze.client.maze.decorator import TaskMetadata

    signature = inspect.signature(tool)
    inputs = [
        parameter.name
        for parameter in signature.parameters.values()
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    ]
    for parameter in signature.parameters.values():
        if parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        ):
            raise ValueError(
                f"Callable agent tool {name} must use explicit named parameters; "
                f"unsupported parameter: {parameter.name}"
            )

    type_map = dict(data_types or {})
    hints = getattr(tool, "__annotations__", {}) or {}
    for input_name in inputs:
        type_map.setdefault(input_name, _annotation_to_data_type(hints.get(input_name, inspect.Signature.empty)))
    for output_name in outputs:
        type_map.setdefault(output_name, "any")

    source = _callable_source(tool)
    runtime_func_name = getattr(tool, "__name__", name)
    if source:
        def runtime_callable(task_input_data: Dict[str, Any] | None = None) -> Dict[str, Any]:
            namespace: Dict[str, Any] = {}
            exec(source, namespace)
            func = namespace.get(runtime_func_name)
            if func is None:
                raise NameError(f"Callable agent tool function not found after source load: {runtime_func_name}")
            kwargs = dict(task_input_data or {})
            result = func(**kwargs)
            if isinstance(result, dict):
                return result
            if len(outputs) == 1:
                return {outputs[0]: result}
            raise TypeError(
                f"Callable agent tool {name} returned {type(result).__name__}; "
                "non-dict results require exactly one output key."
            )
    else:
        def runtime_callable(task_input_data: Dict[str, Any] | None = None) -> Dict[str, Any]:
            kwargs = dict(task_input_data or {})
            result = tool(**kwargs)
            if isinstance(result, dict):
                return result
            if len(outputs) == 1:
                return {outputs[0]: result}
            raise TypeError(
                f"Callable agent tool {name} returned {type(result).__name__}; "
                "non-dict results require exactly one output key."
            )

    code_ser = base64.b64encode(cloudpickle.dumps(runtime_callable)).decode("utf-8")
    wrapper._maze_task_metadata = TaskMetadata(
        func=wrapper,
        func_name=name,
        code_str=_callable_code_preview(tool, name),
        code_ser=code_ser,
        inputs=inputs,
        outputs=list(outputs),
        resources=dict(resources),
        data_types=type_map,
        timeout_seconds=timeout_seconds,
    )


def _write_agent_tool_output_artifact(
    *,
    run_id: str,
    task_id: str,
    tool_name: str,
    server_url: str | None,
    raw_result: Any,
    truncations: List[Dict[str, Any]],
) -> Dict[str, Any]:
    from maze.core.files.artifact_store import LocalCASArtifactStore, artifact_uri, sha256_bytes
    from maze.core.scheduler.result_summary import to_json_safe

    payload = {
        "schema": "maze_agent_tool_output",
        "schema_version": 1,
        "run_id": run_id,
        "task_id": task_id,
        "tool": tool_name,
        "created_time": time.time(),
        "truncations": truncations,
        "result": to_json_safe(raw_result),
    }
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    sha = sha256_bytes(data)
    artifact_info = _put_agent_tool_output_bytes(server_url, sha, data)
    path = f"agent_tool_outputs/{task_id}-{tool_name}.json"
    mime_type, _ = mimetypes.guess_type(path)
    return {
        "path": path,
        "name": Path(path).name,
        "size": artifact_info["size"],
        "sha256": sha,
        "artifact_id": artifact_info["artifact_id"],
        "storage_uri": artifact_uri(sha),
        "uri": f"maze://runs/{run_id}/artifacts/tasks/{task_id}/{path}",
        "run_id": run_id,
        "task_id": task_id,
        "producer_task_id": task_id,
        "tool": tool_name,
        "mime": mime_type or "application/json",
        "kind": "agent_tool_output",
    }


def _put_agent_tool_output_bytes(server_url: str | None, sha: str, data: bytes) -> Dict[str, Any]:
    if server_url:
        import requests

        response = requests.put(
            f"{server_url.rstrip('/')}/artifacts/sha256/{sha}",
            data=data,
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    from maze.core.files.artifact_store import LocalCASArtifactStore

    return LocalCASArtifactStore().put_bytes(sha, data)


def _callable_code_preview(tool: Callable[..., Any], name: str) -> str:
    source = _callable_source(tool)
    if source:
        return source
    digest = hashlib.sha256(repr(tool).encode("utf-8")).hexdigest()[:12]
    return f"# Callable agent tool {name}; source unavailable; id={digest}"


def _callable_source(tool: Callable[..., Any]) -> str:
    try:
        return textwrap.dedent(inspect.getsource(tool))
    except (OSError, TypeError):
        return ""


def _annotation_to_data_type(annotation: Any) -> str:
    if annotation is inspect.Signature.empty:
        return "str"
    if annotation is None:
        return "None"
    if isinstance(annotation, str):
        return annotation
    name = getattr(annotation, "__name__", None)
    if name:
        return name
    return str(annotation).replace("typing.", "")


def _normalize_callable_resources(resources: Dict[str, Any] | None) -> Dict[str, Any]:
    normalized = dict(DEFAULT_CALLABLE_RESOURCES)
    normalized.update(resources or {})
    return normalized


def _infer_callable_outputs(tool: Callable[..., Any]) -> List[str]:
    try:
        from maze.client.maze.decorator import infer_output_keys_from_source

        return infer_output_keys_from_source(tool)
    except Exception:
        return ["result"]


def _required_inputs(tool: Callable[..., Dict[str, Any]], input_names: List[str]) -> List[str]:
    required = []
    signature = inspect.signature(tool)
    allowed = set(input_names)
    for parameter in signature.parameters.values():
        if parameter.name in allowed and parameter.default is inspect.Signature.empty:
            required.append(parameter.name)
    return sorted(required)


def _json_type_for_data_type(data_type: str) -> str:
    normalized = str(data_type or "").lower()
    if normalized in {"int", "integer"}:
        return "integer"
    if normalized in {"float", "number"}:
        return "number"
    if normalized in {"bool", "boolean"}:
        return "boolean"
    if normalized in {"dict", "object"}:
        return "object"
    if normalized in {"list", "array", "tuple"}:
        return "array"
    return "string"


def _json_schema_type_to_data_type(schema: Dict[str, Any]) -> str:
    value = schema.get("type", "string")
    if isinstance(value, list):
        value = next((item for item in value if item != "null"), value[0] if value else "string")
    normalized = str(value or "string")
    if normalized == "integer":
        return "int"
    if normalized == "number":
        return "float"
    if normalized == "boolean":
        return "bool"
    if normalized == "object":
        return "dict"
    if normalized == "array":
        return "list"
    return "str"


def _permission_target_for_tool(spec: AgentToolSpec, args: Dict[str, Any]) -> tuple[str | None, str]:
    if spec.name == "read_file":
        raw_path = str(args.get("path") or "")
        try:
            return "read", normalize_workspace_relative_path(raw_path)
        except Exception:
            return "external_directory", raw_path
    if spec.name == "write_file":
        raw_path = str(args.get("path") or "")
        try:
            return "write", normalize_workspace_relative_path(raw_path)
        except Exception:
            return "external_directory", raw_path
    if spec.name == "exec_code":
        path = str(args.get("path") or "").strip()
        if path:
            try:
                return "exec_code", normalize_workspace_relative_path(path)
            except Exception:
                return "external_directory", path
        code = str(args.get("code") or "").strip()
        first_line = code.splitlines()[0] if code else ""
        return "exec_code", f"python {first_line}".strip()
    if spec.source == "mcp":
        return "mcp", spec.name
    if spec.source == "skill":
        return "skill", str(args.get("name") or spec.name)
    return None, ""


def _mcp_error_message(result: Dict[str, Any]) -> str:
    content = result.get("content") or []
    for item in content:
        if isinstance(item, dict) and item.get("text"):
            return str(item["text"])
    if result.get("structured_content"):
        return json.dumps(result["structured_content"], ensure_ascii=False)
    return "MCP tool returned an error"


def _mcp_content_attachments(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    attachments = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"image", "audio", "resource", "resource_link"}:
            attachments.append(item)
    return attachments


def _skill_event_payload(skill: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": skill.get("name"),
        "description": skill.get("description"),
        "resources": skill.get("resources") or [],
        "metadata": skill.get("metadata") or {},
        "truncated": bool(skill.get("truncated")),
        "original_chars": skill.get("original_chars"),
        "returned_chars": skill.get("returned_chars"),
    }


def _skill_exception_payload(exc: Exception) -> Dict[str, Any]:
    if hasattr(exc, "to_dict"):
        payload = exc.to_dict()
        details = payload.get("details") if isinstance(payload, dict) else {}
        return {
            "error_type": payload.get("error_type", type(exc).__name__),
            "message": payload.get("message", str(exc)),
            "repairable": bool(payload.get("repairable", True)),
            "details": details or {},
        }
    return {
        "error_type": type(exc).__name__,
        "message": str(exc),
        "repairable": True,
        "details": {},
    }
