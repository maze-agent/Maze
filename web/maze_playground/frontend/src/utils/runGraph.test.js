import assert from 'node:assert/strict';
import test from 'node:test';
import { acceptedTaskResult, awaitingMoreSteps, projectRunGraph, taskReport } from './runGraph.ts';

function run(tasks, edges = []) {
  return { run_id: 'dynamic', status: 'running', task_nodes: Object.fromEntries(tasks.map((task) => [task.task_id, task])), graph: { nodes: tasks.map((task) => task.task_id), edges } };
}

test('append adds dependencies without moving existing nodes', () => {
  const a = { task_id: 'a', task_name: 'Baseline', status: 'completed', created_time: 1 };
  const first = projectRunGraph(run([a]));
  const positions = new Map(first.nodes.map((node) => [node.id, node.position]));
  const second = projectRunGraph(run([a, { task_id: 'b', status: 'running', created_time: 2 }], [{ source: 'a', target: 'b' }]), positions);
  assert.deepEqual(second.nodes[0].position, first.nodes[0].position);
  assert.ok(second.nodes[1].position.x > second.nodes[0].position.x);
  assert.equal(second.edges[0].animated, true);
});

test('executed negative verdict remains separate from task execution status', () => {
  const task = { task_id: 'a', status: 'completed', outputs: [{ name: 'report', value: { verdict: 'not_passed' } }] };
  const graph = projectRunGraph(run([task]));
  assert.equal(graph.nodes[0].data.task.status, 'completed');
  assert.equal(graph.nodes[0].data.report.verdict, 'not_passed');
  assert.equal(awaitingMoreSteps(run([task])), true);
  assert.equal(awaitingMoreSteps({ ...run([task]), status: 'finalized' }), false);
});

test('result can be restored from accepted finish events after refresh', () => {
  const task = { task_id: 'a', status: 'completed', outputs: [] };
  const events = [{ type: 'finish_task', data: { task_id: 'a', result: { report: { verdict: 'passed' } } } }];
  assert.equal(taskReport(acceptedTaskResult(task, events)).verdict, 'passed');
});

test('edges with missing endpoints do not break a partially loaded graph', () => {
  const graph = projectRunGraph(run([{ task_id: 'a' }], [{ source: 'a', target: 'not-loaded' }]));
  assert.equal(graph.edges.length, 0);
});

test('a static run finishing its tasks does not ask for dynamic steps', () => {
  const snapshot = run([{ task_id: 'a', status: 'completed' }]);
  assert.equal(awaitingMoreSteps({ ...snapshot, kind: 'static' }), false);
  assert.equal(awaitingMoreSteps({ ...snapshot, kind: 'dynamic' }), true);
});
