import type {
  UnifiedRunSnapshot,
  UnifiedRunTaskSnapshot,
  WorkflowEdge,
  WorkflowNode,
} from '@/types/workflow';

type WorkflowIdentity = {
  workflowId?: string | null;
  workflowPath?: string | null;
  workspaceId?: string | null;
  workspaceDir?: string | null;
};

export function runSubmissionOrderTime(run: UnifiedRunSnapshot) {
  return Number(run.submitted_time ?? run.created_time ?? 0);
}

export function mergeStaticRunSnapshots(
  cachedRuns: UnifiedRunSnapshot[],
  incomingRuns: UnifiedRunSnapshot[],
) {
  const cachedById = new Map(cachedRuns.map((run) => [run.run_id, run]));
  const incomingIds = new Set(incomingRuns.map((run) => run.run_id));
  const mergedIncoming = incomingRuns.map((incoming) => {
    const cached = cachedById.get(incoming.run_id);
    if (!cached || incoming.summary !== true) return incoming;

    return {
      ...cached,
      ...incoming,
      task_nodes: incoming.task_nodes ?? cached.task_nodes,
      graph: incoming.graph ?? cached.graph,
      summary: cached.summary === true,
    };
  });

  return [
    ...mergedIncoming,
    ...cachedRuns.filter((run) => !incomingIds.has(run.run_id)),
  ];
}

type DagTaskSpec = {
  id?: unknown;
  task_name?: unknown;
  inputs?: Record<string, any>;
  outputs?: any[];
  resources?: Record<string, any>;
  task_kind?: unknown;
  code_str?: unknown;
  timeout_seconds?: unknown;
  max_retries?: unknown;
  retry_backoff_seconds?: unknown;
  model_anchor?: Record<string, any> | null;
  metadata?: Record<string, any>;
};

type RuntimeNodeData = WorkflowNode['data'] & {
  runState?: UnifiedRunTaskSnapshot | null;
  runStatus?: string | null;
};

function normalizedPath(value?: string | null) {
  return String(value || '').replace(/\\/g, '/').replace(/\/+$/, '');
}

function runWorkspaceId(run: UnifiedRunSnapshot) {
  return String(run.metadata?.workspace_id || run.workspace_id || '');
}

function runWorkspaceDir(run: UnifiedRunSnapshot) {
  return normalizedPath(
    run.workspace_dir
    || run.metadata?.workspace_dir
    || run.metadata?.dag_spec?.run?.workspace_dir,
  );
}

export function runWorkflowName(run?: UnifiedRunSnapshot | null) {
  return String(
    run?.metadata?.workflow_name
    || run?.metadata?.dag_spec?.name
    || run?.workflow_name
    || 'Workflow Run',
  );
}

export function runMatchesWorkflow(run: UnifiedRunSnapshot, identity: WorkflowIdentity) {
  const currentWorkspaceId = String(identity.workspaceId || '');
  const currentWorkspaceDir = normalizedPath(identity.workspaceDir);
  const snapshotWorkspaceId = runWorkspaceId(run);
  const snapshotWorkspaceDir = runWorkspaceDir(run);

  if (currentWorkspaceId && snapshotWorkspaceId && currentWorkspaceId !== snapshotWorkspaceId) {
    return false;
  }
  if (currentWorkspaceDir && snapshotWorkspaceDir && currentWorkspaceDir !== snapshotWorkspaceDir) {
    return false;
  }
  if (!(currentWorkspaceId && snapshotWorkspaceId) && !(currentWorkspaceDir && snapshotWorkspaceDir)) {
    return false;
  }

  const currentPath = normalizedPath(identity.workflowPath);
  const snapshotPath = normalizedPath(run.metadata?.workflow_path);
  if (currentPath && snapshotPath) {
    return currentPath === snapshotPath;
  }

  const currentWorkflowId = String(identity.workflowId || '');
  const snapshotWorkflowId = String(run.metadata?.playground_workflow_id || '');
  return Boolean(currentWorkflowId && snapshotWorkflowId && currentWorkflowId === snapshotWorkflowId);
}

export function latestRunForWorkflow(runs: UnifiedRunSnapshot[], identity: WorkflowIdentity) {
  return runs
    .filter((run) => run.kind === 'static' && runMatchesWorkflow(run, identity))
    .sort((left, right) => (
      runSubmissionOrderTime(right) - runSubmissionOrderTime(left)
    ))[0] || null;
}

