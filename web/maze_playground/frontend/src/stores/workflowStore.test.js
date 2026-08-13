import assert from 'node:assert/strict';
import test from 'node:test';
import { useWorkflowStore } from './workflowStore.ts';

test('activating a workspace replaces workspace-owned workflow and run state', () => {
  useWorkflowStore.setState({
    workspaceId: 'workspace-a',
    workspaceDir: '/workspaces/a',
    workspaceTasks: [{ relativePath: 'tasks/a.py' }],
    workspaceWorkflows: [{ relativePath: 'workflows/a.json' }],
    currentWorkspaceWorkflowPath: 'workflows/a.json',
    activeRunId: 'run-a',
    selectedRunId: 'run-a',
    staticRuns: [{ kind: 'static', run_id: 'run-a', status: 'running' }],
  });

  useWorkflowStore.getState().activateWorkspace({
    success: true,
    workspaceId: 'workspace-b',
    workspaceDir: '/workspaces/b',
    workspaceManifestVersion: 3,
    manifest: {
      workspace_id: 'workspace-b',
      name: 'Workspace B',
    },
  }, {
    name: 'Draft B',
    nodes: [{ id: 'node-b' }],
    edges: [{ id: 'edge-b' }],
  });

  const state = useWorkflowStore.getState();
  assert.equal(state.workspaceId, 'workspace-b');
  assert.equal(state.workflowName, 'Draft B');
  assert.deepEqual(state.nodes, [{ id: 'node-b' }]);
  assert.deepEqual(state.workspaceTasks, []);
  assert.deepEqual(state.workspaceWorkflows, []);
  assert.deepEqual(state.staticRuns, []);
  assert.equal(state.currentWorkspaceWorkflowPath, null);
  assert.equal(state.activeRunId, null);
  assert.equal(state.selectedRunId, null);
});
