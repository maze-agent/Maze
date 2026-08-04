import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def _node_binary():
    node = shutil.which("node")
    if node is None:
        sibling = Path(sys.executable).with_name("node")
        node = str(sibling) if sibling.is_file() else None
    return node


def _run_node(node, repo_root, workspace_root, workspace_dir, script, **extra_env):
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=repo_root,
        env={
            **os.environ,
            "MAZE_PLAYGROUND_NO_LISTEN": "1",
            "MAZE_WORKSPACES_DIR": str(workspace_root),
            "TEST_WORKSPACE_DIR": str(workspace_dir),
            **extra_env,
        },
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_workspace_agent_session_v2_survives_restart_and_keeps_v1_export(tmp_path):
    node = _node_binary()
    if node is None:
        pytest.skip("Node.js is required for the Playground ownership test")

    repo_root = Path(__file__).resolve().parents[1]
    workspace_root = tmp_path / "workspaces"
    workspace_dir = workspace_root / "agent-test"
    legacy_path = workspace_dir / "agent_sessions" / "legacy.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_payload = {
        "schema": "maze_workspace_agent_session",
        "schema_version": 1,
        "id": "legacy",
        "title": "Legacy",
        "workspaceId": "agent-test",
        "workspaceDir": str(workspace_dir),
        "createdAt": "2026-08-01T00:00:00Z",
        "updatedAt": "2026-08-01T00:00:00Z",
        "summary": "legacy compressed context",
        "messages": [{
            "id": "old-message",
            "sessionId": "legacy",
            "role": "user",
            "createdAt": "2026-08-01T00:00:00Z",
            "parts": [{"type": "text", "text": "old question"}],
        }],
    }
    legacy_path.write_text(json.dumps(legacy_payload), encoding="utf-8")
    legacy_before = legacy_path.read_bytes()

    first = _run_node(
        node,
        repo_root,
        workspace_root,
        workspace_dir,
        r"""
          const hooks = (await import('./web/maze_playground/backend/src/server.js')).__workspaceAgentTestHooks;
          const context = {workspaceId: 'agent-test', workspaceDir: process.env.TEST_WORKSPACE_DIR, workspaceManifestVersion: 1};
          const session = await hooks.createAgentSessionRecord(context, {id: 'session-test', title: 'Two turns'});
          await hooks.appendAgentSessionTurn(context.workspaceDir, session, 'dynamic-run-one');
          console.log(JSON.stringify({sessionId: session.id}));
        """,
    )
    assert first == {"sessionId": "session-test"}

    second = _run_node(
        node,
        repo_root,
        workspace_root,
        workspace_dir,
        r"""
          const hooks = (await import('./web/maze_playground/backend/src/server.js')).__workspaceAgentTestHooks;
          const context = {workspaceId: 'agent-test', workspaceDir: process.env.TEST_WORKSPACE_DIR, workspaceManifestVersion: 1};
          const session = await hooks.loadAgentSession(context.workspaceDir, 'session-test');
          await hooks.appendAgentSessionTurn(context.workspaceDir, session, 'dynamic-run-two');
          const legacyExport = await hooks.buildAgentSessionExport(context, 'legacy');
          const events = [
            {type: 'workspace_agent_turn_started', seq: 1, timestamp: '2026-08-02T00:00:00Z', data: {message: 'inspect it'}},
            {type: 'agent_action', seq: 2, timestamp: '2026-08-02T00:00:01Z', data: {step: 1, tool: 'inspect_workflow_run', args: {runId: 'run-1'}}},
            {type: 'agent_observation', seq: 3, timestamp: '2026-08-02T00:00:02Z', data: {step: 1, tool: 'inspect_workflow_run', result: {structured_content: {draft: {id: 'draft-1'}}}}},
            {type: 'agent_final', seq: 4, timestamp: '2026-08-02T00:00:03Z', data: {answer: 'done'}},
          ];
          const turn = session.turns.at(-1);
          const messages = hooks.agentMessagesFromDynamicTurn(session.id, turn, {status: 'finalized'}, events);
          const prompt = hooks.buildWorkspaceAgentPrompt('next', Array.from({length: 20}, (_, index) => ({
            role: 'assistant', parts: [{type: 'text', text: String(index).repeat(1000)}],
          })), legacyExport.summary);
          console.log(JSON.stringify({
            turns: session.turns,
            legacyMessages: legacyExport.messages,
            toolNames: hooks.agentToolDefinitions().map((tool) => tool.function.name),
            messageRoles: messages.map((message) => message.role),
            draftIds: hooks.collectAgentDraftIdsFromEvents(events),
            promptLength: prompt.length,
            promptHasLegacySummary: prompt.includes('legacy compressed context'),
          }));
        """,
    )

    session_payload = json.loads(
        (workspace_dir / "agent_sessions" / "session-test.json").read_text(encoding="utf-8")
    )
    assert session_payload["schema_version"] == 2
    assert [turn["dynamic_run_id"] for turn in session_payload["turns"]] == [
        "dynamic-run-one",
        "dynamic-run-two",
    ]
    assert "messages" not in session_payload
    assert "summary" not in session_payload
    assert "compaction" not in session_payload
    assert not (workspace_dir / "agent_runs").exists()
    assert legacy_path.read_bytes() == legacy_before
    assert second["legacyMessages"] == legacy_payload["messages"]
    assert "save_workflow_draft" not in second["toolNames"]
    assert "run_workflow_draft" not in second["toolNames"]
    assert second["messageRoles"] == ["user", "assistant", "tool", "assistant"]
    assert second["draftIds"] == ["draft-1"]
    assert second["promptLength"] <= 12000
    assert second["promptHasLegacySummary"] is True


