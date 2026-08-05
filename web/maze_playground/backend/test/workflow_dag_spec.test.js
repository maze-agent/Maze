import assert from 'node:assert/strict';
import test from 'node:test';

import {
  BUILTIN_TASK_ALIASES,
  compileWorkflowToDagSpec,
} from '../src/workflow_dag_spec.js';

const taskCode = (name) => `from maze import task\n@task\ndef ${name}(): return {"out": 1}\n`;

function node(id, data = {}) {
  return {
    id,
    type: 'taskNode',
    position: { x: 0, y: 0 },
    data: {
      category: 'custom',
      nodeType: 'task',
      label: id,
      customCode: taskCode(id.replaceAll('-', '_')),
      inputs: [],
      outputs: [{ name: 'out', dataType: 'int' }],
      configured: true,
      ...data,
    },
  };
}

const workflow = (nodes, edges = []) => ({ id: 'wf', name: 'Test workflow', nodes, edges });

function compile(value, context = {}, definitions = {}) {
  const resolved = new Map(
    value.nodes
      .filter((item) => item.data.category === 'custom')
      .map((item) => [`custom:${item.id}`, { codeSer: `parsed:${item.id}` }]),
  );
  for (const [key, definition] of definitions instanceof Map
    ? definitions
    : Object.entries(definitions)) resolved.set(key, definition);
  return compileWorkflowToDagSpec(value, context, resolved);
}

test('compiles current ReactFlow nodes to Core v1 without changing node ids or values', () => {
  const source = node('load_text', {
    category: 'workspace',
    customCode: undefined,
    taskPath: 'tasks/load_text.py',
    functionName: 'load_text',
    inputs: [
      { name: 'count', dataType: 'int', source: 'user', value: 0 },
      { name: 'enabled', dataType: 'bool', source: 'user', value: false },
      { name: 'prefix', dataType: 'str', source: 'user', value: '' },
    ],
    outputs: [{ name: 'text', dataType: 'str' }],
    resources: { cpu_num: 2, gpu_mem: 0, io_num: 1 },
  });
  const target = node('gpu_probe', {
    category: 'builtin',
    customCode: undefined,
    taskRef: 'distributedSmoke.distributed_gpu_probe',
    inputs: [{
      name: 'payload', dataType: 'str', source: 'task',
      taskSource: { taskId: 'load_text', outputKey: 'text' },
    }],
    outputs: [{ name: 'placement', dataType: 'dict' }],
    resources: { cpu_num: 1, gpu_mem: 512, io_num: 0 },
    localModel: 'Qwen2.5-3B-Instruct',
    maxRetries: 2,
    retryBackoffSeconds: 1.5,
    retryOn: ['RuntimeError'],
    taskTimeout: 90,
  });
  const definitions = new Map([
    ['tasks/load_text.py::load_text', { codeStr: taskCode('load_text'), taskKind: 'cpu' }],
    ['tasks/distributed_gpu_probe.py', { codeStr: taskCode('distributed_gpu_probe') }],
  ]);
  const spec = compileWorkflowToDagSpec(
    workflow([source, target], [{
      id: 'edge', source: 'load_text', target: 'gpu_probe',
      sourceHandle: 'text', targetHandle: 'payload',
    }]),
    {
      workspaceDir: '/workspace/demo', workspaceId: 'demo', workspaceManifestVersion: 7,
      artifactMode: false, fileContext: { enabled: true }, timeoutSeconds: 300, tags: ['smoke'],
    },
    definitions,
  );

  assert.equal(spec.schema, 'maze.workflow/v1');
  assert.equal(spec.nodes[0].id, 'load_text');
  assert.deepEqual(spec.nodes[0].inputs, {
    count: { key: 'count', input_schema: 'from_user', value: 0, data_type: 'int', has_value: true },
    enabled: { key: 'enabled', input_schema: 'from_user', value: false, data_type: 'bool', has_value: true },
    prefix: { key: 'prefix', input_schema: 'from_user', value: '', data_type: 'str', has_value: true },
  });
  assert.deepEqual(spec.nodes[1].inputs.payload, {
    key: 'payload', input_schema: 'from_task', value: 'load_text.output.text',
    data_type: 'str', has_value: true,
  });
  assert.deepEqual(spec.edges, [{
    source_task_id: 'load_text', source_output: 'text',
    target_task_id: 'gpu_probe', target_input: 'payload',
  }]);
  assert.equal(spec.nodes[1].task_kind, 'gpu');
  assert.deepEqual(spec.nodes[1].model_anchor, {
    local_model: 'Qwen2.5-3B-Instruct', model_scope: 'head', backend: 'transformers',
  });
  assert.deepEqual(spec.run, {
    workspace_dir: '/workspace/demo', artifact_mode: false, file_context: { enabled: true },
    timeout_seconds: 300, tags: ['smoke'],
    metadata: { workspace_id: 'demo', workspace_manifest_version: 7 },
  });
});

