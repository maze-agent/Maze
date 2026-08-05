import assert from 'node:assert/strict';
import test from 'node:test';
import type { WorkflowNode } from '../src/types/workflow.ts';
import {
  bindWorkflowConnection,
  clearWorkflowSource,
  syncWorkflowInputEdges,
  unbindWorkflowEdges,
} from '../src/utils/workflowBindings.ts';

function node(
  id: string,
  inputs: WorkflowNode['data']['inputs'],
  outputs: WorkflowNode['data']['outputs'],
): WorkflowNode {
  return {
    id,
    type: 'taskNode',
    position: { x: 0, y: 0 },
    data: {
      category: 'custom',
      nodeType: 'task',
      label: id,
      inputs,
      outputs,
      configured: true,
    },
  };
}

test('workflow connections bind and clear task inputs', () => {
  const nodes = [
    node('source', [], [{ name: 'result', dataType: 'str' }]),
    node('target', [
      { name: 'constant', dataType: 'str', source: 'user', value: 'keep' },
      { name: 'result', dataType: 'str', source: 'user', value: '' },
    ], []),
  ];

  const bound = bindWorkflowConnection(nodes, {
    source: 'source',
    target: 'target',
    sourceHandle: 'result',
    targetHandle: 'result',
  });
  assert.equal(bound.error, undefined);
  assert.deepEqual(bound.nodes[1].data.inputs[1].taskSource, {
    taskId: 'source',
    outputKey: 'result',
  });
  assert.equal(bound.nodes[1].data.inputs[0].value, 'keep');

  const unbound = unbindWorkflowEdges(bound.nodes, [{
    source: 'source',
    target: 'target',
    sourceHandle: 'result',
    targetHandle: 'result',
  }]);
  assert.equal(unbound[1].data.inputs[1].source, 'user');
  assert.equal(unbound[1].data.inputs[1].taskSource, undefined);

  const rebound = bindWorkflowConnection(nodes, { source: 'source', target: 'target' });
  assert.equal(rebound.nodes[1].data.inputs[1].taskSource?.taskId, 'source');
  assert.equal(rebound.nodes[1].data.inputs[0].value, 'keep');
  assert.equal(clearWorkflowSource(rebound.nodes, 'source')[1].data.inputs[1].source, 'user');
});

test('input bindings add and remove their canvas dependency edge', () => {
  const nodes = [
    node('source', [], [{ name: 'result', dataType: 'str' }]),
    node('other', [], [{ name: 'result', dataType: 'str' }]),
    node('target', [
      {
        name: 'first',
        dataType: 'str',
        source: 'task',
        taskSource: { taskId: 'source', outputKey: 'result' },
      },
      {
        name: 'second',
        dataType: 'str',
        source: 'task',
        taskSource: { taskId: 'source', outputKey: 'result' },
      },
    ], []),
  ];
  const edges = [{ id: 'existing', source: 'source', target: 'target' }];

  const oneBindingLeft = syncWorkflowInputEdges(nodes, edges, 'target', [
    { name: 'first', dataType: 'str', source: 'user', value: '' },
    nodes[2].data.inputs[1],
  ]);
  assert.deepEqual(oneBindingLeft, edges);

  const changedSource = syncWorkflowInputEdges(nodes, edges, 'target', [
    { name: 'first', dataType: 'str', source: 'user', value: '' },
    {
      name: 'second',
      dataType: 'str',
      source: 'task',
      taskSource: { taskId: 'other', outputKey: 'result' },
    },
  ]);
  assert.deepEqual(changedSource, [
    { id: 'edge-other-target', source: 'other', target: 'target' },
  ]);
});
