"""Legacy ReAct application host.

This module is not part of the Maze Core Runtime public boundary. It remains
temporarily for compatibility and should move to an extension or be removed in
a later purification phase.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

from maze.client.maze.decorator import get_task_metadata
from maze.client.maze.dynamic import DynamicRun, DynamicTaskInvocation, DynamicTaskSpec
from maze.client.maze.agent_tools import AgentToolRegistry, AgentToolRuntime
from maze.client.maze.agent_permissions import AgentPermissionPolicy


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
        task_timeout: float | None = None,
        permission_policy: AgentPermissionPolicy | Dict[str, Any] | None = None,
    ):
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")

        self.dynamic_run = dynamic_run
        self.llm_task = llm_task
        self.max_steps = max_steps
        self.system_prompt = system_prompt
        self.task_timeout = task_timeout
        self.steps: List[ReActStep] = []
        self.tool_registry = AgentToolRegistry(dynamic_run)
        self.tool_runtime = AgentToolRuntime(
            dynamic_run=dynamic_run,
            registry=self.tool_registry,
            task_timeout=task_timeout,
            permission_policy=permission_policy,
        )

        llm_metadata = get_task_metadata(llm_task)
        self._llm_input_names = set(llm_metadata.inputs)
        self._llm_input_types = dict(llm_metadata.data_types or {})
        self._llm_spec = self.dynamic_run.register_task_spec(
            llm_task,
            task_spec_id="react_llm_decision",
            task_name=llm_metadata.func_name,
        )

        for tool in tools:
            self._register_tool(tool)

        if not self.tool_registry.specs:
            raise ValueError("ReActWorkflow requires at least one agent tool")

        self.tool_specs: Dict[str, Dict[str, Any]] = self.tool_registry.tool_specs_for_llm()
        self._registered_tools: Dict[str, DynamicTaskSpec] = self.tool_registry.task_specs
        self._available_tools = self.tool_registry.available_tool_names()

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
                "task_timeout": self.task_timeout,
                "llm_task": self._llm_spec.task_name,
                "tools": self._available_tools,
                "tool_harness": "agent_tool_runtime_v1",
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
                        "max_steps": self.max_steps,
                        "task_timeout": self.task_timeout,
                        "timings": timings,
                        "artifacts": artifacts,
                    })
                    self.dynamic_run.finalize({
                        "mode": "react",
                        "answer": answer,
                        "stop_reason": "final",
                        "max_steps": self.max_steps,
                        "task_timeout": self.task_timeout,
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
        self.tool_registry.register_task_tool(tool)

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
            "step": step_index,
            "system_prompt": self.system_prompt,
        }
        return {
            key: value
            for key, value in available.items()
            if key in self._llm_input_names
        }

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
        error = self.tool_runtime.validate_tool_call(tool_name, args)
        if error is None:
            return None
        return self.tool_runtime.repair_observation(
            error,
            tool_name=tool_name,
            args=args,
            action=action,
        )

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
            "available_tools": self._available_tools,
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
        self.tool_runtime.emit_repair(
            mode="react",
            step=step_index,
            tool_name=tool_name,
            args=args,
            observation=observation,
            decision_task_id=decision_task.task_id,
        )

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
        if tool_name not in self.tool_registry.specs:
            raise ValueError(f"ReAct tool is not allowed: {tool_name}")

        args = action.get("args") or {}
        self.dynamic_run.emit_event("agent_action", {
            "mode": "react",
            "step": step_index,
            "tool": tool_name,
            "args": args,
            "decision_task_id": decision_task.task_id,
        })
        call_result = self.tool_runtime.execute_task_tool(
            step=step_index,
            tool_name=tool_name,
            args=args,
            mode="react",
            parents=[decision_task],
            request_id=f"react-step-{step_index}-tool",
            decision_task_id=decision_task.task_id,
        )
        finished_time = time.time()
        tool_seconds = finished_time - started_time
        tool_task = call_result.task
        observation = call_result.observation
        self.dynamic_run.emit_event("agent_observation", {
            "mode": "react",
            "step": step_index,
            "tool": tool_name,
            "task_id": tool_task.task_id,
            "result": observation["result"],
            "tool_result": observation["tool_result"],
            "artifacts": observation["artifacts"],
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
            timings=call_result.timings,
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