test('resolves builtin aliases to their canonical task files', () => {
  const aliases = {
    'distributedSmoke.distributed_gpu_probe': ['tasks/distributed_gpu_probe.py', 'distributed_gpu_probe'],
  };
  assert.deepEqual(BUILTIN_TASK_ALIASES, Object.fromEntries(
    Object.entries(aliases).map(([taskRef, [path]]) => [taskRef, path]),
  ));
  for (const [taskRef, [path, functionName]] of Object.entries(aliases)) {
    const builtin = node('builtin', { category: 'builtin', customCode: undefined, taskRef });
    const spec = compileWorkflowToDagSpec(workflow([builtin]), {}, {
      [`${path}::${functionName}`]: { codeStr: taskCode(functionName) },
    });
    assert.equal(spec.nodes[0].code_str, taskCode(functionName));
  }
});

test('resolves workspace source keys and serialized parser output', () => {
  const workspaceNode = node('workspace', {
    category: 'workspace', customCode: undefined, sourceKey: 'tasks/work.py::run_work',
  });
  const spec = compileWorkflowToDagSpec(workflow([workspaceNode]), {}, new Map([
    ['tasks/work.py::run_work', { codeSer: 'serialized' }],
  ]));
  assert.equal(spec.nodes[0].code_ser, 'serialized');
});

test('rejects empty, invalid, and duplicate node ids', () => {
  assert.throws(() => compileWorkflowToDagSpec(workflow([])), /non-empty/);
  assert.throws(() => compileWorkflowToDagSpec(workflow([node('bad id')])), /invalid node id/);
  assert.throws(() => compileWorkflowToDagSpec(workflow([node('same'), node('same')])), /duplicate node id/);
});

test('requires custom nodes to use their parsed resolver definition', () => {
  const custom = node('custom', { customCode: 'raw code must not be submitted' });
  assert.throws(() => compileWorkflowToDagSpec(workflow([custom])), /resolved custom definition/);
  const spec = compileWorkflowToDagSpec(workflow([custom]), {}, {
    'custom:custom': { codeSer: 'parsed-code' },
  });
  assert.equal(spec.nodes[0].code_ser, 'parsed-code');
  assert.equal(Object.hasOwn(spec.nodes[0], 'code_str'), false);
});

test('rejects unconfigured, agent, and unknown node categories', () => {
  assert.throws(() => compileWorkflowToDagSpec(workflow([node('draft', { configured: false })])), /not configured/);
  assert.throws(() => compileWorkflowToDagSpec(workflow([node('agent', { category: 'agent' })])), /agent category/);
  assert.throws(() => compileWorkflowToDagSpec(workflow([node('future', { category: 'future' })])), /unknown category/);
});

test('rejects missing or duplicate ports', () => {
  assert.throws(() => compile(workflow([node('missing', { outputs: [] })])), /outputs must be/);
  assert.throws(() => compile(workflow([node('outputs', {
    outputs: [{ name: 'out' }, { name: 'out' }],
  })])), /duplicate output/);
  assert.throws(() => compile(workflow([node('inputs', {
    inputs: [{ name: 'value', source: 'user' }, { name: 'value', source: 'user' }],
  })])), /duplicate input/);
});

test('rejects unknown builtins and missing code definitions', () => {
  assert.throws(() => compileWorkflowToDagSpec(workflow([node('builtin', {
    category: 'builtin', customCode: undefined, taskRef: 'unknown.task',
    sourceKey: 'tasks/unknown.py::unknown',
  })])), /unknown builtin/);
  assert.throws(() => compileWorkflowToDagSpec(workflow([node('workspace', {
    category: 'workspace', customCode: undefined, taskPath: 'tasks/missing.py',
  })])), /no resolved definition/);
});

test('rejects task bindings to unknown nodes or outputs', () => {
  assert.throws(() => compile(workflow([node('target', {
    inputs: [{ name: 'value', source: 'task', taskSource: { taskId: 'missing', outputKey: 'out' } }],
  })])), /unknown node/);
  assert.throws(() => compile(workflow([node('source'), node('target', {
    inputs: [{ name: 'value', source: 'task', taskSource: { taskId: 'source', outputKey: 'missing' } }],
  })])), /unknown output/);
});

test('requires ReactFlow edges and taskSource bindings to agree', () => {
  const source = node('source');
  const target = node('target', {
    inputs: [{ name: 'value', source: 'task', taskSource: { taskId: 'source', outputKey: 'out' } }],
  });
  assert.throws(() => compile(workflow([source, target])), /missing a ReactFlow edge/);
  assert.throws(() => compile(workflow([source, target], [{
    id: 'wrong', source: 'source', target: 'target', sourceHandle: 'wrong', targetHandle: 'value',
  }])), /disagrees with task bindings/);
  assert.throws(() => compile(workflow([source, target], [{
    id: 'unknown', source: 'source', target: 'missing',
  }])), /unknown node/);
});
