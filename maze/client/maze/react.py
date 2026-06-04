from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

from maze.client.maze.decorator import get_task_metadata
from maze.client.maze.dynamic import DynamicRun, DynamicTaskInvocation, DynamicTaskSpec
from maze.client.maze.skills import (
    SkillSpec,
    build_skill_catalog,
    build_skill_reader_tool,
    normalize_skills,
)


@dataclass
class ReActStep:
    index: int
    decision_task_id: str
    decision: Dict[str, Any]
    tool_name: str | None = None
    args: Dict[str, Any] = field(default_factory=dict)
    tool_task_id: str | None = None
    observation: Dict[str, Any] | None = None
    timings: Dict[str, Any] = field(default_factory=dict)


class ReActWorkflow:
    """A minimal ReAct workflow template backed by DynamicRun.

    The LLM decision function is itself a Maze @task. It should return either:
    - {"action": {"tool": "<tool-name>", "args": {...}}}
    - {"action": {"final": ...}}

    For convenience, direct {"tool": ...} and {"final": ...} payloads are also
    accepted.
    """

    def __init__(
        self,
        dynamic_run: DynamicRun,
        llm_task: Callable[..., Dict[str, Any]],
        tools: List[Callable[..., Dict[str, Any]]],
        max_steps: int = 10,
        system_prompt: str | None = None,
        skills: List[SkillSpec | str] | None = None,
        progressive_skills: bool = True,
        skill_reader_max_chars: int = 12000,
        task_timeout: float | None = None,
    ):
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")

        self.dynamic_run = dynamic_run
        self.llm_task = llm_task
        self.max_steps = max_steps
        self.system_prompt = system_prompt
        self.task_timeout = task_timeout
        self.skills = normalize_skills(skills)
        self.progressive_skills = progressive_skills
        self.steps: List[ReActStep] = []
        self.tool_specs: Dict[str, Dict[str, Any]] = {}
        self._registered_tools: Dict[str, DynamicTaskSpec] = {}
        self._tool_input_names: Dict[str, set[str]] = {}
        self._tool_required_inputs: Dict[str, set[str]] = {}

        llm_metadata = get_task_metadata(llm_task)
        self._llm_input_names = set(llm_metadata.inputs)
        self._llm_spec = self.dynamic_run.register_task_spec(
            llm_task,
            task_spec_id="react_llm_decision",
            task_name=llm_metadata.func_name,
        )

        all_tools = list(tools)
        if self.skills and self.progressive_skills:
            all_tools.append(build_skill_reader_tool(self.skills, max_chars=skill_reader_max_chars))

        for tool in all_tools:
            self._register_tool(tool)

        if not self._registered_tools:
            raise ValueError("ReActWorkflow requires at least one @task tool")

        self.skill_catalog = build_skill_catalog(self.skills)

    @property
    def run_id(self) -> str:
        return self.dynamic_run.run_id

    def run(self, prompt: str) -> Any:
        run_started = time.time()
        try:
            self.dynamic_run.emit_event("agent_run_started", {
                "mode": "react",
                "prompt": prompt,
                "max_steps": self.max_steps,
                "llm_task": self._llm_spec.task_name,
                "tools": sorted(self._registered_tools),
                "skills": sorted(self.skill_catalog),
                "progressive_skills": self.progressive_skills,
            })

            for step_index in range(1, self.max_steps + 1):
                decision_event = self._run_llm_decision(step_index, prompt)
                decision_task = decision_event["task"]
                decision = decision_event["decision"]
                try:
                    action = self._parse_action(decision)
                except Exception as exc:
                    action = {"error": str(exc)}

                self.dynamic_run.emit_event("react_llm_decision", {
                    "step": step_index,
                    "task_id": decision_task.task_id,
                    "decision": decision,
                    "action": action,
                    "timings": decision_event.get("timings"),
                })

                if "error" in action:
                    self.steps.append(self._record_repair_step(
                        step_index=step_index,
                        decision_task=decision_task,
                        decision=decision,
                        action=action,
                        error=str(action["error"]),
                        error_type="invalid_action",
                        details={"timings": decision_event.get("timings")},
                    ))
                    continue

                if "final" in action:
                    answer = action["final"]
                    total_seconds = time.time() - run_started
                    timings = self._run_timing_summary(
                        total_seconds,
                        extra_timings=decision_event.get("timings"),
                    )
                    artifacts = self._artifact_summary()
                    self.dynamic_run.emit_event("agent_final", {
                        "mode": "react",
                        "answer": answer,
                        "step_count": len(self.steps),
                        "stop_reason": "final",
                        "timings": timings,
                        "artifacts": artifacts,
                    })
                    self.dynamic_run.finalize({
                        "mode": "react",
                        "answer": answer,
                        "stop_reason": "final",
                        "step_count": len(self.steps),
                        "timings": timings,
                        "artifacts": artifacts,
                        "steps": [self._step_snapshot(step) for step in self.steps],
                    })
                    return answer

                repair = self._validate_tool_action(action)
                if repair is not None:
                    self.steps.append(self._record_repair_step(
                        step_index=step_index,
                        decision_task=decision_task,
                        decision=decision,
                        action=action,
                        error=repair["error"],
                        error_type=repair["error_type"],
                        details={**repair, "timings": decision_event.get("timings")},
                    ))
                    continue

                step = self._run_tool_step(step_index, decision_task, decision, action)
                step.timings = {
                    **(decision_event.get("timings") or {}),
                    **(step.timings or {}),
                }
                self.steps.append(step)

            raise RuntimeError(f"ReAct workflow exceeded max_steps={self.max_steps} without a final answer")
        except Exception as exc:
            self._emit_best_effort("agent_error", {
                "mode": "react",
                "error": str(exc),
                "stop_reason": "max_steps" if "max_steps" in str(exc) else "controller_error",
                "elapsed_seconds": round(time.time() - run_started, 6),
            })
            self._cancel_if_active("ReAct workflow controller error")
            raise

    def status(self) -> Dict[str, Any]:
        return self.dynamic_run.get_status()

    def get_events(self, after: int | None = None) -> List[Dict[str, Any]]:
        return self.dynamic_run.get_events(after=after)

    def cancel(self, reason: str | None = None):
        return self.dynamic_run.cancel(reason)

    def _register_tool(self, tool: Callable[..., Dict[str, Any]]):
        metadata = get_task_metadata(tool)
        tool_name = metadata.func_name
        if tool_name in self._registered_tools:
            raise ValueError(f"Duplicate ReAct tool name: {tool_name}")

        task_spec = self.dynamic_run.register_task_spec(
            tool,
            task_spec_id=tool_name,
            task_name=tool_name,
        )
        self._registered_tools[tool_name] = task_spec
        input_names = set(metadata.inputs)
        required_inputs = set()
        signature = inspect.signature(tool)
        for parameter in signature.parameters.values():
            if parameter.name in input_names and parameter.default is inspect.Signature.empty:
                required_inputs.add(parameter.name)

        self._tool_input_names[tool_name] = input_names
        self._tool_required_inputs[tool_name] = required_inputs
        self.tool_specs[tool_name] = {
            "name": tool_name,
            "description": (getattr(tool, "__doc__", None) or "").strip(),
            "inputs": metadata.inputs,
            "required_inputs": sorted(required_inputs),
            "outputs": metadata.outputs,
            "data_types": metadata.data_types,
            "resources": metadata.resources,
        }

    def _run_llm_decision(self, step_index: int, prompt: str) -> Dict[str, Any]:
        started_time = time.time()
        task = self.dynamic_run.append_task(
            self._llm_spec,
            inputs=self._build_llm_inputs(step_index, prompt),
            request_id=f"react-step-{step_index}-llm",
        )
        finish_event = self.dynamic_run.wait_for_task(task, timeout=self.task_timeout)
        finished_time = time.time()
        result = finish_event.get("data", {}).get("result", {})
        if not isinstance(result, dict):
            raise TypeError("ReAct LLM task must return a dict result")
        return {
            "task": task,
            "decision": result,
            "event": finish_event,
            "timings": {
                "llm_started_time": started_time,
                "llm_finished_time": finished_time,
                "llm_seconds": round(finished_time - started_time, 6),
            },
        }

    def _build_llm_inputs(self, step_index: int, prompt: str) -> Dict[str, Any]:
        available = {
            "prompt": prompt,
            "history": [self._step_snapshot(step) for step in self.steps],
            "tools": self.tool_specs,
            "skills": self._build_skill_context(),
            "step": step_index,
            "system_prompt": self.system_prompt,
        }
        return {
            key: value
            for key, value in available.items()
            if key in self._llm_input_names
        }

    def _build_skill_context(self) -> Dict[str, Any]:
        if not self.skills:
            return {}

        context = {
            "catalog": self.skill_catalog,
            "progressive_disclosure": self.progressive_skills,
        }
        if self.progressive_skills:
            context["instructions"] = (
                "Use the skill catalog to decide whether a skill is relevant. "
                "Do not assume details that are not in the catalog. When more "
                "skill instructions are needed, call read_skill_file with "
                "skill_name and file_name such as SKILL.md, reference.md, or examples.md."
            )
        else:
            context["instructions"] = "Full SKILL.md bodies are provided below."
            context["details"] = {
                skill.name: {
                    "description": skill.description,
                    "body": skill.body,
                    "files": skill.list_files(),
                }
                for skill in self.skills
            }
        return context

    def _parse_action(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        action = decision.get("action", decision)
        if not isinstance(action, dict):
            raise TypeError("ReAct decision action must be a dict")

        if "error" in action:
            return {
                "error": str(action.get("error") or "ReAct decision action is invalid"),
                "raw": action.get("raw"),
            }

        if "final" in action:
            return {"final": action["final"]}

        action_type = action.get("type") or action.get("action")
        if action_type == "final":
            return {
                "final": action.get("answer", action.get("result", action.get("content", ""))),
            }

        tool_name = action.get("tool")
        if isinstance(action_type, str) and action_type not in {"tool", "call_tool", "final"} and not tool_name:
            tool_name = action_type

        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("ReAct tool action requires a non-empty tool name")

        args = action.get("args") or {}
        if not isinstance(args, dict):
            raise TypeError("ReAct tool action args must be a dict")

        return {
            "tool": tool_name,
            "args": args,
        }

    def _validate_tool_action(self, action: Dict[str, Any]) -> Dict[str, Any] | None:
        tool_name = action["tool"]
        args = action.get("args") or {}

        if tool_name not in self._registered_tools:
            return {
                "error_type": "tool_not_allowed",
                "error": f"ReAct tool is not allowed: {tool_name}",
                "tool": tool_name,
                "available_tools": sorted(self._registered_tools),
            }

        input_names = self._tool_input_names.get(tool_name, set())
        required_inputs = self._tool_required_inputs.get(tool_name, set())
        missing_inputs = sorted(required_inputs - set(args))
        unknown_inputs = sorted(set(args) - input_names)

        if missing_inputs:
            return {
                "error_type": "missing_args",
                "error": f"Missing required args for {tool_name}: {missing_inputs}",
                "tool": tool_name,
                "args": args,
                "missing_args": missing_inputs,
                "expected_args": sorted(input_names),
                "required_args": sorted(required_inputs),
            }

        if unknown_inputs:
            return {
                "error_type": "unknown_args",
                "error": f"Unknown args for {tool_name}: {unknown_inputs}",
                "tool": tool_name,
                "args": args,
                "unknown_args": unknown_inputs,
                "expected_args": sorted(input_names),
                "required_args": sorted(required_inputs),
            }

        return None

    def _record_repair_step(
        self,
        step_index: int,
        decision_task: DynamicTaskInvocation,
        decision: Dict[str, Any],
        action: Dict[str, Any],
        error: str,
        error_type: str,
        details: Dict[str, Any] | None = None,
    ) -> ReActStep:
        observation = {
            "error": error,
            "error_type": error_type,
            "repairable": True,
            "action": action,
            "available_tools": sorted(self._registered_tools),
        }
        if details:
            observation.update(details)

        tool_name = action.get("tool") if isinstance(action.get("tool"), str) else None
        args = action.get("args") if isinstance(action.get("args"), dict) else {}

        self.dynamic_run.emit_event("agent_repair_observation", {
            "mode": "react",
            "step": step_index,
            "tool": tool_name,
            "decision_task_id": decision_task.task_id,
            "result": observation,
            "timings": details.get("timings") if details else None,
        })

        return ReActStep(
            index=step_index,
            decision_task_id=decision_task.task_id,
            decision=decision,
            tool_name=tool_name,
            args=dict(args),
            observation=observation,
            timings=(details.get("timings") if details else {}) or {},
        )

    def _run_tool_step(
        self,
        step_index: int,
        decision_task: DynamicTaskInvocation,
        decision: Dict[str, Any],
        action: Dict[str, Any],
    ) -> ReActStep:
        started_time = time.time()
        tool_name = action["tool"]
        if tool_name not in self._registered_tools:
            raise ValueError(f"ReAct tool is not allowed: {tool_name}")

        args = action.get("args") or {}
        self.dynamic_run.emit_event("agent_action", {
            "mode": "react",
            "step": step_index,
            "tool": tool_name,
            "args": args,
            "decision_task_id": decision_task.task_id,
        })
        tool_task = self.dynamic_run.append_task(
            self._registered_tools[tool_name],
            inputs=args,
            parents=[decision_task],
            request_id=f"react-step-{step_index}-tool",
        )
        finish_event = self.dynamic_run.wait_for_task(tool_task, timeout=self.task_timeout)
        finished_time = time.time()
        tool_seconds = finished_time - started_time
        observation = {
            "tool": tool_name,
            "task_id": tool_task.task_id,
            "result": finish_event.get("data", {}).get("result", {}),
        }
        self.dynamic_run.emit_event("agent_observation", {
            "mode": "react",
            "step": step_index,
            "tool": tool_name,
            "task_id": tool_task.task_id,
            "result": observation["result"],
            "timings": {
                "tool_seconds": round(tool_seconds, 6),
            },
        })

        return ReActStep(
            index=step_index,
            decision_task_id=decision_task.task_id,
            decision=decision,
            tool_name=tool_name,
            args=dict(args),
            tool_task_id=tool_task.task_id,
            observation=observation,
            timings={
                "tool_started_time": started_time,
                "tool_finished_time": finished_time,
                "tool_seconds": round(tool_seconds, 6),
            },
        )

    def _step_snapshot(self, step: ReActStep) -> Dict[str, Any]:
        return {
            "index": step.index,
            "decision_task_id": step.decision_task_id,
            "decision": step.decision,
            "tool": step.tool_name,
            "args": step.args,
            "tool_task_id": step.tool_task_id,
            "observation": step.observation,
            "timings": step.timings,
        }

    def _run_timing_summary(
        self,
        total_seconds: float,
        extra_timings: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        llm_seconds = 0.0
        tool_seconds = 0.0
        for step in self.steps:
            timings = step.timings or {}
            llm_seconds += float(timings.get("llm_seconds") or 0)
            tool_seconds += float(timings.get("tool_seconds") or 0)

        if extra_timings:
            llm_seconds += float(extra_timings.get("llm_seconds") or 0)
            tool_seconds += float(extra_timings.get("tool_seconds") or 0)

        return {
            "total_seconds": round(total_seconds, 6),
            "llm_seconds": round(llm_seconds, 6),
            "tool_seconds": round(tool_seconds, 6),
            "controller_seconds": round(max(0.0, total_seconds - llm_seconds - tool_seconds), 6),
        }

    def _artifact_summary(self) -> Dict[str, Any]:
        try:
            snapshot = self.dynamic_run.get_status()
        except Exception:
            return {"count": 0, "files": []}

        files = []
        for task_id, task_node in (snapshot.get("task_nodes") or {}).items():
            manifest = task_node.get("file_manifest") or {}
            for file_info in manifest.get("files") or []:
                record = dict(file_info)
                record.setdefault("task_id", task_id)
                record.setdefault("producer_task_id", record.get("task_id"))
                files.append(record)

        files.sort(key=lambda item: (str(item.get("task_id") or ""), str(item.get("path") or "")))
        return {
            "count": len(files),
            "files": files,
        }

    def _cancel_if_active(self, reason: str):
        try:
            status = self.dynamic_run.get_status().get("status")
            if status not in {"finalized", "failed", "canceled", "timed_out", "interrupted"}:
                self.dynamic_run.cancel(reason)
        except Exception:
            pass

    def _emit_best_effort(self, event_type: str, data: Dict[str, Any]):
        try:
            self.dynamic_run.emit_event(event_type, data)
        except Exception:
            pass