def test_workspace_agent_session_concurrent_turns_are_merged_and_capability_is_revoked(tmp_path):
    node = _node_binary()
    if node is None:
        pytest.skip("Node.js is required for the Playground ownership test")

    repo_root = Path(__file__).resolve().parents[1]
    workspace_root = tmp_path / "workspaces"
    workspace_dir = workspace_root / "agent-test"
    result = _run_node(
        node,
        repo_root,
        workspace_root,
        workspace_dir,
        r"""
          const hooks = (await import('./web/maze_playground/backend/src/server.js')).__workspaceAgentTestHooks;
          const context = {workspaceId: 'agent-test', workspaceDir: process.env.TEST_WORKSPACE_DIR, workspaceManifestVersion: 1};
          const created = await hooks.createAgentSessionRecord(context, {id: 'session-concurrent'});
          const [left, right] = await Promise.all([
            hooks.loadAgentSession(context.workspaceDir, created.id),
            hooks.loadAgentSession(context.workspaceDir, created.id),
          ]);
          await Promise.all([
            hooks.appendAgentSessionTurn(context.workspaceDir, left, 'run-left'),
            hooks.appendAgentSessionTurn(context.workspaceDir, right, 'run-right'),
          ]);
          const saved = await hooks.loadAgentSession(context.workspaceDir, created.id);

          const token = hooks.createWorkspaceAgentCapability(context, {sessionId: created.id});
          const request = {
            socket: {remoteAddress: '127.0.0.1'},
            get: () => `Bearer ${token}`,
          };
          hooks.bindWorkspaceAgentCapability(token, 'run-left');
          const beforeRevoke = Boolean(hooks.workspaceAgentCapability(request));
          hooks.revokeWorkspaceAgentCapabilities('run-left');
          const afterRevoke = Boolean(hooks.workspaceAgentCapability(request));
          console.log(JSON.stringify({
            runIds: saved.turns.map((turn) => turn.dynamic_run_id),
            beforeRevoke,
            afterRevoke,
          }));
        """,
    )

    assert result == {
        "runIds": ["run-left", "run-right"],
        "beforeRevoke": True,
        "afterRevoke": False,
    }
