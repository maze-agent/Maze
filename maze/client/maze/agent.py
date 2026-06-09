from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

from maze.client.maze.dynamic import DynamicRun, DynamicTaskInvocation, DynamicTaskSpec
from maze.client.maze.agent_tools import AgentToolRegistry, AgentToolRuntime
from maze.client.maze.agent_permissions import AgentPermissionPolicy


AgentPlanner = Callable[["AgentContext"], Dict[str, Any]]


@dataclass
class AgentStep:
    """One planner decision and its Maze task observation."""

    index: int
    action: Dict[str, Any]
    tool_name: str | None = None
    args: Dict[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    observation: Dict[str, Any] | None = None
    started_time: float | None = None
    finished_time: float | None = None
    duration_seconds: float | None = None


@dataclass
class AgentContext:
    """Planner input for a running agent loop."""

    prompt: str
    tool_specs: Dict[str, Dict[str, Any]]
    skills: List[Dict[str, Any]] = field(default_factory=list)
    available_skills: List[Dict[str, Any]] = field(default_factory=list)
    steps: List[AgentStep] = field(default_factory=list)

    @property
    def observations(self) -> List[Dict[str, Any]]:
        return [
            step.observation
            for step in self.steps
            if step.observation is not None
        ]


class AgentRun:
    """Minimal Agent controller backed by a Maze DynamicRun.

    The planner returns either:
    - {"tool": "<tool-name>", "args": {...}}
    - {"final": ...}
    """

    def __init__(
        self,
        dynamic_run: DynamicRun,
        tools: List[Callable[..., Dict[str, Any]]],
        planner: AgentPlanner,
        max_steps: int = 10,
        task_timeout: float | None = None,
        mcp_manager: Any | None = None,
        mcp_tools: List[Any] | None = None,
        skill_registry: Any | None = None,
        permission_policy: AgentPermissionPolicy | Dict[str, Any] | None = None,
    ):
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")

        self.dynamic_run = dynamic_run
        self.planner = planner
        self.max_steps = max_steps
        self.task_timeout = task_timeout
        self.mcp_manager = mcp_manager
        self.skill_registry = skill_registry
        self._initial_skill_events_emitted = False
        self.steps: List[AgentStep] = []
        self.tool_registry = AgentToolRegistry(dynamic_run)
        self.tool_runtime = AgentToolRuntime(
            dynamic_run=dynamic_run,
            registry=self.tool_registry,
            task_timeout=task_timeout,
            permission_policy=permission_policy,
        )

        for tool in tools:
            self._register_tool(tool)
        for mcp_tool in (mcp_tools or []):
            self.tool_registry.register_mcp_tool(mcp_tool)
        if self.skill_registry is not None:
            self.tool_registry.register_skill_loader(self.skill_registry)

        if not self.tool_registry.specs:
            raise ValueError("AgentRun requires at least one agent tool")

        self.tool_specs: Dict[str, Dict[str, Any]] = self.tool_registry.tool_specs_for_llm()
        self._registered_tools: Dict[str, DynamicTaskSpec] = self.tool_registry.task_specs
        self._available_tools = self.tool_registry.available_tool_names()

    @property
    def run_id(self) -> str:
        return self.dynamic_run.run_id

    def run(self, prompt: str) -> Any:
        run_started = time.time()
        context = AgentContext(
            prompt=prompt,
            tool_specs=self.tool_specs,
            skills=self._loaded_skills_for_llm(),
            available_skills=self._available_skills_summary(),
            steps=self.steps,
        )

        try:
            self._emit_loaded_skill_events_once(mode="agent")
            self.dynamic_run.emit_event("agent_run_started", {
                "prompt": prompt,
                "max_steps": self.max_steps,
                "task_timeout": self.task_timeout,
                "tools": self._available_tools,
                "skills": self._loaded_skills_for_llm(),
                "available_skills": self._available_skills_summary(),
                "tool_harness": "agent_tool_runtime_v1",
            })
            for step_index in range(1, self.max_steps + 1):
                context.skills = self._loaded_skills_for_llm()
                context.available_skills = self._available_skills_summary()
                action = self._next_action(context)
                if "final" in action:
                    final_result = action["final"]
                    total_seconds = time.time() - run_started
                    self.dynamic_run.emit_event("agent_final", {
                        "mode": "agent",
                        "answer": final_result,
                        "step_count": len(self.steps),
                        "stop_reason": "final",
                        "max_steps": self.max_steps,
                        "task_timeout": self.task_timeout,
                        "skills": self._loaded_skills_for_llm(),
                        "timings": {
                            "total_seconds": round(total_seconds, 6),
                            "task_seconds": round(sum(
                                step.duration_seconds or 0
                                for step in self.steps
                            ), 6),
                        },
                    })
                    self.dynamic_run.finalize({
                        "mode": "agent",
                        "answer": final_result,
                        "stop_reason": "final",
                        "max_steps": self.max_steps,
                        "task_timeout": self.task_timeout,
                        "step_count": len(self.steps),
                        "skills": self._loaded_skills_for_llm(),
                        "timings": {
                            "total_seconds": round(total_seconds, 6),
                            "task_seconds": round(sum(
                                step.duration_seconds or 0
                                for step in self.steps
                            ), 6),
                        },
                        "steps": [self._step_snapshot(step) for step in self.steps],
                    })
                    self._close_mcp()
                    return final_result

                step = self._run_tool_step(step_index, action)
                self.steps.append(step)

            raise RuntimeError(f"Agent exceeded max_steps={self.max_steps} without a final answer")
        except Exception as exc:
            stop_reason = "max_steps" if "max_steps" in str(exc) else "controller_error"
            self._emit_best_effort("agent_error", {
                "error": str(exc),
                "stop_reason": stop_reason,
                "elapsed_seconds": round(time.time() - run_started, 6),
            })
            self._cancel_if_active("Agent controller error")
            self._close_mcp()
            raise

    def status(self) -> Dict[str, Any]:
        return self.dynamic_run.get_status()

    def get_events(self, after: int | None = None) -> List[Dict[str, Any]]:
        return self.dynamic_run.get_events(after=after)

    def cancel(self, reason: str | None = None):
        try:
            return self.dynamic_run.cancel(reason)
        finally:
            self._close_mcp()

    def _register_tool(self, tool: Callable[..., Dict[str, Any]]):
        self.tool_registry.register_task_tool(tool)

    def _next_action(self, context: AgentContext) -> Dict[str, Any]:
        action = self.planner(context)
        if not isinstance(action, dict):
            raise TypeError("Agent planner must return a dict action")
        if "final" in action:
            return action
        if "tool" not in action:
            raise ValueError("Agent planner action must include 'tool' or 'final'")
        return action

    def _run_tool_step(self, step_index: int, action: Dict[str, Any]) -> AgentStep:
        started_time = time.time()
        tool_name = action.get("tool")
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("Agent tool action requires a non-empty string 'tool'")

        args = action.get("args") or {}
        if not isinstance(args, dict):
            raise TypeError("Agent tool action 'args' must be a dict")
        validation_error = self.tool_runtime.validate_tool_call(tool_name, args)
        if validation_error is not None:
            observation = self.tool_runtime.repair_observation(
                validation_error,
                tool_name=tool_name,
                args=args,
                action=action,
            )
            self.tool_runtime.emit_repair(
                mode="agent",
                step=step_index,
                tool_name=tool_name,
                args=args,
                observation=observation,
            )
            if validation_error.error_type == "tool_not_allowed":
                raise ValueError(f"Agent tool is not allowed: {tool_name}")
            raise ValueError(validation_error.message)

        self.dynamic_run.emit_event("agent_action", {
            "step": step_index,
            "tool": tool_name,
            "args": args,
        })
        call_result = self.tool_runtime.execute_task_tool(
            step=step_index,
            tool_name=tool_name,
            args=args,
            mode="agent",
            request_id=f"agent-step-{step_index}",
        )
        task = call_result.task
        observation = call_result.observation
        finished_time = time.time()
        duration_seconds = finished_time - started_time
        self.dynamic_run.emit_event("agent_observation", {
            "step": step_index,
            "tool": tool_name,
            "task_id": task.task_id,
            "result": observation["result"],
            "tool_result": observation["tool_result"],
            "artifacts": observation["artifacts"],
            "timings": {
                "duration_seconds": round(duration_seconds, 6),
            },
        })

        return AgentStep(
            index=step_index,
            action=dict(action),
            tool_name=tool_name,
            args=dict(args),
            task_id=task.task_id,
            observation=observation,
            started_time=started_time,
            finished_time=finished_time,
            duration_seconds=duration_seconds,
        )

    def _observation_from_finish_event(
        self,
        tool_name: str,
        task: DynamicTaskInvocation,
        finish_event: Dict[str, Any],
    ) -> Dict[str, Any]:
        data = finish_event.get("data", {})
        return {
            "tool": tool_name,
            "task_id": task.task_id,
            "result": data.get("result", {}),
            "event": finish_event,
        }

    def _step_snapshot(self, step: AgentStep) -> Dict[str, Any]:
        return {
            "index": step.index,
            "tool": step.tool_name,
            "args": step.args,
            "task_id": step.task_id,
            "observation": step.observation,
            "timings": {
                "started_time": step.started_time,
                "finished_time": step.finished_time,
                "duration_seconds": round(step.duration_seconds, 6)
                if step.duration_seconds is not None
                else None,
            },
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

    def _loaded_skills_for_llm(self) -> List[Dict[str, Any]]:
        if self.skill_registry is None:
            return []
        return self.skill_registry.loaded_for_llm()

    def _available_skills_summary(self) -> List[Dict[str, Any]]:
        if self.skill_registry is None:
            return []
        return self.skill_registry.list_skills()

    def _emit_loaded_skill_events_once(self, mode: str):
        if self._initial_skill_events_emitted or self.skill_registry is None:
            return
        self._initial_skill_events_emitted = True
        for skill in self.skill_registry.loaded_events():
            self._emit_best_effort("agent_skill_loaded", {
                "mode": mode,
                "skill": skill,
                "source": "initial",
            })

    def _close_mcp(self):
        if self.mcp_manager is None:
            return
        try:
            from maze.client.maze.agent_mcp import close_mcp_manager_blocking

            close_mcp_manager_blocking(self.mcp_manager)
        except Exception:
            pass
        self.mcp_manager = None
