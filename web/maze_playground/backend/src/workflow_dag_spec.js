const NODE_ID_RE = /^[A-Za-z_][A-Za-z0-9_-]{0,127}$/;

export const BUILTIN_TASK_ALIASES = Object.freeze({
  'distributedSmoke.distributed_gpu_probe': 'tasks/distributed_gpu_probe.py',
});

export function compileWorkflowToDagSpec(workflow, context = {}, resolvedDefinitions = {}) {
  if (!workflow || typeof workflow !== 'object') throw new Error('workflow must be an object');
  if (!Array.isArray(workflow.nodes) || workflow.nodes.length === 0) {
    throw new Error('workflow nodes must be a non-empty array');
  }
  if (!Array.isArray(workflow.edges)) throw new Error('workflow edges must be an array');

  const nodeIds = new Set();
  for (const node of workflow.nodes) {
    const id = node?.id;
    if (typeof id !== 'string' || !NODE_ID_RE.test(id)) {
      throw new Error(`invalid node id ${JSON.stringify(id)}`);
    }
    if (nodeIds.has(id)) throw new Error(`duplicate node id: ${id}`);
    nodeIds.add(id);
  }

  const nodeMap = new Map();
  for (const node of workflow.nodes) {
    const { id } = node;
    const data = node.data;
    if (!data || typeof data !== 'object') throw new Error(`node ${id} has invalid data`);
    if (data.configured !== true) throw new Error(`node ${id} is not configured`);
    if (data.category === 'agent') throw new Error(`node ${id} uses unsupported agent category`);
    if (!['builtin', 'custom', 'workspace'].includes(data.category)) {
      throw new Error(`node ${id} has unknown category: ${String(data.category)}`);
    }

    const outputs = compileOutputs(data.outputs, id);
    const definition = resolveDefinition(data, resolvedDefinitions, id);
    const codeStr = text(definition.codeStr) ?? text(definition.code);
    const codeSer = text(definition.codeSer);
    if (!codeStr && !codeSer) throw new Error(`node ${id} is missing a code definition`);

    const resources = normalizeResources(data.resources ?? definition.resources);
    const modelAnchor = data.modelAnchor
      ? { ...data.modelAnchor }
      : data.localModel
        ? { local_model: data.localModel, model_scope: 'head', backend: 'transformers' }
      : definition.modelAnchor ?? null;
    const taskKind = data.task_kind
      ?? definition.taskKind
      ?? (resources.gpu_mem > 0 || modelAnchor?.local_model ? 'gpu' : 'cpu');
    if (!['cpu', 'gpu', 'io'].includes(taskKind)) {
      throw new Error(`node ${id} has invalid task_kind: ${String(taskKind)}`);
    }

    const [sourcePath, sourceFunction] = text(data.sourceKey)?.split('::') ?? [];
    const taskPath = data.category === 'builtin'
      ? BUILTIN_TASK_ALIASES[data.taskRef]
      : text(data.taskPath) ?? sourcePath;
    const functionName = text(data.functionName)
      ?? sourceFunction
      ?? (data.category === 'builtin' ? text(data.taskRef)?.split('.').at(-1) : undefined);

    const compiled = {
      id,
      type: 'code',
      task_name: String(data.label || definition.displayName || definition.functionName || id),
      inputs: {},
      outputs,
      resources,
      task_kind: taskKind,
      model_anchor: modelAnchor,
      file_context: data.fileContext ?? definition.fileContext ?? null,
      max_retries: data.maxRetries ?? definition.maxRetries ?? null,
      retry_backoff_seconds: data.retryBackoffSeconds ?? definition.retryBackoffSeconds ?? 0,
      retry_on: data.retryOn ?? definition.retryOn ?? null,
      timeout_seconds: data.taskTimeout ?? definition.timeoutSeconds ?? null,
      metadata: {
        ...(definition.metadata ?? {}),
        ...(data.metadata ?? {}),
        playground_category: data.category,
        ...(text(data.taskRef) ? { playground_task_ref: text(data.taskRef) } : {}),
        ...(taskPath ? { playground_task_path: taskPath } : {}),
        ...(functionName ? { playground_function_name: functionName } : {}),
      },
    };
    if (codeStr) compiled.code_str = codeStr;
    if (codeSer) compiled.code_ser = codeSer;
    nodeMap.set(id, {
      data,
      compiled,
      outputNames: new Set(outputs.map((output) => output.name)),
    });
  }

  const edges = [];
  for (const [nodeId, entry] of nodeMap) {
    entry.compiled.inputs = compileInputs(entry.data.inputs, nodeId, nodeMap, edges);
  }
  validateReactFlowEdges(workflow.edges, nodeMap, edges);

  const tags = context.tags ?? workflow.tags ?? [];
  const metadata = { ...(context.metadata ?? {}) };
  if (context.workspaceId !== undefined) metadata.workspace_id = context.workspaceId;
  if (context.workspaceManifestVersion !== undefined) {
    metadata.workspace_manifest_version = context.workspaceManifestVersion;
  }
  const run = {
    workspace_dir: context.workspaceDir ?? null,
    artifact_mode: context.artifactMode ?? true,
    file_context: context.fileContext ?? null,
    timeout_seconds: context.timeoutSeconds ?? null,
    tags: tags.map(String),
    metadata,
  };

  return {
    schema: 'maze.workflow/v1',
    name: String(workflow.name || workflow.id || 'workflow'),
    description: workflow.description ?? null,
    nodes: [...nodeMap.values()].map(({ compiled }) => compiled),
    edges,
    run,
    tags: [...run.tags],
    metadata: { ...(workflow.metadata ?? {}) },
  };
}