function taskInput(name: string, input: any) {
  const fromTask = input?.input_schema === 'from_task';
  const reference = fromTask
    ? String(input?.value || '').match(/^([A-Za-z_][A-Za-z0-9_-]*)\.output\.([A-Za-z_][A-Za-z0-9_-]*)$/)
    : null;
  return {
    name,
    dataType: String(input?.data_type || 'any'),
    source: fromTask ? 'task' as const : 'user' as const,
    value: fromTask || input?.value === undefined || input?.value === null
      ? undefined
      : String(input.value),
    taskSource: reference
      ? { taskId: reference[1], outputKey: reference[2] }
      : undefined,
  };
}

function taskSpecNode(
  spec: DagTaskSpec,
  runtime: UnifiedRunTaskSnapshot | undefined,
  index: number,
  positions: ReadonlyMap<string, WorkflowNode['position']>,
): WorkflowNode | null {
  const id = typeof spec.id === 'string' ? spec.id : runtime?.task_id;
  if (!id) return null;
  const metadata = spec.metadata || {};
  const category = ['builtin', 'custom', 'workspace'].includes(metadata.playground_category)
    ? metadata.playground_category as WorkflowNode['data']['category']
    : 'custom';
  const resources = spec.resources || runtime?.resources || {};
  const taskKind = spec.task_kind || runtime?.task_kind;
  const data: RuntimeNodeData = {
    category,
    nodeType: 'task',
    label: String(spec.task_name || runtime?.task_name || id),
    taskRef: metadata.playground_task_ref,
    taskPath: metadata.playground_task_path,
    functionName: metadata.playground_function_name,
    customCode: typeof spec.code_str === 'string' ? spec.code_str : undefined,
    inputs: Object.entries(spec.inputs || {}).map(([name, input]) => taskInput(name, input)),
    outputs: (spec.outputs || runtime?.outputs || []).map((output: any) => ({
      name: String(output?.name || 'output'),
      dataType: String(output?.data_type || output?.dataType || 'any'),
    })),
    resources: {
      cpu_num: Number((resources as any).cpu_num ?? (resources as any).cpu ?? 1),
      gpu_mem: Number((resources as any).gpu_mem || 0),
      io_num: Number((resources as any).io_num || 0),
    },
    task_kind: ['cpu', 'gpu', 'io'].includes(String(taskKind))
      ? taskKind as WorkflowNode['data']['task_kind']
      : undefined,
    taskTimeout: typeof spec.timeout_seconds === 'number' ? spec.timeout_seconds : undefined,
    maxRetries: typeof spec.max_retries === 'number' ? spec.max_retries : undefined,
    retryBackoffSeconds: typeof spec.retry_backoff_seconds === 'number'
      ? spec.retry_backoff_seconds
      : undefined,
    modelAnchor: spec.model_anchor?.local_model
      ? spec.model_anchor as WorkflowNode['data']['modelAnchor']
      : undefined,
    localModel: spec.model_anchor?.local_model,
    configured: true,
    runState: runtime || null,
    runStatus: runtime?.status || null,
  };
  return {
    id,
    type: 'taskNode',
    position: positions.get(id) || {
      x: 160 + (index % 3) * 280,
      y: 120 + Math.floor(index / 3) * 180,
    },
    data,
  };
}

export function runWorkflowGraph(
  run: UnifiedRunSnapshot,
  positionNodes: WorkflowNode[] = [],
): { nodes: WorkflowNode[]; edges: WorkflowEdge[] } {
  const dagSpec = run.metadata?.dag_spec;
  const taskNodes = run.task_nodes || {};
  const positions = new Map(positionNodes.map((node) => [node.id, node.position]));
  const specs: DagTaskSpec[] = Array.isArray(dagSpec?.nodes)
    ? dagSpec.nodes
    : (run.graph?.nodes || Object.keys(taskNodes)).map((id) => ({
      id,
      task_name: taskNodes[id]?.task_name,
      inputs: Object.fromEntries((taskNodes[id]?.inputs || []).map((input: any) => [input.name, input])),
      outputs: taskNodes[id]?.outputs || [],
      resources: taskNodes[id]?.resources,
      task_kind: taskNodes[id]?.task_kind,
    }));
  const nodes = specs.flatMap((spec, index) => {
    const id = typeof spec.id === 'string' ? spec.id : '';
    const node = taskSpecNode(spec, taskNodes[id], index, positions);
    return node ? [node] : [];
  });
  const dagEdges = Array.isArray(dagSpec?.edges) ? dagSpec.edges : null;
  const edges = (dagEdges || run.graph?.edges || []).flatMap((edge: any, index: number) => {
    const source = edge.source_task_id || edge.source;
    const target = edge.target_task_id || edge.target;
    if (!source || !target) return [];
    return [{
      id: `run-edge-${source}-${target}-${index}`,
      source: String(source),
      target: String(target),
      sourceHandle: edge.source_output || undefined,
      targetHandle: edge.target_input || undefined,
    }];
  });
  return { nodes, edges };
}
