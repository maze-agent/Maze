import type { WorkflowEdge, WorkflowNode } from '../types/workflow';

export type WorkflowConnection = {
  source?: string | null;
  target?: string | null;
  sourceHandle?: string | null;
  targetHandle?: string | null;
};

export function bindWorkflowConnection(
  nodes: WorkflowNode[],
  connection: WorkflowConnection,
): { nodes: WorkflowNode[]; error?: string } {
  const source = nodes.find((node) => node.id === connection.source);
  const target = nodes.find((node) => node.id === connection.target);
  if (!source || !target || source.id === target.id) {
    return { nodes, error: 'Select two different task nodes' };
  }

  const existingInput = target.data.inputs.find((input) => (
    input.source === 'task' && input.taskSource?.taskId === source.id
  ));
  const availableInputs = target.data.inputs.filter((item) => item.source !== 'task');
  const input = connection.targetHandle
    ? target.data.inputs.find((item) => item.name === connection.targetHandle)
    : existingInput
      || availableInputs.find((item) => item.value === undefined || item.value === '')
      || availableInputs[0];
  if (!input) {
    return { nodes, error: `${target.data.label} has no unbound input` };
  }

  const existingOutputKey = existingInput?.taskSource?.outputKey;
  const output = connection.sourceHandle
    ? source.data.outputs.find((item) => item.name === connection.sourceHandle)
    : source.data.outputs.find((item) => item.name === existingOutputKey)
      || source.data.outputs.find((item) => item.name === input.name)
      || source.data.outputs[0];
  if (!output) {
    return { nodes, error: `${source.data.label} has no output` };
  }

  return {
    nodes: nodes.map((node) => node.id === target.id ? {
      ...node,
      data: {
        ...node.data,
        inputs: node.data.inputs.map((item) => item.name === input.name ? {
          ...item,
          source: 'task',
          taskSource: { taskId: source.id, outputKey: output.name },
        } : item),
      },
    } : node),
  };
}

export function syncWorkflowInputEdges(
  nodes: WorkflowNode[],
  edges: WorkflowEdge[],
  targetId: string,
  nextInputs: WorkflowNode['data']['inputs'],
): WorkflowEdge[] {
  const target = nodes.find((node) => node.id === targetId);
  if (!target) return edges;

  const previousSources = new Set(target.data.inputs.flatMap((input) => (
    input.source === 'task' && input.taskSource?.taskId ? [input.taskSource.taskId] : []
  )));
  const nextSources = new Set(nextInputs.flatMap((input) => (
    input.source === 'task' && input.taskSource?.taskId ? [input.taskSource.taskId] : []
  )));
  const nextEdges = edges.filter((edge) => !(
    edge.target === targetId
    && previousSources.has(edge.source)
    && !nextSources.has(edge.source)
  ));

  nextSources.forEach((sourceId) => {
    const sourceExists = nodes.some((node) => node.id === sourceId);
    const edgeExists = nextEdges.some((edge) => edge.source === sourceId && edge.target === targetId);
    if (sourceExists && sourceId !== targetId && !edgeExists) {
      nextEdges.push({
        id: `edge-${sourceId}-${targetId}`,
        source: sourceId,
        target: targetId,
      });
    }
  });

  return nextEdges;
}

export function clearWorkflowSource(nodes: WorkflowNode[], sourceId: string): WorkflowNode[] {
  return nodes.map((node) => {
    const inputs = node.data.inputs.map((input) => (
      input.source === 'task' && input.taskSource?.taskId === sourceId
        ? { ...input, source: 'user' as const, taskSource: undefined }
        : input
    ));
    return inputs.some((input, index) => input !== node.data.inputs[index])
      ? { ...node, data: { ...node.data, inputs } }
      : node;
  });
}

export function unbindWorkflowEdges(
  nodes: WorkflowNode[],
  deletedEdges: Array<{
    source: string;
    target: string;
    sourceHandle?: string | null;
    targetHandle?: string | null;
  }>,
): WorkflowNode[] {
  return nodes.map((node) => {
    const incoming = deletedEdges.filter((edge) => edge.target === node.id);
    if (incoming.length === 0) return node;
    const inputs = node.data.inputs.map((input) => {
      const shouldClear = incoming.some((edge) => (
        input.source === 'task'
        && input.taskSource?.taskId === edge.source
        && (!edge.targetHandle || edge.targetHandle === input.name)
        && (!edge.sourceHandle || edge.sourceHandle === input.taskSource.outputKey)
      ));
      return shouldClear
        ? { ...input, source: 'user' as const, taskSource: undefined }
        : input;
    });
    return inputs.some((input, index) => input !== node.data.inputs[index])
      ? { ...node, data: { ...node.data, inputs } }
      : node;
  });
}
