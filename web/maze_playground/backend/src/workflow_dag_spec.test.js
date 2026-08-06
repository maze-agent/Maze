import assert from 'node:assert/strict';
import test from 'node:test';

import { compileWorkflowToDagSpec } from './workflow_dag_spec.js';

const node = (id, data) => ({
  id,
  type: 'taskNode',
  position: { x: 0, y: 0 },
  data: {
    nodeType: 'task',
    label: id,
    inputs: [],
    outputs: [{ name: 'result', dataType: 'str' }],
    configured: true,
    ...data,
  },
});

test('preserves Playground task provenance in submitted DAG nodes', () => {
  const spec = compileWorkflowToDagSpec({
    id: 'workflow',
    name: 'Workflow',
    edges: [],
    nodes: [
      node('workspace_task', {
        category: 'workspace',
        sourceKey: 'tasks/work.py::run_work',
      }),
      node('builtin_task', {
        category: 'builtin',
        taskRef: 'distributedSmoke.distributed_gpu_probe',
      }),
    ],
  }, {}, {
    'tasks/work.py::run_work': { codeStr: 'def run_work(): pass' },
    'tasks/distributed_gpu_probe.py': { codeStr: 'def distributed_gpu_probe(): pass' },
  });

  assert.deepEqual(spec.nodes[0].metadata, {
    playground_category: 'workspace',
    playground_task_path: 'tasks/work.py',
    playground_function_name: 'run_work',
  });
  assert.deepEqual(spec.nodes[1].metadata, {
    playground_category: 'builtin',
    playground_task_ref: 'distributedSmoke.distributed_gpu_probe',
    playground_task_path: 'tasks/distributed_gpu_probe.py',
    playground_function_name: 'distributed_gpu_probe',
  });
});