function resolveDefinition(data, definitions, nodeId) {
  if (data.category === 'custom') {
    const definition = getDefinition(definitions, `custom:${nodeId}`);
    if (definition) return definition;
    throw new Error(`node ${nodeId} has no resolved custom definition`);
  }

  const sourceKey = text(data.sourceKey);
  const [sourcePath, sourceFunction] = sourceKey?.split('::') ?? [];
  if (sourceKey && sourceKey.split('::').length > 2) {
    throw new Error(`node ${nodeId} has invalid sourceKey`);
  }

  let taskPath = text(data.taskPath) ?? sourcePath;
  let functionName = text(data.functionName) ?? sourceFunction;
  if (data.category === 'builtin') {
    const aliasPath = BUILTIN_TASK_ALIASES[data.taskRef];
    if (!aliasPath) {
      throw new Error(`node ${nodeId} references unknown builtin: ${String(data.taskRef || '')}`);
    }
    if (taskPath && aliasPath !== taskPath) {
      throw new Error(`node ${nodeId} has conflicting builtin source paths`);
    }
    taskPath = aliasPath;
    functionName ??= text(data.taskRef)?.split('.').at(-1);
  }
  if (!isCanonicalTaskPath(taskPath)) {
    throw new Error(`node ${nodeId} needs a canonical tasks/*.py source`);
  }

  const keys = [sourceKey, functionName && `${taskPath}::${functionName}`, taskPath].filter(Boolean);
  for (const key of new Set(keys)) {
    const definition = getDefinition(definitions, key);
    if (definition) return definition;
  }
  throw new Error(`node ${nodeId} has no resolved definition for ${keys.join(', ')}`);
}

function getDefinition(definitions, key) {
  const value = definitions instanceof Map ? definitions.get(key) : definitions?.[key];
  return value && typeof value === 'object' ? value : undefined;
}

function compileOutputs(outputs, nodeId) {
  if (!Array.isArray(outputs) || outputs.length === 0) {
    throw new Error(`node ${nodeId} outputs must be a non-empty array`);
  }
  const names = new Set();
  return outputs.map((output) => {
    const name = text(output?.name);
    if (!name) throw new Error(`node ${nodeId} has an output without a name`);
    if (names.has(name)) throw new Error(`node ${nodeId} has duplicate output: ${name}`);
    names.add(name);
    return { name, data_type: String(output.dataType ?? 'any') };
  });
}

function compileInputs(inputs, nodeId, nodeMap, edges) {
  if (!Array.isArray(inputs)) throw new Error(`node ${nodeId} inputs must be an array`);
  const compiled = {};
  for (const input of inputs) {
    const name = text(input?.name);
    if (!name) throw new Error(`node ${nodeId} has an input without a name`);
    if (Object.hasOwn(compiled, name)) throw new Error(`node ${nodeId} has duplicate input: ${name}`);
    const dataType = String(input.dataType ?? 'any');
    if (input.source === 'user') {
      compiled[name] = {
        key: name,
        input_schema: 'from_user',
        value: input.value,
        data_type: dataType,
        has_value: Object.hasOwn(input, 'value'),
      };
      continue;
    }
    if (input.source !== 'task') {
      throw new Error(`node ${nodeId} input ${name} has invalid source: ${String(input.source)}`);
    }

    const sourceId = text(input.taskSource?.taskId);
    const outputKey = text(input.taskSource?.outputKey);
    if (!sourceId || !outputKey) throw new Error(`node ${nodeId} input ${name} has incomplete taskSource`);
    const source = nodeMap.get(sourceId);
    if (!source) throw new Error(`node ${nodeId} input ${name} references unknown node: ${sourceId}`);
    if (!source.outputNames.has(outputKey)) {
      throw new Error(`node ${nodeId} input ${name} references unknown output: ${sourceId}.${outputKey}`);
    }

    compiled[name] = {
      key: name,
      input_schema: 'from_task',
      value: `${sourceId}.output.${outputKey}`,
      data_type: dataType,
      has_value: true,
    };
    edges.push({
      source_task_id: sourceId,
      source_output: outputKey,
      target_task_id: nodeId,
      target_input: name,
    });
  }
  return compiled;
}

function validateReactFlowEdges(reactFlowEdges, nodeMap, bindings) {
  for (const edge of reactFlowEdges) {
    if (!nodeMap.has(edge?.source) || !nodeMap.has(edge?.target)) {
      throw new Error(`ReactFlow edge references an unknown node: ${edge?.source} -> ${edge?.target}`);
    }
    if (!bindings.some((binding) => matches(binding, edge))) {
      throw new Error(`ReactFlow edge ${edge.source} -> ${edge.target} disagrees with task bindings`);
    }
  }
  for (const binding of bindings) {
    if (!reactFlowEdges.some((edge) => matches(binding, edge))) {
      throw new Error(
        `task binding ${binding.source_task_id}.${binding.source_output} -> `
        + `${binding.target_task_id}.${binding.target_input} is missing a ReactFlow edge`,
      );
    }
  }
}

function matches(binding, edge) {
  return binding.source_task_id === edge.source
    && binding.target_task_id === edge.target
    && (!edge.sourceHandle || edge.sourceHandle === binding.source_output)
    && (!edge.targetHandle || edge.targetHandle === binding.target_input);
}

function normalizeResources(resources = {}) {
  return {
    cpu_num: resources.cpu_num ?? 1,
    gpu_mem: resources.gpu_mem ?? 0,
    io_num: resources.io_num ?? 0,
  };
}

function isCanonicalTaskPath(value) {
  return typeof value === 'string'
    && /^tasks\/[A-Za-z0-9_./-]+\.py$/.test(value)
    && !value.split('/').includes('..');
}

function text(value) {
  return typeof value === 'string' && value.trim() ? value : undefined;
}
