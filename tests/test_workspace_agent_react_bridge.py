import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from maze.client.maze.agent_mcp import (
    close_mcp_manager_blocking,
    discover_mcp_tools_blocking,
    normalize_mcp_tool_result,
)
from maze.client.maze.agent_tools import AgentToolRegistry, AgentToolRuntime
from maze.client.maze.react_llm import _build_react_messages
from web.maze_playground.backend import maze_bridge


WORKSPACE_TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "inspect_workflow_run",
            "description": "Inspect one workflow run.",
            "parameters": {
                "type": "object",
                "properties": {
                    "runId": {"type": "string"},
                    "detail": {"type": "string", "enum": ["summary", "full"]},
                },
                "required": ["runId"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_workflow_draft",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_workflow_draft",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class _Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _workspace_client(token="capability-secret"):
    return maze_bridge._WorkspaceAgentMCPClient(
        url="http://127.0.0.1:3001/api/internal/workspace-agent/tool",
        token=token,
        tools=WORKSPACE_TOOL_SCHEMA,
        timeout=12,
    )


def test_workspace_agent_mcp_client_preserves_schema_and_keeps_capability_out_of_tool_data(monkeypatch):
    token = "capability-secret"
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return _Response(200, {
            "success": True,
            "result": {
                "ok": True,
                "status": "failed",
                token: {"echo": f"prefix-{token}"},
            },
        })

    monkeypatch.setattr(requests, "post", post)
    manager, tools = discover_mcp_tools_blocking(clients=[_workspace_client(token)])
    try:
        assert [tool.agent_tool_name for tool in tools] == ["inspect_workflow_run"]
        result = tools[0](runId="run-1", detail="full")
        normalized = normalize_mcp_tool_result(result)

        assert normalized == {
            "structured_content": {
                "ok": True,
                "status": "failed",
                "<redacted>": {"echo": "prefix-<redacted>"},
            },
            "content": [],
            "is_error": False,
        }
        assert token not in json.dumps(normalized)
        assert calls == [(
            "http://127.0.0.1:3001/api/internal/workspace-agent/tool",
            {
                "headers": {
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                "json": {
                    "name": "inspect_workflow_run",
                    "input": {"runId": "run-1", "detail": "full"},
                },
                "timeout": 12.0,
            },
        )]

        registry = AgentToolRegistry(SimpleNamespace())
        spec = registry.register_mcp_tool(tools[0])
        assert spec.source == "mcp"
        assert spec.task_spec is None
        assert spec.input_schema["properties"]["detail"]["enum"] == ["summary", "full"]
        assert token not in json.dumps(spec.to_llm_spec())

        messages = _build_react_messages(
            prompt="inspect it",
            history=[],
            tools={spec.name: spec.to_llm_spec()},
            step=1,
            system_prompt="Use the available tools.",
        )
        available = json.loads(messages[1]["content"])["available_tools"]
        assert available["inspect_workflow_run"]["input_schema"] == spec.input_schema
    finally:
        close_mcp_manager_blocking(manager)


def test_workspace_agent_mcp_client_turns_auth_and_timeout_into_repairable_results(monkeypatch):
    token = "capability-secret"
    events = []
    dynamic_run = SimpleNamespace(
        server_url="http://maze-core:8000",
        emit_event=lambda event_type, data: events.append({"type": event_type, "data": data}),
    )
    manager, tools = discover_mcp_tools_blocking(clients=[_workspace_client(token)])
    registry = AgentToolRegistry(dynamic_run)
    registry.register_mcp_tool(tools[0])
    runtime = AgentToolRuntime(dynamic_run, registry)
    try:
        monkeypatch.setattr(
            requests,
            "post",
            lambda *args, **kwargs: _Response(401, {"error": f"bad {token}"}),
        )
        unauthorized = runtime.execute_task_tool(
            step=1,
            tool_name="inspect_workflow_run",
            args={"runId": "run-1"},
            mode="react",
        )
        assert unauthorized.result["repairable"] is True
        assert unauthorized.result["error_type"] == "mcp_tool_error"
        assert unauthorized.result["structured_content"]["error_type"] == "authorization_error"
        assert unauthorized.tool_result.error["repairable"] is True
        assert token not in json.dumps(events)

        def timeout(*args, **kwargs):
            raise requests.Timeout(f"timed out with {token}")

        monkeypatch.setattr(requests, "post", timeout)
        timed_out = runtime.execute_task_tool(
            step=2,
            tool_name="inspect_workflow_run",
            args={"runId": "run-1"},
            mode="react",
        )
        assert timed_out.result["structured_content"] == {
            "ok": False,
            "error": "Workspace Agent tool request timed out",
            "error_type": "timeout",
            "repairable": True,
        }
        assert timed_out.tool_result.error["repairable"] is True
        assert token not in json.dumps(events)
    finally:
        close_mcp_manager_blocking(manager)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/internal/tool",
        "http://user:password@127.0.0.1:3001/internal/tool",
        "file:///tmp/tool",
    ],
)
def test_workspace_agent_mcp_client_rejects_non_loopback_or_userinfo_urls(url):
    with pytest.raises(ValueError, match="loopback"):
        maze_bridge._WorkspaceAgentMCPClient(
            url=url,
            token="secret",
            tools=WORKSPACE_TOOL_SCHEMA,
            timeout=10,
        )


def test_run_react_workflow_workspace_agent_uses_core_url_and_emits_safe_turn_event(
    tmp_path,
    monkeypatch,
):
    order = []
    clients = []
    llm_factory = {}

    class FakeDynamicRun:
        def __init__(self):
            self.events = []

        def emit_event(self, event_type, data):
            event = {"type": event_type, "data": data}
            self.events.append(event)
            order.append(("event", event_type, data))
            return event

    class FakeReact:
        run_id = "dynamic-run-1"

        def __init__(self):
            self.dynamic_run = FakeDynamicRun()
            self.prompt = None

        def run(self, prompt):
            self.prompt = prompt
            return "done"

        def status(self):
            return {"status": "finalized"}

        def get_events(self):
            return list(self.dynamic_run.events)

    class FakeClient:
        def __init__(self, server_url):
            self.server_url = server_url
            self.create_kwargs = None
            self.react = FakeReact()
            clients.append(self)

        def create_react_workflow(self, **kwargs):
            self.create_kwargs = kwargs
            return self.react

    def fake_llm_task(**kwargs):
        llm_factory.update(kwargs)
        return object()

    monkeypatch.setattr(maze_bridge, "DynamicMaClient", FakeClient)
    monkeypatch.setattr(maze_bridge, "create_openai_react_llm_task", fake_llm_task)
    monkeypatch.setattr(maze_bridge, "_maze_head_node_id", lambda _core_url: "head-node")
    monkeypatch.setattr(
        maze_bridge,
        "emit_progress",
        lambda event: order.append(("progress", event["type"], event["data"])),
    )
    monkeypatch.setenv("MAZE_CORE_URL", "http://maze-core.internal:9123/")
    monkeypatch.setenv("MAZE_REACT_API_KEY", "llm-api-secret")
    monkeypatch.setenv("MAZE_WORKSPACE_AGENT_TOOL_TOKEN", "capability-secret")

    result = maze_bridge.run_react_workflow({
        "mode": "workspace-agent",
        "prompt": "truncated conversation context",
        "workspaceAgentMessage": "inspect the failed run",
        "workspaceDir": str(tmp_path),
        "baseUrl": "https://llm.example/v1",
        "model": "test-model",
        "systemPrompt": "Use only the supplied Workspace Agent tools.",
        "workspaceAgentTools": WORKSPACE_TOOL_SCHEMA,
        "workspaceAgentToolUrl": "http://127.0.0.1:3001/api/internal/workspace-agent/tool",
        "permissionPolicy": {
            "mcp": {"inspect_workflow_run": "allow"},
            "skill": {"*": "allow"},
        },
    })

    assert result["success"] is True
    assert clients[0].server_url == "http://maze-core.internal:9123"
    assert clients[0].create_kwargs["tools"] == []
    assert clients[0].create_kwargs["system_prompt"] == "Use only the supplied Workspace Agent tools."
    assert clients[0].create_kwargs["permission_policy"] == {
        "mcp": {"inspect_workflow_run": "allow"},
        "skill": {"*": "allow"},
    }
    assert list(clients[0].create_kwargs["mcp_clients"][0]._tools) == ["inspect_workflow_run"]
    assert clients[0].react.prompt == "truncated conversation context"
    assert clients[0].react.dynamic_run.events == [{
        "type": "workspace_agent_turn_started",
        "data": {"message": "inspect the failed run"},
    }]

    turn_index = next(index for index, item in enumerate(order) if item[1] == "workspace_agent_turn_started")
    created_index = next(index for index, item in enumerate(order) if item[1] == "react_run_created")
    assert turn_index < created_index
    assert "capability-secret" not in json.dumps(order)
    assert "llm-api-secret" not in json.dumps(order)
    assert llm_factory["system_prompt"] == "Use only the supplied Workspace Agent tools."
    assert llm_factory["resources"]["target_node_id"] == "head-node"
    assert not Path(llm_factory["config_path"]).exists()


def test_workspace_agent_rejects_legacy_tool_shapes_and_cli_capability(tmp_path, monkeypatch):
    with pytest.raises(TypeError, match="OpenAI function-tool list"):
        maze_bridge._normalize_workspace_agent_tools({"tools": WORKSPACE_TOOL_SCHEMA})

    monkeypatch.delenv("MAZE_WORKSPACE_AGENT_TOOL_TOKEN", raising=False)
    result = maze_bridge.run_react_workflow({
        "mode": "workspace-agent",
        "prompt": "inspect it",
        "workspaceDir": str(tmp_path),
        "workspaceAgentTools": WORKSPACE_TOOL_SCHEMA,
        "workspaceAgentToolUrl": "http://127.0.0.1:3001/api/internal/workspace-agent/tool",
        "workspaceAgentToolToken": "must-not-be-read-from-argv",
    })

    assert result["success"] is False
    assert result["error"] == "MAZE_WORKSPACE_AGENT_TOOL_TOKEN is required"
