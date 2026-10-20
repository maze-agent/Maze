/** Generic runtime projection. No integration-specific state or protocols. */
export type RuntimeGraphSnapshot = {
  kind?: string;
  run_id: string;
  status: string;
  task_nodes?: Record<string, any>;
  graph?: { nodes: string[]; edges: Array<{ source: string; target: string }> };
};

export function taskReport(value: any): any | null {
  if (!value || typeof value !== 'object') return null;
  if (!Array.isArray(value) && (typeof value.verdict === 'string' || value.resource_usage)) return value;
  if (Array.isArray(value)) {
    for (const item of value) {
      const report = taskReport(item);
      if (report) return report;
    }
  } else {
    for (const key of ['report', 'result', 'value']) {
      const report = taskReport(value[key]);
      if (report) return report;
    }
  }
  return null;
}

export function acceptedTaskResult(task: any, events: any[] = []) {
  const direct = task?.result_summary ?? task?.result;
  if (direct !== undefined && direct !== null) return direct;
  const outputs = task?.outputs || [];
  if (outputs.some((output: any) => output.value !== undefined && output.value !== null)) {
    return Object.fromEntries(outputs.map((output: any) => [output.name, output.value]));
  }
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.type === 'finish_task' && event.data?.task_id === task?.task_id) return event.data.result;
  }
  return null;
}

export function projectRunGraph(
  run: RuntimeGraphSnapshot,
  previous: Map<string, { x: number; y: number }> = new Map(),
  events: any[] = [],
) {
  const tasks = run.task_nodes || {};
  const ids = [...new Set([...(run.graph?.nodes || []), ...Object.keys(tasks)])]
    .sort((a, b) => Number(tasks[a]?.created_time || 0) - Number(tasks[b]?.created_time || 0) || a.localeCompare(b));
  const known = new Set(ids);
  const links = (run.graph?.edges || []).filter((edge) => known.has(edge.source) && known.has(edge.target));
  const parents = new Map(ids.map((id) => [id, links.filter((edge) => edge.target === id).map((edge) => edge.source)]));
  const levels = new Map<string, number>();
  const level = (id: string, visiting = new Set<string>()): number => {
    if (levels.has(id)) return levels.get(id)!;
    if (visiting.has(id)) return 0;
    const next = new Set([...visiting, id]);
    const value = Math.max(0, ...(parents.get(id) || []).map((parent) => level(parent, next) + 1));
    levels.set(id, value);
    return value;
  };
  const occupied = new Set([...previous.entries()].filter(([id]) => known.has(id)).map(([, p]) => `${p.x}:${p.y}`));
  const nodes = ids.map((id) => {
    const task = tasks[id] || { task_id: id, status: 'pending' };
    let position = previous.get(id);
    if (!position) {
      const x = 24 + level(id) * 290;
      let y = 24;
      while (occupied.has(`${x}:${y}`)) y += 160;
      position = { x, y };
      occupied.add(`${x}:${y}`);
    }
    const report = taskReport(acceptedTaskResult(task, events));
    return {
      id, type: 'runtimeTask', position,
      data: { label: task.task_name || id, task, report },
    };
  });
  return {
    nodes,
    edges: links.map((edge) => ({ id: `dependency:${edge.source}:${edge.target}`, ...edge, type: 'smoothstep', animated: tasks[edge.target]?.status === 'running' })),
  };
}

export function awaitingMoreSteps(run: RuntimeGraphSnapshot) {
  if (run.kind === 'static') return false;
  if (!['created', 'running', 'queued'].includes(run.status)) return false;
  return Object.values(run.task_nodes || {}).every((task: any) => ['completed', 'succeeded', 'failed', 'cancelled', 'canceled'].includes(task.status));
}
