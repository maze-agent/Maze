import assert from 'node:assert/strict';
import test from 'node:test';
import {
  latestRunForWorkflow,
  mergeStaticRunSnapshots,
  runMatchesWorkflow,
  runWorkflowGraph,
} from './runSnapshot.ts';

function run(overrides = {}) {
  return {
    kind: 'static',
    run_id: 'run-b',
    status: 'succeeded',
    created_time: 20,
    metadata: {
      workspace_id: 'workspace-b',
      workflow_path: 'workflows/shared.json',
      playground_workflow_id: 'workflow-b',
      dag_spec: {
        name: 'Workflow B snapshot',
        nodes: [{
          id: 'shared-node',
          task_name: 'Task from B',
          inputs: {
            prompt: {
              input_schema: 'from_user',
              value: 'B input',
              data_type: 'str',
            },
          },
          outputs: [{ name: 'result', data_type: 'str' }],
          resources: { cpu_num: 2, gpu_mem: 0, io_num: 0 },
          task_kind: 'cpu',
          code_str: 'def task_b():\n    return {"result": "B"}\n',
        }],
        edges: [],
        run: { workspace_dir: '/workspaces/workspace-b' },
      },
    },
    task_nodes: {
      'shared-node': {
        task_id: 'shared-node',
        task_name: 'Task from B',
        status: 'succeeded',
        selected_node: { node_id: 'worker-b', node_ip: '10.0.0.2', gpu_id: null },
      },
    },
    ...overrides,
  };
}

test('workflow matching requires the same workspace and workflow', () => {
  const snapshot = run();
  assert.equal(runMatchesWorkflow(snapshot, {
    workspaceId: 'workspace-a',
    workspaceDir: '/workspaces/workspace-a',
    workflowPath: 'workflows/shared.json',
    workflowId: 'workflow-a',
  }), false);
  assert.equal(runMatchesWorkflow(snapshot, {
    workspaceId: 'workspace-b',
    workspaceDir: '/workspaces/workspace-b',
    workflowPath: 'workflows/shared.json',
    workflowId: 'new-local-id',
  }), true);
});

test('historical graph definitions and runtime come from the run snapshot', () => {
  const graph = runWorkflowGraph(run());
  assert.equal(graph.nodes.length, 1);
  assert.equal(graph.nodes[0].data.label, 'Task from B');
  assert.equal(graph.nodes[0].data.customCode?.includes('task_b'), true);
  assert.equal(graph.nodes[0].data.runState.selected_node.node_ip, '10.0.0.2');
  assert.deepEqual(graph.nodes[0].data.inputs, [{
    name: 'prompt',
    dataType: 'str',
    source: 'user',
    value: 'B input',
    taskSource: undefined,
  }]);
});

test('latest design telemetry ignores runs from other workspaces', () => {
  const olderCurrentRun = run({
    run_id: 'run-a',
    created_time: 10,
    metadata: {
      ...run().metadata,
      workspace_id: 'workspace-a',
      workflow_path: 'workflows/current.json',
      playground_workflow_id: 'workflow-a',
      dag_spec: {
        ...run().metadata.dag_spec,
        run: { workspace_dir: '/workspaces/workspace-a' },
      },
    },
  });
  const newestOtherRun = run({ run_id: 'run-b', created_time: 30 });
  const selected = latestRunForWorkflow([newestOtherRun, olderCurrentRun], {
    workspaceId: 'workspace-a',
    workspaceDir: '/workspaces/workspace-a',
    workflowPath: 'workflows/current.json',
    workflowId: 'workflow-a',
  });
  assert.equal(selected?.run_id, 'run-a');
});

test('latest design telemetry follows submission order, not later status updates', () => {
  const olderRunUpdatedLast = run({
    run_id: 'older-run',
    created_time: 10,
    submitted_time: 11,
    updated_time: 1000,
  });
  const newerRun = run({
    run_id: 'newer-run',
    created_time: 20,
    submitted_time: 21,
    updated_time: 22,
  });
  const identity = {
    workspaceId: 'workspace-b',
    workspaceDir: '/workspaces/workspace-b',
    workflowPath: 'workflows/shared.json',
    workflowId: 'workflow-b',
  };

  assert.equal(
    latestRunForWorkflow([olderRunUpdatedLast, newerRun], identity)?.run_id,
    'newer-run',
  );
});

test('static run hydration preserves cached detail while refreshing summary fields', () => {
  const cached = run({
    run_id: 'active-run',
    status: 'running',
    summary: false,
  });
  const { task_nodes: _taskNodes, graph: _graph, ...summary } = cached;
  const retained = run({ run_id: 'selected-run' });
  const merged = mergeStaticRunSnapshots([cached, retained], [{
    ...summary,
    summary: true,
    status: 'succeeded',
    updated_time: 30,
  }]);

  assert.deepEqual(merged.map((item) => item.run_id), ['active-run', 'selected-run']);
  assert.equal(merged[0].status, 'succeeded');
  assert.equal(merged[0].summary, false);
  assert.deepEqual(merged[0].task_nodes, cached.task_nodes);
});
