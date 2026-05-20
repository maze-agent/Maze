from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

from maze.client.maze.decorator import get_task_metadata
from maze.client.maze.dynamic import DynamicRun, DynamicTaskInvocation, DynamicTaskSpec


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


@dataclass
class AgentContext:
    """Planner input for a running agent loop."""

    prompt: str
    tool_specs: Dict[str, Dict[str, Any]]
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
    ):
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")

        self.dynamic_run = dynamic_run
        self.planner = planner
        self.max_steps = max_steps
        self.task_timeout = task_timeout
        self.steps: List[AgentStep] = []
        self.tool_specs: Dict[str, Dict[str, Any]] = {}
        self._registered_tools: Dict[str, DynamicTaskSpec] = {}

        for tool in tools:
            self._register_tool(tool)

        if not self._registered_tools:
            raise ValueError("AgentRun requires at least one @task tool")

    @property
    def run_id(self) -> str:
        return self.dynamic_run.run_id

    def run(self, prompt: str) -> Any:
        context = AgentContext(
            prompt=prompt,
            tool_specs=self.tool_specs,
            steps=self.steps,
        )

        try:
            self.dynamic_run.emit_event("agent_run_started", {
                "prompt": prompt,
                "max_steps": self.max_steps,
                "tools": sorted(self._registered_tools),
            })
            for step_index in range(1, self.max_steps + 1):
                action = self._next_action(context)
                if "final" in action:
                    final_result = action["final"]
                    self.dynamic_run.emit_event("agent_final", {
                        "answer": final_result,
                        "step_count": len(self.steps),
                    })
                    self.dynamic_run.finalize({
                        "answer": final_result,
                        "steps": [self._step_snapshot(step) for step in self.steps],
                    })
                    return final_result

                step = self._run_tool_step(step_index, action)
                self.steps.append(step)

            raise RuntimeError(f"Agent exceeded max_steps={self.max_steps} without a final answer")
        except Exception as exc:
            self._emit_best_effort("agent_error", {"error": str(exc)})
            self._cancel_if_active("Agent controller error")
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
            raise ValueError(f"Duplicate agent tool name: {tool_name}")

        task_spec = self.dynamic_run.register_task_spec(
            tool,
            task_spec_id=tool_name,
            task_name=tool_name,
        )
        self._registered_tools[tool_name] = task_spec
        self.tool_specs[tool_name] = {
            "name": tool_name,
            "description": (getattr(tool, "__doc__", None) or "").strip(),
            "inputs": metadata.inputs,
            "outputs": metadata.outputs,
            "data_types": metadata.data_types,
            "resources": metadata.resources,
        }

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
        tool_name = action.get("tool")
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("Agent tool action requires a non-empty string 'tool'")
        if tool_name not in self._registered_tools:
            raise ValueError(f"Agent tool is not allowed: {tool_name}")

        args = action.get("args") or {}
        if not isinstance(args, dict):
            raise TypeError("Agent tool action 'args' must be a dict")

        self.dynamic_run.emit_event("agent_action", {
            "step": step_index,
            "tool": tool_name,
            "args": args,
        })
        task = self.dynamic_run.append_task(
            self._registered_tools[tool_name],
            inputs=args,
            request_id=f"agent-step-{step_index}",
        )
        finish_event = self.dynamic_run.wait_for_task(task, timeout=self.task_timeout)
        observation = self._observation_from_finish_event(tool_name, task, finish_event)
        self.dynamic_run.emit_event("agent_observation", {
            "step": step_index,
            "tool": tool_name,
            "task_id": task.task_id,
            "result": observation["result"],
        })

        return AgentStep(
            index=step_index,
            action=dict(action),
            tool_name=tool_name,
            args=dict(args),
            task_id=task.task_id,
            observation=observation,
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
