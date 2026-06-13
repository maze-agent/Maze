import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Collapse,
  Descriptions,
  Divider,
  Drawer,
  Empty,
  Input,
  List,
  Modal,
  Popconfirm,
  Space,
  Statistic,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  DownloadOutlined,
  DeleteOutlined,
  EyeOutlined,
  HistoryOutlined,
  InboxOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SearchOutlined,
  StopOutlined,
} from '@ant-design/icons';
import { api } from '@/api/client';
import { useWorkflowStore } from '@/stores/workflowStore';
import { computeReactRunTimeout, normalizeReactTaskTimeout } from '@/utils/reactRuntime';
import type {
  DynamicRunEvent,
  DynamicRunSnapshot,
  DynamicRunStatus,
  RunArtifact,
  RunLogLine,
  StaticWorkflowRunEvent,
  StaticWorkflowRunNode,
  StaticWorkflowRunSnapshot,
  StaticWorkflowRunStatus,
  UnifiedRunSnapshot,
  UnifiedRunTaskSnapshot,
  WorkspaceSkillMeta,
} from '@/types/workflow';
import { loadLlmSettings } from '@/utils/llmSettings';
import ReActRuntimeCanvas, {
  buildAgentTrace,
  formatJson,
  hasAgentTrace,
  type AgentTraceStep,
} from './ReActRuntimeCanvas';
import PendingActionCard from './PendingActionCard';

const { Text, Title } = Typography;

const staticTerminalStatuses = new Set<StaticWorkflowRunStatus>([
  'completed',
  'succeeded',
  'failed',
  'canceled',
  'cancelled',
  'timed_out',
  'interrupted',
]);

const dynamicTerminalStatuses = new Set<DynamicRunStatus>([
  'finalized',
  'succeeded',
  'failed',
  'canceled',
  'cancelled',
  'timed_out',
  'interrupted',
]);

const staticStatusColors: Record<StaticWorkflowRunStatus, string> = {
  created: 'default',
  queued: 'default',
  running: 'processing',
  completed: 'success',
  succeeded: 'success',
  failed: 'error',
  canceled: 'orange',
  cancelled: 'orange',
  timed_out: 'volcano',
  interrupted: 'magenta',
};

const dynamicStatusColors: Record<DynamicRunStatus, string> = {
  created: 'default',
  running: 'processing',
  finalized: 'success',
  succeeded: 'success',
  failed: 'error',
  canceled: 'orange',
  cancelled: 'orange',
  timed_out: 'volcano',
  interrupted: 'magenta',
};

type RunItem =
  | {
      kind: 'static';
      id: string;
      createdTime?: number;
      updatedTime?: number;
      status: StaticWorkflowRunStatus;
      run: StaticWorkflowRunSnapshot;
    }
  | {
      kind: 'dynamic';
      id: string;
      createdTime?: number;
      updatedTime?: number;
      status: DynamicRunStatus;
      run: DynamicRunSnapshot;
    };

type ArtifactPreviewState = {
  artifact: RunArtifact;
  href: string;
  content?: string;
  error?: string;
  loading: boolean;
};

interface RunsInspectorProps {
  open: boolean;
  onClose: () => void;
  focusDynamicRunId?: string | null;
  focusStaticRunId?: string | null;
}

function runKey(item: RunItem) {
  return `${item.kind}:${item.id}`;
}

function shortId(value?: string) {
  if (!value) return '';
  return value.length > 12 ? `${value.slice(0, 8)}...` : value;
}

function formatTime(value?: number | null) {
  if (!value) return '-';
  return new Date(value * 1000).toLocaleString();
}

function formatIsoTime(value?: string | null) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString();
}

function formatBytes(value?: number | null) {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';

  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let current = bytes;
  let unitIndex = 0;
  while (current >= 1024 && unitIndex < units.length - 1) {
    current /= 1024;
    unitIndex += 1;
  }
  return `${current >= 10 ? current.toFixed(1) : current.toFixed(2)} ${units[unitIndex]}`;
}

function formatDurationSeconds(value?: number | null) {
  if (value === undefined || value === null) return '-';
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return '-';
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < 60) return `${seconds.toFixed(2)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${(seconds % 60).toFixed(0)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function nodeDuration(node: any) {
  if (node?.duration_seconds !== undefined && node?.duration_seconds !== null) {
    return Number(node.duration_seconds);
  }

  const started = node?.started_time ?? node?.start_time;
  const finished = node?.finished_time ?? node?.finish_time;
  if (started !== undefined && started !== null && finished !== undefined && finished !== null) {
    return Math.max(0, Number(finished) - Number(started));
  }
  return null;
}

function renderJsonValue(value: any) {
  if (value === undefined || value === null || value === '') return '-';
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  return (
    <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 12 }}>
      {formatJson(value)}
    </pre>
  );
}

function errorSummary(error: any) {
  if (!error) return '';
  if (typeof error === 'string') return error;
  return String(error.message || error.error || error.kind || error.type || formatJson(error));
}

function compactText(value: any, maxLength = 180) {
  const text = typeof value === 'string' ? value : formatJson(value);
  if (text.length <= maxLength) return text;
  return `${text.slice(0, maxLength - 1)}...`;
}

function formatResources(resources?: any) {
  if (!resources || typeof resources !== 'object') return '';
  const parts = [];
  if (resources.cpu !== undefined) parts.push(`CPU ${resources.cpu}`);
  if (resources.gpu !== undefined) parts.push(`GPU ${resources.gpu}`);
  if (resources.cpu_mem !== undefined) parts.push(`Mem ${resources.cpu_mem}MB`);
  if (resources.gpu_mem !== undefined) parts.push(`VRAM ${resources.gpu_mem}MB`);
  return parts.join(' / ');
}

function runtimeNodeInfo(node: any) {
  const selected = node?.selected_node || node?.schedule_decision?.selected_node || {};
  return {
    nodeIp: selected.node_ip || node?.node_ip || null,
    nodeId: selected.node_id || node?.node_id_runtime || null,
    gpuId: selected.gpu_id ?? node?.gpu_id ?? null,
  };
}

function collectPlacementSummary(nodes: any[]) {
  const byKey = new Map<string, {
    nodeIp?: string | null;
    nodeId?: string | null;
    gpuIds: Set<string>;
    count: number;
  }>();

  nodes.forEach((node) => {
    const runtime = runtimeNodeInfo(node);
    if (!runtime.nodeIp && !runtime.nodeId) return;
    const key = `${runtime.nodeIp || runtime.nodeId}`;
    const current = byKey.get(key) || {
      nodeIp: runtime.nodeIp,
      nodeId: runtime.nodeId,
      gpuIds: new Set<string>(),
      count: 0,
    };
    if (runtime.gpuId !== undefined && runtime.gpuId !== null) {
      current.gpuIds.add(String(runtime.gpuId));
    }
    current.count += 1;
    byKey.set(key, current);
  });

  return Array.from(byKey.values());
}

function collectResourceSummary(nodes: any[]) {
  const values = new Set<string>();
  nodes.forEach((node) => {
    const direct = formatResources(node?.resources);
    const requested = formatResources(node?.schedule_decision?.requested_resources);
    if (direct) values.add(direct);
    if (requested) values.add(requested);
  });
  return Array.from(values);
}

function collectSandboxSummary(nodes: any[], events: DynamicRunEvent[] = [], run?: DynamicRunSnapshot | null) {
  const values = new Set<string>();
  const finalBackend = run?.final_result?.exec_backend || run?.final_result?.backend;
  if (finalBackend) values.add(String(finalBackend));

  events.forEach((event) => {
    const data = event.data || {};
    if (data.exec_backend) values.add(String(data.exec_backend));
    if (data.sandbox_backend) values.add(String(data.sandbox_backend));
  });

  nodes.forEach((node) => {
    const result = node?.result_summary || node?.result || {};
    const backend = result?.metadata?.sandbox_backend || result?.sandbox_backend || result?.backend;
    if (backend) values.add(String(backend));
  });

  return Array.from(values);
}

function collectToolSummary(events: DynamicRunEvent[]) {
  const byTool = new Map<string, { count: number; failed: number }>();
  events.forEach((event) => {
    const data = event.data || {};
    const tool = data.tool || data.tool_name;
    if (!tool) return;
    if (![
      'agent_action',
      'agent_tool_call_started',
      'agent_tool_call_finished',
      'agent_observation',
      'agent_repair_observation',
    ].includes(event.type)) {
      return;
    }
    const key = String(tool);
    const current = byTool.get(key) || { count: 0, failed: 0 };
    if (event.type === 'agent_action' || event.type === 'agent_tool_call_started') {
      current.count += 1;
    }
    if (data.error || data.error_type || data.ok === false) {
      current.failed += 1;
    }
    byTool.set(key, current);
  });
  return Array.from(byTool.entries())
    .map(([tool, summary]) => ({ tool, ...summary }))
    .sort((a, b) => a.tool.localeCompare(b.tool));
}

function collectRunErrors(run: any, nodes: any[], events: DynamicRunEvent[] = []) {
  const errors: string[] = [];
  if (run?.failure_reason) errors.push(compactText(run.failure_reason));
  if (run?.cancel_reason) errors.push(compactText(run.cancel_reason));
  nodes.forEach((node) => {
    if (node?.error || node?.last_error) {
      errors.push(compactText(node.error || node.last_error));
    }
  });
  events.forEach((event) => {
    if (['agent_error', 'agent_skill_load_failed', 'task_exception'].includes(event.type)) {
      errors.push(compactText(event.data?.error || event.data?.result || event.data));
    }
  });
  return Array.from(new Set(errors)).slice(0, 5);
}

function issueGuidance(issue: any) {
  const text = compactText(issue, 300);
  const lower = text.toLowerCase();
  let stage = 'runtime';
  let suggestion = 'Open the failed task or event, inspect the raw error, then retry after fixing the input, tool call, or environment.';

  if (lower.includes('api key') || lower.includes('unauthorized') || lower.includes('401')) {
    stage = 'llm';
    suggestion = 'Check Advanced Setting -> LLM API key/base URL/model, then rerun the ReAct workflow.';
  } else if (lower.includes('skill_not_found') || lower.includes('skill not found')) {
    stage = 'skills';
    suggestion = 'Import the missing skill from Library, or remove it from the ReAct run skill selection.';
  } else if (lower.includes('docker')) {
    stage = 'sandbox';
    suggestion = 'Switch to workspace_sandbox, or connect a worker that reports docker_sandbox=true before rerunning.';
  } else if (lower.includes('permission')) {
    stage = 'sandbox';
    suggestion = 'Review the permission target and policy. Prefer writing under workspace/files or generated artifact paths.';
  } else if (lower.includes('timeout') || lower.includes('timed out')) {
    stage = 'execution';
    suggestion = 'Increase timeout, reduce max steps, or split the work into a smaller task before rerunning.';
  } else if (lower.includes('no registered') || lower.includes('no alive') || lower.includes('insufficient')) {
    stage = 'scheduler';
    suggestion = 'Check Cluster resources. Reconnect worker nodes or lower requested CPU/GPU resources.';
  } else if (lower.includes('json') || lower.includes('parse')) {
    stage = 'llm/tool';
    suggestion = 'Inspect the raw LLM decision and repair prompt. Rerun after making the prompt/tool schema more explicit.';
  }

  return { stage, issue: text, suggestion };
}

function collectRepairGuidance(run: any, nodes: any[], events: DynamicRunEvent[] = []) {
  const rawIssues: any[] = [];
  if (run?.failure_reason) rawIssues.push(run.failure_reason);
  if (run?.cancel_reason) rawIssues.push(run.cancel_reason);
  nodes.forEach((node) => {
    if (node?.error || node?.last_error) rawIssues.push(node.error || node.last_error);
    if (node?.pending_reason) rawIssues.push(node.pending_reason);
  });
  events.forEach((event) => {
    if (['agent_error', 'agent_skill_load_failed', 'task_exception'].includes(event.type)) {
      rawIssues.push(event.data?.error || event.data?.result || event.data);
    }
  });

  const byKey = new Map<string, ReturnType<typeof issueGuidance>>();
  rawIssues.forEach((issue) => {
    const guidance = issueGuidance(issue);
    byKey.set(`${guidance.stage}:${guidance.issue}`, guidance);
  });
  return Array.from(byKey.values()).slice(0, 5);
}

function artifactLooksImage(artifact: RunArtifact) {
  const mime = String(artifact.mime || '').toLowerCase();
  const name = String(artifact.path || artifact.name || '').toLowerCase();
  return mime.startsWith('image/') || /\.(png|jpe?g|gif|webp|svg)$/.test(name);
}

function artifactLooksText(artifact: RunArtifact) {
  const mime = String(artifact.mime || '').toLowerCase();
  const name = String(artifact.path || artifact.name || '').toLowerCase();
  return (
    mime.startsWith('text/') ||
    mime.includes('json') ||
    mime.includes('xml') ||
    /\.(txt|md|json|jsonl|csv|log|py|js|ts|tsx|jsx|html|css|xml|yaml|yml|toml|canvas)$/.test(name)
  );
}

function canPreviewArtifact(artifact: RunArtifact) {
  return artifactLooksImage(artifact) || artifactLooksText(artifact);
}

function formatArtifactPreview(artifact: RunArtifact, content: string) {
  const name = String(artifact.path || artifact.name || '').toLowerCase();
  const mime = String(artifact.mime || '').toLowerCase();
  if (mime.includes('json') || /\.(json|canvas)$/.test(name)) {
    try {
      return JSON.stringify(JSON.parse(content), null, 2);
    } catch {
      return content;
    }
  }
  return content;
}

function inferReactRerunMode(started: Record<string, any> | null, run?: DynamicRunSnapshot | null): 'local' | 'online' {
  const llmTask = String(started?.llm_task || '').toLowerCase();
  const mode = String(started?.mode || run?.final_result?.mode || run?.mode || '').toLowerCase();
  if (llmTask.includes('openai') || llmTask.includes('online')) return 'online';
  if (mode === 'local' || mode.includes('local')) return 'local';
  if (mode === 'online' || mode.includes('online')) return 'online';
  return 'local';
}

function scheduleRejectSummary(decision: any): string[] {
  const candidates = decision?.candidate_nodes || [];
  return candidates
    .filter((candidate: any) => Array.isArray(candidate.reject_reasons) && candidate.reject_reasons.length > 0)
    .slice(0, 4)
    .map((candidate: any) => (
      `${candidate.node_ip || shortId(candidate.node_id)}: ${candidate.reject_reasons.join(', ')}`
    ));
}

function getRunMode(run?: DynamicRunSnapshot | null) {
  if (!run) return null;
  if (run.mode) return String(run.mode);
  if (run.final_result?.mode) return String(run.final_result.mode);
  if (run.task_specs?.react_llm_decision) return 'react';
  const taskNodes = Object.values(run.task_nodes || {});
  if (taskNodes.some((node: any) => node?.task_spec_id === 'react_llm_decision')) {
    return 'react';
  }
  return null;
}

function runModeTag(run?: DynamicRunSnapshot | null) {
  const mode = getRunMode(run);
  if (!mode) return null;
  return <Tag color={mode === 'react' ? 'purple' : 'geekblue'}>{mode}</Tag>;
}

function isAppRun(run?: StaticWorkflowRunSnapshot | DynamicRunSnapshot | null) {
  return Boolean((run as any)?.metadata?.run_kind === 'app' || (run as any)?.metadata?.app_spec);
}

function appRunName(run?: StaticWorkflowRunSnapshot | UnifiedRunSnapshot | null) {
  const metadata = run?.metadata || {};
  return String(metadata.app_name || metadata.workflow_name || (run as any)?.workflow_name || run?.workflow_id || 'Workflow Run');
}

function completedCount(counts?: Record<string, number>) {
  return Number(counts?.completed || counts?.succeeded || 0);
}

function staticTaskStatus(status?: string): StaticWorkflowRunNode['status'] {
  if (status === 'succeeded') return 'completed';
  if (status === 'cancelled') return 'canceled';
  return (status || 'pending') as StaticWorkflowRunNode['status'];
}

function dynamicTaskStatus(status?: string) {
  if (status === 'succeeded') return 'completed';
  if (status === 'queued') return 'submitted';
  return status || 'pending';
}

function adaptStaticRun(run: UnifiedRunSnapshot): StaticWorkflowRunSnapshot {
  const taskNodes = Object.fromEntries(
    Object.entries(run.task_nodes || {}).map(([taskId, task]: [string, UnifiedRunTaskSnapshot]) => {
      const selectedNode = task.selected_node || {};
      return [
        taskId,
        {
          node_id: taskId,
          task_name: task.task_name,
          label: task.task_name,
          status: staticTaskStatus(task.status),
          created_time: task.created_time,
          started_time: task.started_time,
          finished_time: task.finished_time,
          result_summary: task.result_summary,
          error: task.error || task.last_error,
          last_error: task.last_error,
          pending_reason: task.pending_reason,
          retry_wait_seconds: task.retry_wait_seconds,
          next_eligible_time: task.next_eligible_time,
          timeout_seconds: task.timeout_seconds,
          maze_task_id: task.task_id,
          node_ip: selectedNode.node_ip,
          node_id_runtime: selectedNode.node_id,
          gpu_id: selectedNode.gpu_id,
          duration_seconds: task.duration_seconds,
          resources: task.resources,
          schedule_decision: task.schedule_decision,
          file_manifest: task.file_manifest,
          artifacts: task.file_manifest?.files || [],
        },
      ];
    }),
  );

  return {
    schema: 'static_workflow_run',
    schema_version: run.schema_version || 1,
    kind: 'static',
    run_id: run.run_id,
    workflow_id: run.workflow_id || run.run_id,
    workflow_name: String(run.metadata?.workflow_name || run.workflow_id || 'Workflow Run'),
    status: (run.status === 'cancelled' ? 'canceled' : run.status) as StaticWorkflowRunStatus,
    created_time: run.created_time,
    updated_time: run.updated_time,
    finished_time: run.finished_time,
    task_counts: run.task_counts,
    task_nodes: taskNodes,
    graph: run.graph,
    events: {
      count: run.event_count || 0,
      last_seq: run.last_event_seq || 0,
    },
    final_result: run.result_summary,
    result_summary: run.result_summary,
    error: run.error_summary,
    error_summary: run.error_summary,
    maze_run_id: run.run_id,
    metadata: run.metadata,
    tags: run.tags,
  };
}

async function loadRunArtifacts(runId: string): Promise<RunArtifact[]> {
  try {
    const result = await api.getRunArtifacts(runId);
    return (result.artifacts || []) as RunArtifact[];
  } catch (error) {
    console.warn('Failed to load run artifacts:', error);
    return [];
  }
}

function adaptDynamicRun(run: UnifiedRunSnapshot): DynamicRunSnapshot {
  const normalizedTaskNodes = Object.fromEntries(
    Object.entries(run.task_nodes || {}).map(([taskId, task]: [string, UnifiedRunTaskSnapshot]) => [
      taskId,
      {
        ...task,
        status: dynamicTaskStatus(task.status),
        start_time: task.start_time ?? task.started_time,
        finish_time: task.finish_time ?? task.finished_time,
      },
    ]),
  );

  return {
    schema: run.schema,
    schema_version: run.schema_version,
    run_id: run.run_id,
    status: (run.native_status || run.status) as DynamicRunStatus,
    kind: 'dynamic',
    summary: run.summary,
    mode: run.mode || (run.run_type === 'react' ? 'react' : undefined),
    max_tasks: run.max_tasks,
    timeout_seconds: run.timeout_seconds,
    created_time: run.created_time,
    updated_time: run.updated_time,
    finished_time: run.finished_time,
    task_counts: run.task_counts,
    task_specs: (run as any).task_specs,
    task_nodes: normalizedTaskNodes,
    graph: run.graph,
    request_ids: (run as any).request_ids,
    event_count: run.event_count,
    last_event_seq: run.last_event_seq,
    final_result: run.final_result || run.result_summary,
    cancel_reason: run.cancel_reason,
    failure_reason: run.failure_reason || run.error_summary,
    metadata: run.metadata,
  };
}

function staticEventSummary(event: StaticWorkflowRunEvent) {
  const data = event.data || {};
  const taskName = data.node_label || data.task_name || data.node_id || 'task';

  switch (event.type) {
    case 'workflow_started':
      return 'Workflow run started';
    case 'building':
      return data.message || 'Building workflow';
    case 'maze_run_created':
      return `Maze run created: ${shortId(data.maze_run_id)}`;
    case 'task_ready':
      return `${taskName} is ready`;
    case 'start_task':
      return `${taskName} started`;
    case 'finish_task':
      return `${taskName} completed`;
    case 'task_exception':
      return `${taskName} failed`;
    case 'workflow_completed':
      return 'Workflow completed';
    case 'workflow_failed':
      return data.error || 'Workflow failed';
    case 'workflow_interrupted':
      return data.message || 'Workflow interrupted';
    default:
      return event.type;
  }
}

function dynamicEventSummary(event: DynamicRunEvent) {
  const data = event.data || {};
  const taskId = shortId(data.task_id);

  switch (event.type) {
    case 'start_dynamic_run':
      return `Run ${shortId(data.run_id)} started`;
    case 'register_task_spec':
      return `Task spec registered: ${data.task_name || data.task_spec_id || 'unknown'}`;
    case 'append_task':
      return `Task ${taskId} appended${data.status ? ` (${data.status})` : ''}`;
    case 'task_ready':
      return `Task ${taskId} is ready`;
    case 'start_task':
      return `Task ${taskId} started`;
    case 'finish_task':
      return `Task ${taskId} finished`;
    case 'task_exception':
      return `Task ${taskId} failed: ${data.result || 'Unknown error'}`;
    case 'finish_workflow':
      return 'Run finalized';
    case 'cancel_dynamic_run':
      return `Run canceled${data.reason ? `: ${data.reason}` : ''}`;
    case 'timeout_dynamic_run':
      return `Run timed out${data.timeout_seconds ? ` after ${data.timeout_seconds}s` : ''}`;
    case 'interrupt_dynamic_run':
      return data.reason || 'Run interrupted';
    case 'agent_run_started':
      return `Agent started with ${Array.isArray(data.tools) ? data.tools.length : 0} tool(s)`;
    case 'agent_skill_loaded':
      return `Skill loaded: ${data.skill?.name || data.skill || 'unknown'}`;
    case 'agent_skill_load_failed':
      return `Skill load failed: ${data.skill || data.error?.details?.skill || data.error?.message || 'unknown'}`;
    case 'agent_action':
      return `Agent step ${data.step || '?'} selected ${data.tool || 'unknown tool'}`;
    case 'agent_observation':
      return `Agent step ${data.step || '?'} observed ${data.tool || 'tool'} result`;
    case 'agent_repair_observation':
      return `Agent step ${data.step || '?'} recorded a repair observation`;
    case 'agent_final':
      return 'Agent produced a final answer';
    case 'agent_error':
      return `Agent error: ${data.error || 'Unknown error'}`;
    default:
      return event.type;
  }
}

function normalizeSkillSummary(skill: any, source?: string): WorkspaceSkillMeta & { source?: string } | null {
  if (!skill) return null;
  const name = String(skill.name || '').trim();
  if (!name) return null;
  return {
    name,
    path: String(skill.path || ''),
    description: skill.description ? String(skill.description) : '',
    metadata: skill.metadata || {},
    resources: Array.isArray(skill.resources) ? skill.resources : [],
    truncated: Boolean(skill.truncated),
    original_chars: skill.original_chars,
    returned_chars: skill.returned_chars,
    source,
  };
}

function collectLoadedSkills(
  run?: DynamicRunSnapshot | null,
  events: DynamicRunEvent[] = [],
): Array<WorkspaceSkillMeta & { source?: string }> {
  const byName = new Map<string, WorkspaceSkillMeta & { source?: string }>();
  const addSkill = (skill: any, source?: string) => {
    const normalized = normalizeSkillSummary(skill, source);
    if (!normalized) return;
    byName.set(normalized.name, {
      ...(byName.get(normalized.name) || {}),
      ...normalized,
      source: normalized.source || byName.get(normalized.name)?.source,
    });
  };

  const finalSkills = Array.isArray(run?.final_result?.skills) ? run?.final_result?.skills : [];
  finalSkills.forEach((skill: any) => addSkill(skill, 'final'));
  events.forEach((event) => {
    const data = event.data || {};
    if (event.type === 'agent_run_started') {
      const startedSkills = Array.isArray(data.skills) ? data.skills : [];
      startedSkills.forEach((skill: any) => addSkill(skill, 'start'));
    }
    if (event.type === 'agent_skill_loaded') {
      addSkill(data.skill, data.source || 'event');
    }
  });

  return Array.from(byName.values()).sort((a, b) => a.name.localeCompare(b.name));
}

function rawDecisionText(step: AgentTraceStep) {
  if (step.decision?.raw) return String(step.decision.raw);
  if (step.decision && step.decision !== step.action) return formatJson(step.decision);
  return '';
}

function itemStatusColor(item: RunItem) {
  return item.kind === 'static'
    ? staticStatusColors[item.status]
    : dynamicStatusColors[item.status];
}

function isTerminalItem(item: RunItem) {
  return item.kind === 'static'
    ? staticTerminalStatuses.has(item.status)
    : dynamicTerminalStatuses.has(item.status);
}

export default function RunsInspector({
  open,
  onClose,
  focusDynamicRunId,
  focusStaticRunId,
}: RunsInspectorProps) {
  const {
    workspaceId,
    workspaceDir,
    staticRuns,
    setStaticRuns,
    upsertStaticRun,
    setStaticRunEvents,
    removeStaticRun,
  } = useWorkflowStore();
  const [dynamicRuns, setDynamicRuns] = useState<DynamicRunSnapshot[]>([]);
  const [selectedRunKey, setSelectedRunKey] = useState<string | null>(null);
  const [selectedStaticRun, setSelectedStaticRun] = useState<StaticWorkflowRunSnapshot | null>(null);
  const [selectedDynamicRun, setSelectedDynamicRun] = useState<DynamicRunSnapshot | null>(null);
  const [selectedRunArtifacts, setSelectedRunArtifacts] = useState<RunArtifact[]>([]);
  const [selectedRunLogs, setSelectedRunLogs] = useState<RunLogLine[]>([]);
  const [staticEvents, setStaticEvents] = useState<StaticWorkflowRunEvent[]>([]);
  const [dynamicEvents, setDynamicEvents] = useState<DynamicRunEvent[]>([]);
  const [lastAppliedFocusKey, setLastAppliedFocusKey] = useState<string | null>(null);
  const [filterText, setFilterText] = useState('');
  const [loading, setLoading] = useState(false);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [cleanupLoading, setCleanupLoading] = useState(false);
  const [staticCleanupLoading, setStaticCleanupLoading] = useState(false);
  const [artifactCleanupLoading, setArtifactCleanupLoading] = useState(false);
  const [runActionLoading, setRunActionLoading] = useState(false);
  const [artifactPreview, setArtifactPreview] = useState<ArtifactPreviewState | null>(null);
  const [promotingArtifactKey, setPromotingArtifactKey] = useState<string | null>(null);

  const runItems = useMemo<RunItem[]>(() => {
    const staticItems: RunItem[] = staticRuns.map((run) => ({
      kind: 'static',
      id: run.run_id,
      createdTime: run.created_time,
      updatedTime: run.updated_time,
      status: run.status,
      run,
    }));

    const dynamicItems: RunItem[] = dynamicRuns.map((run) => ({
      kind: 'dynamic',
      id: run.run_id,
      createdTime: run.created_time,
      updatedTime: run.updated_time,
      status: run.status,
      run,
    }));

    return [...staticItems, ...dynamicItems].sort((a, b) => (
      (b.updatedTime || b.createdTime || 0) - (a.updatedTime || a.createdTime || 0)
    ));
  }, [dynamicRuns, staticRuns]);

  const filteredRunItems = useMemo(() => {
    const query = filterText.trim().toLowerCase();
    if (!query) return runItems;
    return runItems.filter((item) => {
      const label = item.kind === 'static'
        ? appRunName(item.run)
        : getRunMode(item.run) || 'dynamic';
      return [
        item.id,
        item.kind,
        item.status,
        label,
      ].some((value) => String(value).toLowerCase().includes(query));
    });
  }, [filterText, runItems]);

  const selectedItem = useMemo(
    () => runItems.find((item) => runKey(item) === selectedRunKey) || null,
    [runItems, selectedRunKey],
  );

  const selectedStaticTaskNodes = useMemo(() => {
    if (!selectedStaticRun?.task_nodes) return [];
    return Object.values(selectedStaticRun.task_nodes);
  }, [selectedStaticRun]);

  const selectedDynamicTaskNodes = useMemo(() => {
    if (!selectedDynamicRun?.task_nodes) return [];
    return Object.values(selectedDynamicRun.task_nodes);
  }, [selectedDynamicRun]);

  const selectedDynamicEdges = selectedDynamicRun?.graph?.edges || [];
  const agentTrace = useMemo(() => buildAgentTrace(dynamicEvents), [dynamicEvents]);
  const showAgentTrace = hasAgentTrace(agentTrace);
  const selectedLoadedSkills = useMemo(
    () => collectLoadedSkills(selectedDynamicRun, dynamicEvents),
    [dynamicEvents, selectedDynamicRun],
  );

  const loadRuns = useCallback(async (silent = false) => {
    if (!silent) {
      setLoading(true);
    }

    try {
      const result = await api.getRuns({ limit: 100, detail: false });
      const runs = result.runs || [];
      setStaticRuns(runs.filter((run) => run.kind === 'static').map((run) => adaptStaticRun(run)));
      setDynamicRuns(runs.filter((run) => run.kind !== 'static').map((run) => adaptDynamicRun(run)));
    } catch (error: any) {
      console.error('Failed to load runs:', error);
      if (!silent) {
        message.error(error.response?.data?.error || 'Failed to load runs');
      }
    } finally {
      if (!silent) {
        setLoading(false);
      }
    }
  }, [setStaticRuns]);

  const loadStaticRunDetails = useCallback(async (runId: string, silent = false) => {
    if (!silent) {
      setDetailsLoading(true);
    }

    try {
      const [runResult, eventResult, artifacts, logResult] = await Promise.all([
        api.getRun(runId),
        api.getRunEvents(runId),
        loadRunArtifacts(runId),
        api.getRunLogs(runId, { tail: 500 }),
      ]);
      const adaptedRun = adaptStaticRun(runResult.run);
      setSelectedStaticRun(adaptedRun);
      setStaticEvents((eventResult.events || []) as StaticWorkflowRunEvent[]);
      setSelectedRunArtifacts(artifacts);
      setSelectedRunLogs((logResult.lines || []) as RunLogLine[]);
      setSelectedDynamicRun(null);
      setDynamicEvents([]);
      upsertStaticRun(adaptedRun);
      setStaticRunEvents(runId, (eventResult.events || []) as StaticWorkflowRunEvent[]);
    } catch (error: any) {
      console.error('Failed to open workflow run:', error);
      setSelectedRunArtifacts([]);
      setSelectedRunLogs([]);
      if (!silent) {
        message.error(error.response?.data?.error || 'Failed to open workflow run');
      }
    } finally {
      if (!silent) {
        setDetailsLoading(false);
      }
    }
  }, [setStaticRunEvents, upsertStaticRun]);

  const loadDynamicRunDetails = useCallback(async (runId: string, silent = false) => {
    if (!silent) {
      setDetailsLoading(true);
    }

    try {
      const [runResult, eventResult, artifacts, logResult] = await Promise.all([
        api.getRun(runId),
        api.getRunEvents(runId),
        loadRunArtifacts(runId),
        api.getRunLogs(runId, { tail: 500 }),
      ]);
      const adaptedRun = adaptDynamicRun(runResult.run);
      setSelectedDynamicRun(adaptedRun);
      setDynamicEvents((eventResult.events || []) as DynamicRunEvent[]);
      setSelectedRunArtifacts(artifacts);
      setSelectedRunLogs((logResult.lines || []) as RunLogLine[]);
      setSelectedStaticRun(null);
      setStaticEvents([]);
      setDynamicRuns((current) => [
        adaptedRun,
        ...current.filter((run) => run.run_id !== adaptedRun.run_id),
      ]);
    } catch (error: any) {
      console.error('Failed to open dynamic run:', error);
      setSelectedRunArtifacts([]);
      setSelectedRunLogs([]);
      if (!silent) {
        message.error(error.response?.data?.error || 'Failed to open dynamic run');
      }
    } finally {
      if (!silent) {
        setDetailsLoading(false);
      }
    }
  }, []);

  const selectRun = useCallback((item: RunItem, silent = false) => {
    setSelectedRunKey(runKey(item));
    if (item.kind === 'static') {
      void loadStaticRunDetails(item.id, silent);
    } else {
      void loadDynamicRunDetails(item.id, silent);
    }
  }, [loadDynamicRunDetails, loadStaticRunDetails]);

  const selectedIsLoaded = (item: RunItem) => (
    item.kind === 'static'
      ? selectedStaticRun?.run_id === item.id
      : selectedDynamicRun?.run_id === item.id
  );

  const deleteSelectedRun = async () => {
    if (!selectedItem) return;

    try {
      if (selectedItem.kind === 'static') {
        await api.deleteStaticWorkflowRun(selectedItem.id, workspaceDir || undefined);
        removeStaticRun(selectedItem.id);
        message.success('Workflow run deleted');
      } else {
        await api.deleteDynamicRun(selectedItem.id);
        setDynamicRuns((current) => current.filter((run) => run.run_id !== selectedItem.id));
        message.success('Dynamic run deleted');
      }

      setSelectedRunKey(null);
      setSelectedStaticRun(null);
      setSelectedDynamicRun(null);
      setSelectedRunArtifacts([]);
      setSelectedRunLogs([]);
      setStaticEvents([]);
      setDynamicEvents([]);
      await loadRuns(true);
    } catch (error: any) {
      console.error('Failed to delete run:', error);
      message.error(error.response?.data?.error || 'Failed to delete run');
    }
  };

  const runDryCleanup = async () => {
    setCleanupLoading(true);
    try {
      const result = await api.cleanupDynamicRuns({
        statuses: ['finalized', 'failed', 'canceled', 'timed_out', 'interrupted'],
        older_than_days: 7,
        dry_run: true,
      });
      message.info(`Dynamic cleanup dry run matched ${result.cleanup?.matched_count || 0} run(s)`);
    } catch (error: any) {
      console.error('Failed to run cleanup dry run:', error);
      message.error(error.response?.data?.error || 'Failed to run dynamic cleanup dry run');
    } finally {
      setCleanupLoading(false);
    }
  };

  const cleanupStaticRuns = async (dryRun: boolean) => {
    setStaticCleanupLoading(true);
    try {
      const result = await api.cleanupStaticWorkflowRuns({
        workspaceId: workspaceId || undefined,
        workspaceDir: workspaceDir || undefined,
        statuses: ['completed', 'succeeded', 'failed', 'canceled', 'cancelled', 'timed_out', 'interrupted'],
        older_than_days: 7,
        keep_latest: 100,
        dry_run: dryRun,
      });
      const cleanup = result.cleanup || {};
      if (dryRun) {
        message.info(`Static cleanup dry run matched ${cleanup.matched_count || 0} run(s)`);
      } else {
        message.success(`Deleted ${cleanup.deleted_count || 0} old static run(s)`);
        setSelectedRunKey(null);
        setSelectedStaticRun(null);
        setSelectedRunArtifacts([]);
        setSelectedRunLogs([]);
        setStaticEvents([]);
        await loadRuns(true);
      }
    } catch (error: any) {
      console.error('Failed to clean up static runs:', error);
      message.error(error.response?.data?.error || 'Failed to clean up static runs');
    } finally {
      setStaticCleanupLoading(false);
    }
  };

  const cleanupArtifacts = async (dryRun: boolean) => {
    setArtifactCleanupLoading(true);
    try {
      const result = await api.cleanupArtifacts({
        workspaceId: workspaceId || undefined,
        workspaceDir: workspaceDir || undefined,
        older_than_days: 7,
        dry_run: dryRun,
      });
      const cleanup = result.cleanup || {};
      if (dryRun) {
        message.info(`Artifact cleanup dry run matched ${cleanup.matched_count || 0} orphan artifact(s)`);
      } else {
        message.success(`Deleted ${cleanup.deleted_count || 0} orphan artifact(s)`);
      }
    } catch (error: any) {
      console.error('Failed to clean up artifacts:', error);
      message.error(error.response?.data?.error || 'Failed to clean up artifacts');
    } finally {
      setArtifactCleanupLoading(false);
    }
  };

  const cancelSelectedRun = async () => {
    if (!selectedItem) return;
    setRunActionLoading(true);
    try {
      await api.cancelRun(selectedItem.id, 'Canceled from Maze Playground');
      message.success('Run canceled');
      await loadRuns(true);
      if (selectedItem.kind === 'static') {
        await loadStaticRunDetails(selectedItem.id, true);
      } else {
        await loadDynamicRunDetails(selectedItem.id, true);
      }
    } catch (error: any) {
      console.error('Failed to cancel run:', error);
      message.error(error.response?.data?.error || 'Failed to cancel run');
    } finally {
      setRunActionLoading(false);
    }
  };

  const retrySelectedRun = async () => {
    if (!selectedItem) return;
    setRunActionLoading(true);
    try {
      const result = await api.retryRun(selectedItem.id);
      message.success('Run submitted');
      await loadRuns(true);
      setSelectedRunKey(`static:${result.runId}`);
      await loadStaticRunDetails(result.runId, true);
    } catch (error: any) {
      console.error('Failed to retry run:', error);
      message.error(error.response?.data?.error || 'Failed to retry run');
    } finally {
      setRunActionLoading(false);
    }
  };

  const rerunSelectedDynamicReactRun = async () => {
    if (!selectedDynamicRun) return;

    const started = agentTrace.started || {};
    const prompt = String(started.prompt || '').trim();
    if (!prompt) {
      message.warning('The selected ReAct run does not include its original prompt');
      return;
    }

    const mode = inferReactRerunMode(agentTrace.started, selectedDynamicRun);
    const maxSteps = Number(started.max_steps || selectedDynamicRun.final_result?.max_steps || 4);
    const maxTokensValue = Number(started.max_tokens || selectedDynamicRun.final_result?.max_tokens || 0);
    const taskTimeout = normalizeReactTaskTimeout(
      started.task_timeout || selectedDynamicRun.final_result?.task_timeout,
      mode,
    );
    const sandboxes = collectSandboxSummary(selectedDynamicTaskNodes, dynamicEvents, selectedDynamicRun);
    const execBackend = (
      sandboxes.find((value) => value === 'workspace_sandbox' || value === 'docker')
      || selectedDynamicRun.final_result?.exec_backend
      || 'workspace_sandbox'
    ) as 'workspace_sandbox' | 'docker';

    const llmSettings = loadLlmSettings();
    if (mode === 'online') {
      if (!llmSettings.baseUrl.trim() || !llmSettings.model.trim() || !llmSettings.apiKey.trim()) {
        message.warning('Please configure online LLM settings first');
        return;
      }
    }

    setRunActionLoading(true);
    try {
      const result = await api.startReactRun({
        mode,
        prompt,
        workspaceId: workspaceId || undefined,
        workspaceDir: workspaceDir || undefined,
        maxSteps: Number.isFinite(maxSteps) && maxSteps > 0 ? maxSteps : 4,
        maxTokens: Number.isFinite(maxTokensValue) && maxTokensValue > 0 ? maxTokensValue : undefined,
        timeoutSeconds: computeReactRunTimeout(maxSteps, taskTimeout),
	        taskTimeout,
	        skills: selectedLoadedSkills.map((skill) => skill.name),
	        execBackend,
	        permissionAskTimeoutSeconds: 120,
	        llm: mode === 'online'
          ? {
              ...llmSettings,
              baseUrl: llmSettings.baseUrl.trim(),
              model: llmSettings.model.trim(),
            }
          : undefined,
      });

      message.success(`ReAct rerun started: ${result.runId.slice(0, 8)}...`);
      await loadRuns(true);
      setSelectedRunKey(`dynamic:${result.runId}`);
      await loadDynamicRunDetails(result.runId, true);
    } catch (error: any) {
      console.error('Failed to rerun ReAct workflow:', error);
      message.error(error.response?.data?.error || 'Failed to rerun ReAct workflow');
    } finally {
      setRunActionLoading(false);
    }
  };

  const decidePermissionRequest = async (event: any, action: 'allow' | 'deny') => {
    if (!selectedDynamicRun) return;
    const request = event.request || {};
    const requestId = event.request_id || request.request_id;
    if (!requestId) {
      message.warning('Permission request id is missing');
      return;
    }

    setRunActionLoading(true);
    try {
      await api.decideDynamicPermissionRequest(selectedDynamicRun.run_id, requestId, {
        action,
        reason: `playground ${action}`,
      });
      message.success(`Permission ${action === 'allow' ? 'allowed' : 'denied'}`);
      await loadDynamicRunDetails(selectedDynamicRun.run_id, true);
    } catch (error: any) {
      message.error(error.response?.data?.error || `Failed to ${action} permission request`);
    } finally {
      setRunActionLoading(false);
    }
  };

  useEffect(() => {
    if (open) {
      void loadRuns();
    }
  }, [loadRuns, open]);

  useEffect(() => {
    if (!open || runItems.length === 0) {
      return;
    }

    const requestedFocusKey = focusDynamicRunId
      ? `dynamic:${focusDynamicRunId}`
      : focusStaticRunId
        ? `static:${focusStaticRunId}`
        : null;
    const shouldApplyFocus = Boolean(requestedFocusKey && requestedFocusKey !== lastAppliedFocusKey);
    const focusedDynamic = shouldApplyFocus && focusDynamicRunId
      ? runItems.find((item) => item.kind === 'dynamic' && item.id === focusDynamicRunId)
      : null;
    const focusedStatic = shouldApplyFocus && focusStaticRunId
      ? runItems.find((item) => item.kind === 'static' && item.id === focusStaticRunId)
      : null;
    const current = selectedRunKey
      ? runItems.find((item) => runKey(item) === selectedRunKey)
      : null;
    const next = focusedDynamic || focusedStatic || current || runItems[0];

    if (!next || (runKey(next) === selectedRunKey && selectedIsLoaded(next))) {
      return;
    }

    if (focusedDynamic || focusedStatic) {
      setLastAppliedFocusKey(runKey(next));
    }
    selectRun(next, true);
  }, [
    focusDynamicRunId,
    focusStaticRunId,
    lastAppliedFocusKey,
    open,
    runItems,
    selectRun,
    selectedDynamicRun?.run_id,
    selectedRunKey,
    selectedStaticRun?.run_id,
  ]);

  useEffect(() => {
    if (!open || !selectedItem || isTerminalItem(selectedItem)) {
      return undefined;
    }

    const timer = window.setInterval(() => {
      if (selectedItem.kind === 'static') {
        void loadStaticRunDetails(selectedItem.id, true);
      } else {
        void loadDynamicRunDetails(selectedItem.id, true);
      }
      void loadRuns(true);
    }, selectedItem.kind === 'static' ? 1200 : 1000);

    return () => window.clearInterval(timer);
  }, [loadDynamicRunDetails, loadRuns, loadStaticRunDetails, open, selectedItem]);

  const renderRunList = () => (
    <div style={{ minWidth: 0 }}>
      <Input
        placeholder="Filter runs"
        prefix={<SearchOutlined />}
        allowClear
        value={filterText}
        onChange={(event) => setFilterText(event.target.value)}
      />

      <List
        size="small"
        loading={loading}
        dataSource={filteredRunItems}
        locale={{ emptyText: <Empty description="No runs found" /> }}
        style={{ marginTop: 16, maxHeight: 'calc(100vh - 170px)', overflow: 'auto' }}
        renderItem={(item) => {
          const isSelected = runKey(item) === selectedRunKey;
          const isStatic = item.kind === 'static';
          const title = isStatic
            ? appRunName(item.run)
            : `Dynamic ${shortId(item.id)}`;
          const detail = isStatic
            ? `${completedCount(item.run.task_counts)}/${item.run.task_counts?.total || 0} completed`
            : `${item.run.task_counts?.total || 0} task(s), ${item.run.event_count || 0} event(s)`;

          return (
            <List.Item
              onClick={() => selectRun(item)}
              style={{
                cursor: 'pointer',
                padding: '10px 8px',
                background: isSelected ? '#f0f7ff' : undefined,
                borderRadius: 6,
              }}
            >
              <Space direction="vertical" size={2} style={{ width: '100%' }}>
                <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                  <Text strong style={{ maxWidth: 176 }} ellipsis>
                    {title}
                  </Text>
                  <Tag color={itemStatusColor(item)}>{item.status}</Tag>
                </Space>
                <Space size={4} wrap>
                  <Tag color={isStatic ? 'blue' : 'purple'}>
                    {isStatic ? (isAppRun(item.run) ? 'app' : 'workflow') : 'dynamic'}
                  </Tag>
                  {!isStatic && runModeTag(item.run)}
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {shortId(item.id)}
                  </Text>
                </Space>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {formatTime(item.createdTime)}
                </Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {detail}
                </Text>
              </Space>
            </List.Item>
          );
        }}
      />
    </div>
  );

  const artifactDownloadUrl = (artifact: RunArtifact) => {
    if (artifact.sha256) {
      return api.getArtifactDownloadUrl(artifact.sha256);
    }

    const taskId = artifact.task_id || artifact.producer_task_id;
    if (selectedStaticRun && taskId && artifact.path) {
      return api.getStaticRunArtifactDownloadUrl(
        selectedStaticRun.run_id,
        taskId,
        artifact.path,
        selectedStaticRun.workspace_dir || workspaceDir || undefined,
      );
    }
    return null;
  };

  const openArtifactPreview = async (artifact: RunArtifact) => {
    const href = artifactDownloadUrl(artifact);
    if (!href) {
      message.warning('This artifact does not have a previewable download URL');
      return;
    }

    if (artifactLooksImage(artifact)) {
      setArtifactPreview({ artifact, href, loading: false });
      return;
    }

    setArtifactPreview({ artifact, href, loading: true });
    try {
      const response = await fetch(href);
      if (!response.ok) {
        throw new Error(`Preview request failed with HTTP ${response.status}`);
      }
      const rawContent = await response.text();
      const maxPreviewChars = 240000;
      const formatted = formatArtifactPreview(
        artifact,
        rawContent.length > maxPreviewChars
          ? `${rawContent.slice(0, maxPreviewChars)}\n\n... preview truncated ...`
          : rawContent,
      );
      setArtifactPreview({ artifact, href, content: formatted, loading: false });
    } catch (error: any) {
      console.error('Failed to preview artifact:', error);
      setArtifactPreview({
        artifact,
        href,
        error: error?.message || 'Failed to preview artifact',
        loading: false,
      });
    }
  };

  const artifactKey = (artifact: RunArtifact) => (
    artifact.sha256 || artifact.uri || `${artifact.run_id || selectedStaticRun?.run_id || ''}:${artifact.task_id || artifact.producer_task_id || ''}:${artifact.path || artifact.name || ''}`
  );

  const promoteArtifact = async (artifact: RunArtifact) => {
    const activeWorkspaceDir = selectedStaticRun?.workspace_dir || workspaceDir;
    if (!activeWorkspaceDir) {
      message.warning('Workspace is not available for this run');
      return;
    }

    const taskId = artifact.task_id || artifact.producer_task_id;
    const key = artifactKey(artifact);
    setPromotingArtifactKey(key);
    try {
      const result = await api.promoteArtifactToWorkspaceFile({
        workspaceId: workspaceId || undefined,
        workspaceDir: activeWorkspaceDir,
        artifact,
        targetPath: artifact.path || artifact.name || artifact.sha256,
        runId: artifact.run_id || selectedStaticRun?.run_id || selectedDynamicRun?.run_id,
        taskId,
        overwrite: true,
      });
      message.success(`Promoted to Workspace Files: ${result.file?.relativePath || artifact.path || artifact.name}`);
    } catch (error: any) {
      console.error('Failed to promote artifact:', error);
      message.error(error.response?.data?.error || 'Failed to promote artifact');
    } finally {
      setPromotingArtifactKey(null);
    }
  };

  const renderRunLogs = () => (
    <div>
      <Title level={5}>Logs</Title>
      {selectedRunLogs.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No logs recorded" />
      ) : (
        <pre
          style={{
            margin: 0,
            maxHeight: 320,
            overflow: 'auto',
            padding: 12,
            border: '1px solid #f0f0f0',
            borderRadius: 6,
            background: '#0f172a',
            color: '#e2e8f0',
            fontSize: 12,
            lineHeight: 1.5,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {selectedRunLogs.map((line, index) => {
            const stream = line.stream || 'log';
            const task = line.task_id ? shortId(line.task_id) : '-';
            return `[${stream} ${task}] ${line.message || ''}${index === selectedRunLogs.length - 1 ? '' : '\n'}`;
          })}
        </pre>
      )}
    </div>
  );

  const renderArtifacts = () => (
    <div>
      <Title level={5}>Artifacts</Title>
      {selectedRunArtifacts.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No artifacts recorded" />
      ) : (
        <List
          size="small"
          bordered
          dataSource={selectedRunArtifacts}
          renderItem={(artifact) => {
            const href = artifactDownloadUrl(artifact);
            const taskId = artifact.task_id || artifact.producer_task_id;
            const name = artifact.path || artifact.name || artifact.sha256 || 'artifact';

            return (
              <List.Item
                actions={[
                  <Button
                    key="preview"
                    size="small"
                    icon={<EyeOutlined />}
                    disabled={!href || !canPreviewArtifact(artifact)}
                    onClick={() => openArtifactPreview(artifact)}
                  >
                    Preview
                  </Button>,
                  <Button
                    key="promote"
                    size="small"
                    icon={<InboxOutlined />}
                    loading={promotingArtifactKey === artifactKey(artifact)}
                    disabled={!artifact.path && !artifact.name && !artifact.sha256}
                    onClick={() => promoteArtifact(artifact)}
                  >
                    Promote
                  </Button>,
                  <Button
                    key="download"
                    size="small"
                    icon={<DownloadOutlined />}
                    href={href || undefined}
                    target={href ? '_blank' : undefined}
                    disabled={!href}
                  >
                    Download
                  </Button>,
                ]}
              >
                <Space direction="vertical" size={2} style={{ width: '100%' }}>
                  <Space wrap>
                    <Text strong copyable={{ text: name }}>{name}</Text>
                    {taskId && <Tag>{shortId(taskId)}</Tag>}
                    {artifact.run_id && <Tag color="purple">{shortId(artifact.run_id)}</Tag>}
                    {artifact.sha256 && <Tag color="geekblue">CAS</Tag>}
                    {artifact.mime && <Tag>{artifact.mime}</Tag>}
                  </Space>
                  <Space size={8} wrap>
                    <Text type="secondary" style={{ fontSize: 12 }}>{formatBytes(artifact.size)}</Text>
                    {artifact.created_time && (
                      <Text type="secondary" style={{ fontSize: 12 }}>{formatTime(artifact.created_time)}</Text>
                    )}
                    {artifact.sha256 && (
                      <Text copyable={{ text: artifact.sha256 }} type="secondary" style={{ fontSize: 12 }}>
                        sha256 {shortId(artifact.sha256)}
                      </Text>
                    )}
                    {artifact.uri && (
                      <Text type="secondary" style={{ fontSize: 12 }}>{artifact.uri}</Text>
                    )}
                  </Space>
                </Space>
              </List.Item>
            );
          }}
        />
      )}
    </div>
  );

  const renderLoadedSkills = () => {
    if (selectedLoadedSkills.length === 0) {
      return null;
    }

    return (
      <div>
        <Title level={5}>Skills</Title>
        <List
          size="small"
          bordered
          dataSource={selectedLoadedSkills}
          renderItem={(skill) => (
            <List.Item>
              <Space direction="vertical" size={2} style={{ width: '100%' }}>
                <Space wrap>
                  <Text strong>{skill.name}</Text>
                  {skill.source && <Tag>{skill.source}</Tag>}
                  {skill.truncated && <Tag color="orange">truncated</Tag>}
                  {skill.resources && skill.resources.length > 0 && (
                    <Tag color="geekblue">{skill.resources.length} resource(s)</Tag>
                  )}
                </Space>
                {skill.description && (
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {skill.description}
                  </Text>
                )}
                {skill.path && (
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {skill.path}
                  </Text>
                )}
              </Space>
            </List.Item>
          )}
        />
      </div>
    );
  };

  const renderRuntimeEvidence = ({
    title,
    run,
    nodes,
    events = [],
    skills = [],
  }: {
    title: string;
    run?: StaticWorkflowRunSnapshot | DynamicRunSnapshot | null;
    nodes: any[];
    events?: DynamicRunEvent[];
    skills?: Array<WorkspaceSkillMeta & { source?: string }>;
  }) => {
    const placements = collectPlacementSummary(nodes);
    const resources = collectResourceSummary(nodes);
    const sandboxes = collectSandboxSummary(nodes, events, run as DynamicRunSnapshot);
    const tools = collectToolSummary(events);
    const errors = collectRunErrors(run, nodes, events);
    const repairGuidance = collectRepairGuidance(run, nodes, events);
    const finalResult = (run as any)?.final_result || {};
    const artifactFiles = Array.isArray(finalResult?.artifacts?.files) ? finalResult.artifacts.files : [];
    const totalArtifacts = Math.max(
      selectedRunArtifacts.length,
      Number(finalResult?.artifacts?.count || 0),
      artifactFiles.length,
    );
    const timing = finalResult?.timings;

    return (
      <div>
        <Title level={5}>{title}</Title>
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))',
            gap: 10,
          }}
        >
          <div style={{ border: '1px solid #f0f0f0', borderRadius: 6, padding: 10, background: '#fff' }}>
            <Text strong>Placement</Text>
            <Space size={[4, 4]} wrap style={{ display: 'flex', marginTop: 8 }}>
              {placements.length === 0 ? (
                <Tag>not scheduled yet</Tag>
              ) : placements.map((placement) => (
                <Tag key={placement.nodeIp || placement.nodeId || 'node'} color="geekblue">
                  {placement.nodeIp || shortId(placement.nodeId || '')}
                  {placement.gpuIds.size > 0 ? ` GPU ${Array.from(placement.gpuIds).join(',')}` : ''}
                  {` x${placement.count}`}
                </Tag>
              ))}
            </Space>
          </div>

          <div style={{ border: '1px solid #f0f0f0', borderRadius: 6, padding: 10, background: '#fff' }}>
            <Text strong>Sandbox & Resources</Text>
            <Space size={[4, 4]} wrap style={{ display: 'flex', marginTop: 8 }}>
              {sandboxes.length === 0 ? <Tag>default</Tag> : sandboxes.map((sandbox) => (
                <Tag key={sandbox} color="volcano">{sandbox}</Tag>
              ))}
              {resources.length === 0 ? <Tag>no explicit resources</Tag> : resources.slice(0, 4).map((resource) => (
                <Tag key={resource} color="gold">{resource}</Tag>
              ))}
            </Space>
          </div>

          <div style={{ border: '1px solid #f0f0f0', borderRadius: 6, padding: 10, background: '#fff' }}>
            <Text strong>Skills & Tools</Text>
            <Space size={[4, 4]} wrap style={{ display: 'flex', marginTop: 8 }}>
              {skills.length === 0 ? <Tag>no skills</Tag> : skills.map((skill) => (
                <Tag key={skill.name} color="purple">{skill.name}</Tag>
              ))}
              {tools.length === 0 ? <Tag>no tool calls</Tag> : tools.map((tool) => (
                <Tag key={tool.tool} color={tool.failed ? 'red' : 'blue'}>
                  {tool.tool} x{tool.count || 1}
                </Tag>
              ))}
            </Space>
          </div>

          <div style={{ border: '1px solid #f0f0f0', borderRadius: 6, padding: 10, background: '#fff' }}>
            <Text strong>Artifacts & Timing</Text>
            <Space size={[4, 4]} wrap style={{ display: 'flex', marginTop: 8 }}>
              <Tag color={totalArtifacts > 0 ? 'green' : 'default'}>{totalArtifacts} artifact(s)</Tag>
              {timing?.total_seconds !== undefined && (
                <Tag>{formatDurationSeconds(timing.total_seconds)}</Tag>
              )}
              {timing?.llm_seconds !== undefined && (
                <Tag color="cyan">LLM {formatDurationSeconds(timing.llm_seconds)}</Tag>
              )}
              {timing?.tool_seconds !== undefined && (
                <Tag color="green">Tools {formatDurationSeconds(timing.tool_seconds)}</Tag>
              )}
            </Space>
          </div>
        </div>

        {errors.length > 0 && (
          <Alert
            style={{ marginTop: 10 }}
            type="error"
            showIcon
            message="Run Issues"
            description={(
              <Space direction="vertical" size={2}>
                {errors.map((error) => (
                  <Text key={error} type="danger" style={{ fontSize: 12 }}>{error}</Text>
                ))}
              </Space>
            )}
          />
        )}

        {repairGuidance.length > 0 && (
          <Alert
            style={{ marginTop: 10 }}
            type="warning"
            showIcon
            message="Repair Guidance"
            description={(
              <List
                size="small"
                dataSource={repairGuidance}
                renderItem={(item) => (
                  <List.Item style={{ paddingLeft: 0, paddingRight: 0 }}>
                    <Space direction="vertical" size={2} style={{ width: '100%' }}>
                      <Space wrap>
                        <Tag color="orange">{item.stage}</Tag>
                        <Text type="secondary" style={{ fontSize: 12 }}>{item.issue}</Text>
                      </Space>
                      <Text style={{ fontSize: 12 }}>{item.suggestion}</Text>
                    </Space>
                  </List.Item>
                )}
              />
            )}
          />
        )}
      </div>
    );
  };

  const renderStaticDetails = () => {
    if (!selectedStaticRun) {
      return <Empty description="Select a workflow run" />;
    }

    return (
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Space style={{ justifyContent: 'space-between', width: '100%' }} align="start">
          <div>
            <Title level={4} style={{ margin: 0 }}>
              {appRunName(selectedStaticRun)}
            </Title>
            <Space size={6} wrap>
              <Tag color={isAppRun(selectedStaticRun) ? 'cyan' : 'blue'}>
                {isAppRun(selectedStaticRun) ? 'app' : 'workflow'}
              </Tag>
              <Tag color={staticStatusColors[selectedStaticRun.status] || 'default'}>{selectedStaticRun.status}</Tag>
              <Text copyable style={{ fontSize: 12 }}>{selectedStaticRun.run_id}</Text>
            </Space>
          </div>
          <Space>
            <Button
              icon={<ReloadOutlined />}
              onClick={() => loadStaticRunDetails(selectedStaticRun.run_id)}
              loading={detailsLoading}
            >
              Refresh
            </Button>
            {!staticTerminalStatuses.has(selectedStaticRun.status) && (
              <Popconfirm
                title="Cancel this run?"
                onConfirm={cancelSelectedRun}
                okText="Cancel run"
                okButtonProps={{ danger: true }}
              >
                <Button
                  danger
                  icon={<StopOutlined />}
                  loading={runActionLoading}
                >
                  Cancel
                </Button>
              </Popconfirm>
            )}
            {isAppRun(selectedStaticRun) && (
              <Button
                icon={<PlayCircleOutlined />}
                onClick={retrySelectedRun}
                loading={runActionLoading}
              >
                Retry
              </Button>
            )}
            <Popconfirm
              title="Delete this workflow run?"
              disabled={!staticTerminalStatuses.has(selectedStaticRun.status)}
              onConfirm={deleteSelectedRun}
              okText="Delete"
              okButtonProps={{ danger: true }}
            >
              <Button
                danger
                icon={<DeleteOutlined />}
                disabled={!staticTerminalStatuses.has(selectedStaticRun.status)}
              >
                Delete
              </Button>
            </Popconfirm>
          </Space>
        </Space>

        <Descriptions bordered size="small" column={2}>
          <Descriptions.Item label="Status">
            <Tag color={staticStatusColors[selectedStaticRun.status] || 'default'}>{selectedStaticRun.status}</Tag>
          </Descriptions.Item>
          <Descriptions.Item label="Schema">v{selectedStaticRun.schema_version || 1}</Descriptions.Item>
          <Descriptions.Item label="Created">{formatTime(selectedStaticRun.created_time)}</Descriptions.Item>
          <Descriptions.Item label="Updated">{formatTime(selectedStaticRun.updated_time)}</Descriptions.Item>
          <Descriptions.Item label="Finished">{formatTime(selectedStaticRun.finished_time)}</Descriptions.Item>
          <Descriptions.Item label="Maze Run">{selectedStaticRun.maze_run_id ? shortId(selectedStaticRun.maze_run_id) : '-'}</Descriptions.Item>
          <Descriptions.Item label="Workspace" span={2}>{selectedStaticRun.workspace_dir || '-'}</Descriptions.Item>
          <Descriptions.Item label="Error" span={2}>
            {renderJsonValue(selectedStaticRun.error || selectedStaticRun.error_summary)}
          </Descriptions.Item>
        </Descriptions>

        <Space wrap>
          {['total', 'pending', 'running', 'completed', 'failed'].map((key) => (
            <div
              key={key}
              style={{
                width: 120,
                border: '1px solid #f0f0f0',
                borderRadius: 6,
                padding: '8px 10px',
                background: '#fff',
              }}
            >
              <Statistic
                title={key}
                value={key === 'completed' ? completedCount(selectedStaticRun.task_counts) : selectedStaticRun.task_counts?.[key] || 0}
              />
            </div>
          ))}
        </Space>

        {renderRuntimeEvidence({
          title: 'Run Evidence',
          run: selectedStaticRun,
          nodes: selectedStaticTaskNodes,
        })}

        {renderRunLogs()}

        {renderArtifacts()}

        <div>
          <Title level={5}>Task Nodes</Title>
          {selectedStaticTaskNodes.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No task nodes recorded" />
          ) : (
            <List
              size="small"
              bordered
              dataSource={selectedStaticTaskNodes}
              renderItem={(node) => {
                const rejectSummaries = scheduleRejectSummary(node.schedule_decision);
                return (
                  <List.Item>
                    <Space direction="vertical" size={2} style={{ width: '100%' }}>
                      <Space wrap>
                        <Text strong>{node.label || node.task_name || node.node_id}</Text>
                        <Tag color={staticStatusColors[node.status as StaticWorkflowRunStatus] || 'default'}>{node.status}</Tag>
                        {node.category && <Tag>{node.category}</Tag>}
                        {node.maze_task_id && <Tag>{shortId(node.maze_task_id)}</Tag>}
                        {node.node_ip && <Tag color="geekblue">{node.node_ip}</Tag>}
                        {node.gpu_id !== undefined && node.gpu_id !== null && <Tag color="gold">GPU {node.gpu_id}</Tag>}
                        {formatResources(node.resources) && <Tag color="gold">{formatResources(node.resources)}</Tag>}
                        {nodeDuration(node) !== null && <Tag>{formatDurationSeconds(nodeDuration(node))}</Tag>}
                        {node.timeout_seconds !== undefined && node.timeout_seconds !== null && (
                          <Tag color="volcano">timeout {formatDurationSeconds(node.timeout_seconds)}</Tag>
                        )}
                        {node.retry_wait_seconds ? (
                          <Tag color="orange">retry in {formatDurationSeconds(node.retry_wait_seconds)}</Tag>
                        ) : null}
                      </Space>
                      {(node.started_time || node.finished_time) && (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          Started {formatTime(node.started_time)} / Finished {formatTime(node.finished_time)}
                        </Text>
                      )}
                      {node.pending_reason && (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          Pending: {node.pending_reason}
                        </Text>
                      )}
                      {node.next_eligible_time && (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          Next attempt: {formatTime(node.next_eligible_time)}
                        </Text>
                      )}
                      {node.schedule_decision?.reason && (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          Schedule: {node.schedule_decision.reason}
                        </Text>
                      )}
                      {rejectSummaries.map((summary) => (
                        <Text key={summary} type="secondary" style={{ fontSize: 12 }}>
                          {summary}
                        </Text>
                      ))}
                      {(node.error || node.last_error) && (
                        <Alert
                          type="error"
                          showIcon
                          message={errorSummary(node.error || node.last_error)}
                          description={renderJsonValue(node.error || node.last_error)}
                        />
                      )}
                      {node.artifacts && node.artifacts.length > 0 && (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {node.artifacts.length} artifact(s)
                        </Text>
                      )}
                    </Space>
                  </List.Item>
                );
              }}
            />
          )}
        </div>

        <div>
          <Title level={5}>Event Log</Title>
          {staticEvents.length === 0 ? (
            <Alert type="info" showIcon message="No events recorded for this run" />
          ) : (
            <List
              size="small"
              bordered
              dataSource={staticEvents}
              renderItem={(event) => (
                <List.Item>
                  <Space direction="vertical" size={2} style={{ width: '100%' }}>
                    <Space wrap>
                      <Text type="secondary">#{event.seq || '-'}</Text>
                      <Tag>{event.type}</Tag>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {event.timestamp ? new Date(event.timestamp).toLocaleString() : '-'}
                      </Text>
                    </Space>
                    <Text>{staticEventSummary(event)}</Text>
                  </Space>
                </List.Item>
              )}
            />
          )}
        </div>
      </Space>
    );
  };

  const renderDynamicDetails = () => {
    if (!selectedDynamicRun) {
      return <Empty description="Select a dynamic run" />;
    }

    const metadata = selectedDynamicRun.metadata || {};
    const mcpProfile = metadata.mcp_profile || {};
    const metadataMcpServers = Array.isArray(metadata.mcp_servers) ? metadata.mcp_servers : [];
    const metadataMcpTools = Array.isArray(metadata.mcp_tools) ? metadata.mcp_tools : [];
    const mcpProfileName = metadata.mcp_profile_name || mcpProfile.name;
    const mcpServersForDisplay = metadataMcpServers.length > 0 ? metadataMcpServers : (agentTrace.mcp?.servers || []);
    const mcpToolsForDisplay = metadataMcpTools.length > 0 ? metadataMcpTools : (agentTrace.mcp?.tools || []);
    const mcpProfileError = agentTrace.mcp?.discoveryError
      || agentTrace.mcp?.toolErrors?.[agentTrace.mcp.toolErrors.length - 1]?.error
      || agentTrace.mcp?.toolErrors?.[agentTrace.mcp.toolErrors.length - 1]?.tool_result?.error;
    const showMcpProfilePanel = Boolean(
      mcpProfileName
      || mcpServersForDisplay.length
      || mcpToolsForDisplay.length
      || mcpProfileError,
    );

    return (
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Space style={{ justifyContent: 'space-between', width: '100%' }} align="start">
          <div>
            <Title level={4} style={{ margin: 0 }}>Dynamic Run {shortId(selectedDynamicRun.run_id)}</Title>
            <Space size={6} wrap>
              <Tag color="purple">dynamic</Tag>
              {runModeTag(selectedDynamicRun)}
              <Tag color={dynamicStatusColors[selectedDynamicRun.status] || 'default'}>{selectedDynamicRun.status}</Tag>
              <Text copyable style={{ fontSize: 12 }}>{selectedDynamicRun.run_id}</Text>
            </Space>
          </div>
          <Space>
            <Button
              icon={<ReloadOutlined />}
              onClick={() => loadDynamicRunDetails(selectedDynamicRun.run_id)}
              loading={detailsLoading}
            >
              Refresh
            </Button>
            {(getRunMode(selectedDynamicRun) === 'react' || showAgentTrace) && (
              <Button
                icon={<PlayCircleOutlined />}
                onClick={rerunSelectedDynamicReactRun}
                loading={runActionLoading}
                disabled={!agentTrace.started?.prompt}
              >
                Rerun
              </Button>
            )}
            {!dynamicTerminalStatuses.has(selectedDynamicRun.status) && (
              <Popconfirm
                title="Cancel this run?"
                onConfirm={cancelSelectedRun}
                okText="Cancel run"
                okButtonProps={{ danger: true }}
              >
                <Button
                  danger
                  icon={<StopOutlined />}
                  loading={runActionLoading}
                >
                  Cancel
                </Button>
              </Popconfirm>
            )}
            <Popconfirm
              title="Delete this dynamic run?"
              description="Only terminal runs can be deleted."
              disabled={!dynamicTerminalStatuses.has(selectedDynamicRun.status)}
              onConfirm={deleteSelectedRun}
              okText="Delete"
              okButtonProps={{ danger: true }}
            >
              <Button
                danger
                icon={<DeleteOutlined />}
                disabled={!dynamicTerminalStatuses.has(selectedDynamicRun.status)}
              >
                Delete
              </Button>
            </Popconfirm>
          </Space>
        </Space>

        <Descriptions bordered size="small" column={2}>
          <Descriptions.Item label="Status">
            <Tag color={dynamicStatusColors[selectedDynamicRun.status] || 'default'}>{selectedDynamicRun.status}</Tag>
          </Descriptions.Item>
          <Descriptions.Item label="Schema">v{selectedDynamicRun.schema_version || 1}</Descriptions.Item>
          <Descriptions.Item label="Created">{formatTime(selectedDynamicRun.created_time)}</Descriptions.Item>
          <Descriptions.Item label="Updated">{formatTime(selectedDynamicRun.updated_time)}</Descriptions.Item>
          <Descriptions.Item label="Finished">{formatTime(selectedDynamicRun.finished_time)}</Descriptions.Item>
          <Descriptions.Item label="Timeout">{selectedDynamicRun.timeout_seconds ?? '-'}</Descriptions.Item>
          <Descriptions.Item label="Stop Reason">{selectedDynamicRun.final_result?.stop_reason || '-'}</Descriptions.Item>
          <Descriptions.Item label="Cancel Reason">{selectedDynamicRun.cancel_reason || '-'}</Descriptions.Item>
          <Descriptions.Item label="Failure Reason">
            {renderJsonValue(selectedDynamicRun.failure_reason)}
          </Descriptions.Item>
          <Descriptions.Item label="Timing" span={2}>
            {selectedDynamicRun.final_result?.timings
              ? formatJson(selectedDynamicRun.final_result.timings)
              : '-'}
          </Descriptions.Item>
        </Descriptions>

        <Space wrap>
          {['total', 'pending', 'submitted', 'running', 'completed', 'failed'].map((key) => (
            <div
              key={key}
              style={{
                width: 110,
                border: '1px solid #f0f0f0',
                borderRadius: 6,
                padding: '8px 10px',
                background: '#fff',
              }}
            >
              <Statistic title={key} value={selectedDynamicRun.task_counts?.[key] || 0} />
            </div>
          ))}
        </Space>

        {renderRuntimeEvidence({
          title: 'Run Evidence',
          run: selectedDynamicRun,
          nodes: selectedDynamicTaskNodes,
          events: dynamicEvents,
          skills: selectedLoadedSkills,
        })}

        {showMcpProfilePanel && (
          <div>
            <Title level={5}>MCP Profile</Title>
            <Space wrap style={{ marginBottom: 8 }}>
              {mcpProfileName && <Tag color="purple">profile {String(mcpProfileName)}</Tag>}
              <Tag color="magenta">{mcpServersForDisplay.length} server(s)</Tag>
              <Tag color="blue">{mcpToolsForDisplay.length} discovered tool(s)</Tag>
              {mcpProfile.lastTest?.status && (
                <Tag color={mcpProfile.lastTest.status === 'ok' ? 'green' : 'red'}>
                  last test {String(mcpProfile.lastTest.status)}
                </Tag>
              )}
              {mcpProfile.lastTest?.testedAt && (
                <Tag>{formatIsoTime(mcpProfile.lastTest.testedAt)}</Tag>
              )}
              {mcpProfile.usesEnvRefs && <Tag color="gold">{mcpProfile.envRefCount || 0} env ref(s)</Tag>}
            </Space>
            <Descriptions bordered size="small" column={2}>
              <Descriptions.Item label="Using Profile" span={2}>
                {mcpProfileName ? String(mcpProfileName) : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="Description" span={2}>
                {mcpProfile.description || '-'}
              </Descriptions.Item>
              <Descriptions.Item label="Servers" span={2}>
                {mcpServersForDisplay.length > 0 ? (
                  <Space wrap>
                    {mcpServersForDisplay.map((server: any, index: number) => (
                      <Tag key={`mcp-profile-server-${index}`} color="magenta">
                        {server.name || 'mcp'} {server.transport || ''}
                        {server.has_env ? ' env' : ''}
                        {server.has_headers ? ' headers' : ''}
                      </Tag>
                    ))}
                  </Space>
                ) : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="Discovered Tools" span={2}>
                {mcpToolsForDisplay.length > 0 ? (
                  <Space wrap>
                    {mcpToolsForDisplay.map((tool: any, index: number) => (
                      <Tag key={`mcp-profile-tool-${index}`} color="blue">
                        {tool.agent_tool || tool.tool || 'tool'}
                      </Tag>
                    ))}
                  </Space>
                ) : '-'}
              </Descriptions.Item>
            </Descriptions>
            {mcpProfileError && (
              <Alert
                style={{ marginTop: 8 }}
                type="error"
                showIcon
                message={mcpProfileName ? `MCP profile "${mcpProfileName}" error` : 'MCP error'}
                description={compactText(mcpProfileError, 300)}
              />
            )}
          </div>
        )}

        {renderLoadedSkills()}

        {renderRunLogs()}

        {renderArtifacts()}

        {showAgentTrace && (
          <div>
            <Title level={5}>ReAct Runtime Canvas</Title>
            <ReActRuntimeCanvas trace={agentTrace} run={selectedDynamicRun} />

            <Divider style={{ margin: '16px 0 8px' }} />

            <Title level={5}>Agent Trace</Title>
            {agentTrace.started && (
              <Space wrap style={{ marginBottom: 8 }}>
                {agentTrace.started.mode && <Tag color="purple">{String(agentTrace.started.mode)}</Tag>}
                {Array.isArray(agentTrace.started.tools) && (
                  <Tag color="geekblue">{agentTrace.started.tools.length} tool(s)</Tag>
                )}
                {agentTrace.mcp?.servers?.length > 0 && (
                  <Tag color="magenta">MCP servers {agentTrace.mcp.servers.length}</Tag>
                )}
                {agentTrace.mcp?.tools?.length > 0 && (
                  <Tag color="blue">MCP tools {agentTrace.mcp.tools.length}</Tag>
                )}
                {Object.keys(agentTrace.mcp?.calls || {}).length > 0 && (
                  <Tag color="purple">MCP calls {Object.values(agentTrace.mcp.calls).reduce((sum: number, count: any) => sum + Number(count || 0), 0)}</Tag>
                )}
                {agentTrace.mcp?.discoveryError && (
                  <Tag color="red">MCP discovery failed</Tag>
                )}
                {agentTrace.mcp?.toolErrors?.length > 0 && (
                  <Tag color="red">MCP errors {agentTrace.mcp.toolErrors.length}</Tag>
                )}
                {agentTrace.started.llm_task && (
                  <Tag color="cyan">LLM {String(agentTrace.started.llm_task)}</Tag>
                )}
                {agentTrace.permissions?.agent_permission_checked !== undefined && (
                  <Tag color="green">permissions {agentTrace.permissions.agent_permission_checked}</Tag>
                )}
                {agentTrace.permissions?.agent_permission_denied !== undefined && (
                  <Tag color="red">denied {agentTrace.permissions.agent_permission_denied}</Tag>
                )}
                {agentTrace.permissions?.agent_permission_ask !== undefined && (
                  <Tag color="orange">ask {agentTrace.permissions.agent_permission_ask}</Tag>
                )}
              </Space>
            )}

            {(agentTrace.mcp?.servers?.length > 0 || agentTrace.mcp?.tools?.length > 0 || agentTrace.mcp?.discoveryError || agentTrace.mcp?.callEvents?.length > 0 || agentTrace.mcp?.toolErrors?.length > 0) && (
              <div style={{ marginBottom: 12 }}>
                <Text type="secondary">MCP</Text>
                <Space wrap style={{ marginTop: 6, display: 'flex' }}>
                  {(agentTrace.mcp.servers || []).map((server: any, index: number) => (
                    <Tag key={`mcp-server-${index}`} color="magenta">
                      {server.name || 'mcp'} {server.transport || ''}
                    </Tag>
                  ))}
                  {(agentTrace.mcp.tools || []).map((tool: any, index: number) => (
                    <Tag key={`mcp-tool-${index}`} color="blue">
                      {tool.agent_tool || tool.tool || 'tool'}
                    </Tag>
                  ))}
                  {agentTrace.mcp.discoveryError && (
                    <Text type="danger">{compactText(agentTrace.mcp.discoveryError, 180)}</Text>
                  )}
                </Space>
                {agentTrace.mcp.callEvents?.length > 0 && (
                  <List
                    size="small"
                    style={{ marginTop: 8 }}
                    dataSource={agentTrace.mcp.callEvents.slice(-6)}
                    renderItem={(event: any) => (
                      <List.Item>
                        <Space wrap>
                          <Tag color={event.status === 'finished' ? 'green' : 'purple'}>{event.status}</Tag>
                          <Tag>{event.server || 'mcp'}</Tag>
                          <Tag color="blue">{event.tool || event.mcp_tool || 'tool'}</Tag>
                          {event.timings?.tool_seconds !== undefined && <Tag>{event.timings.tool_seconds}s</Tag>}
                        </Space>
                      </List.Item>
                    )}
                  />
                )}
                {agentTrace.mcp.toolErrors?.length > 0 && (
                  <Alert
                    style={{ marginTop: 8 }}
                    type="error"
                    showIcon
                    message="MCP tool error"
                    description={compactText(agentTrace.mcp.toolErrors[agentTrace.mcp.toolErrors.length - 1]?.error || agentTrace.mcp.toolErrors[agentTrace.mcp.toolErrors.length - 1]?.tool_result?.error, 240)}
                  />
                )}
              </div>
            )}

            {agentTrace.permissionEvents?.length > 0 && (
              <div style={{ marginBottom: 12 }}>
                <Text type="secondary">Permissions</Text>
                <List
                  size="small"
                  style={{ marginTop: 6 }}
                  dataSource={agentTrace.permissionEvents.slice(-8)}
                  renderItem={(event: any) => {
                    const request = event.request || event;
                    const permission = event.permission || request.policy_decision || {};
                    const action = permission.action || '';
                    const requestStatus = request.status || '';
                    const pendingRequestId = requestStatus === 'pending'
                      ? (event.request_id || request.request_id)
                      : '';
                    const target = permission.target || request.target || '*';
                    const toolName = event.tool || request.tool || 'tool';
                    const permissionName = permission.permission || request.permission || 'permission';
                    const color = requestStatus === 'allowed' || permission.allowed
                      ? 'green'
                      : requestStatus === 'pending' || action === 'ask'
                        ? 'orange'
                        : 'red';

                    if (pendingRequestId) {
                      return (
                        <List.Item>
                          <PendingActionCard
                            title="Pending Action"
                            actionLabel="Permission"
                            actionColor="orange"
                            subject={`${toolName} requests ${permissionName}`}
                            description={`Target: ${target}`}
                            tags={[
                              <Tag color={color}>{event.type.replace('agent_permission_', '')}</Tag>,
                              <Tag>{toolName}</Tag>,
                              <Tag>{permissionName}</Tag>,
                              <Tag>{target}</Tag>,
                              requestStatus ? <Tag>{requestStatus}</Tag> : null,
                              permission.pattern ? <Tag>rule {permission.pattern}</Tag> : null,
                            ].filter(Boolean)}
                            error={permission.reason && action !== 'ask' ? permission.reason : undefined}
                            approveLabel="Allow"
                            denyLabel="Deny"
                            loading={runActionLoading}
                            onApprove={() => decidePermissionRequest(event, 'allow')}
                            onDeny={() => decidePermissionRequest(event, 'deny')}
                          />
                        </List.Item>
                      );
                    }

                    return (
                      <List.Item>
                        <Space wrap>
                          <Tag color={color}>{event.type.replace('agent_permission_', '')}</Tag>
                          <Tag>{toolName}</Tag>
                          <Tag>{permissionName}</Tag>
                          <Tag>{target}</Tag>
                          {requestStatus && <Tag>{requestStatus}</Tag>}
                          {permission.pattern && <Tag>rule {permission.pattern}</Tag>}
                          {permission.reason && <Text type="secondary">{permission.reason}</Text>}
                        </Space>
                      </List.Item>
                    );
                  }}
                />
              </div>
            )}

            {agentTrace.steps.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No agent steps recorded" />
            ) : (
              <List
                size="small"
                bordered
                dataSource={agentTrace.steps}
                renderItem={(step) => (
                  <List.Item>
                    <Space direction="vertical" size={6} style={{ width: '100%' }}>
                      <Space wrap>
                        <Text strong>Step {step.step}</Text>
                        {step.repair && <Tag color="orange">repair</Tag>}
                        {step.tool && <Tag color="blue">{step.tool}</Tag>}
                        {step.decisionTaskId && <Tag>LLM {shortId(step.decisionTaskId)}</Tag>}
                        {step.toolTaskId && <Tag>Tool {shortId(step.toolTaskId)}</Tag>}
                        {step.timings?.llm_seconds !== undefined && <Tag color="cyan">LLM {step.timings.llm_seconds}s</Tag>}
                        {step.timings?.tool_seconds !== undefined && <Tag color="green">Tool {step.timings.tool_seconds}s</Tag>}
                      </Space>
                      <Text type="secondary">Action</Text>
                      <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 12 }}>
                        {formatJson(step.action || step.decision)}
                      </pre>
                      {rawDecisionText(step) && (
                        <Collapse
                          ghost
                          size="small"
                          items={[
                            {
                              key: 'raw',
                              label: 'Raw LLM decision',
                              children: (
                                <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 12 }}>
                                  {rawDecisionText(step)}
                                </pre>
                              ),
                            },
                          ]}
                        />
                      )}
                      {step.observation !== undefined && (
                        <>
                          <Text type="secondary">Observation</Text>
                          <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 12 }}>
                            {formatJson(step.observation)}
                          </pre>
                        </>
                      )}
                    </Space>
                  </List.Item>
                )}
              />
            )}

            {agentTrace.final && (
              <Alert
                style={{ marginTop: 8 }}
                type="success"
                showIcon
                message="Final Answer"
                description={formatJson(agentTrace.final.answer)}
              />
            )}
            {agentTrace.error && (
              <Alert
                style={{ marginTop: 8 }}
                type="error"
                showIcon
                message="Agent Error"
                description={String(agentTrace.error.error || 'Unknown error')}
              />
            )}
          </div>
        )}

        <div>
          <Title level={5}>Task Graph</Title>
          {selectedDynamicTaskNodes.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No appended tasks" />
          ) : (
            <List
              size="small"
              bordered
              dataSource={selectedDynamicTaskNodes}
              renderItem={(node: any) => {
                const parents = node.parents || [];
                const selectedNode = node.selected_node || {};
                const manifestArtifacts = node.file_manifest?.files || [];
                const rejectSummaries = scheduleRejectSummary(node.schedule_decision);
                const resultSummary = node.result_summary || node.result || {};
                const sandboxBackend = node.task_name === 'exec_code'
                  ? (resultSummary.metadata?.sandbox_backend || resultSummary.backend)
                  : null;
                const generatedFiles = Array.isArray(resultSummary.generated_files)
                  ? resultSummary.generated_files
                  : [];
                return (
                  <List.Item>
                    <Space direction="vertical" size={2} style={{ width: '100%' }}>
                      <Space wrap>
                        <Text strong>{node.task_name || 'task'}</Text>
                        <Tag>{shortId(node.task_id)}</Tag>
                        <Tag color="blue">{node.status}</Tag>
                        {node.request_id && <Tag color="cyan">{node.request_id}</Tag>}
                        {selectedNode.node_ip && <Tag color="geekblue">{selectedNode.node_ip}</Tag>}
                        {selectedNode.gpu_id !== undefined && selectedNode.gpu_id !== null && <Tag color="gold">GPU {selectedNode.gpu_id}</Tag>}
                        {sandboxBackend && <Tag color="volcano">sandbox {String(sandboxBackend)}</Tag>}
                        {formatResources(node.resources) && <Tag color="gold">{formatResources(node.resources)}</Tag>}
                        {nodeDuration(node) !== null && <Tag>{formatDurationSeconds(nodeDuration(node))}</Tag>}
                        {node.timeout_seconds !== undefined && node.timeout_seconds !== null && (
                          <Tag color="volcano">timeout {formatDurationSeconds(node.timeout_seconds)}</Tag>
                        )}
                        {node.retry_wait_seconds ? (
                          <Tag color="orange">retry in {formatDurationSeconds(node.retry_wait_seconds)}</Tag>
                        ) : null}
                      </Space>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        Parents: {parents.length ? parents.map(shortId).join(', ') : 'none'}
                      </Text>
                      {(node.started_time || node.start_time || node.finished_time || node.finish_time) && (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          Started {formatTime(node.started_time ?? node.start_time)} / Finished {formatTime(node.finished_time ?? node.finish_time)}
                        </Text>
                      )}
                      {node.pending_reason && (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          Pending: {node.pending_reason}
                        </Text>
                      )}
                      {node.next_eligible_time && (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          Next attempt: {formatTime(node.next_eligible_time)}
                        </Text>
                      )}
                      {node.schedule_decision?.reason && (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          Schedule: {node.schedule_decision.reason}
                        </Text>
                      )}
                      {rejectSummaries.map((summary) => (
                        <Text key={summary} type="secondary" style={{ fontSize: 12 }}>
                          {summary}
                        </Text>
                      ))}
                      {(node.error || node.last_error) && (
                        <Alert
                          type="error"
                          showIcon
                          message={errorSummary(node.error || node.last_error)}
                          description={renderJsonValue(node.error || node.last_error)}
                        />
                      )}
                      {manifestArtifacts.length > 0 && (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {manifestArtifacts.length} artifact(s)
                        </Text>
                      )}
                      {generatedFiles.length > 0 && (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          Generated: {generatedFiles.map((file: any) => file.path || file.name).filter(Boolean).join(', ')}
                        </Text>
                      )}
                    </Space>
                  </List.Item>
                );
              }}
            />
          )}
          {selectedDynamicEdges.length > 0 && (
            <Text type="secondary" style={{ display: 'block', marginTop: 8, fontSize: 12 }}>
              Edges: {selectedDynamicEdges.map((edge) => `${shortId(edge.source)} -> ${shortId(edge.target)}`).join(', ')}
            </Text>
          )}
        </div>

        <Divider style={{ margin: '4px 0' }} />

        <div>
          <Title level={5}>Event Log</Title>
          {dynamicEvents.length === 0 ? (
            <Alert type="info" showIcon message="No events recorded for this run" />
          ) : (
            <List
              size="small"
              bordered
              dataSource={dynamicEvents}
              renderItem={(event) => (
                <List.Item>
                  <Space direction="vertical" size={2} style={{ width: '100%' }}>
                    <Space wrap>
                      <Text type="secondary">#{event.seq || '-'}</Text>
                      <Tag>{event.type}</Tag>
                      {event.data?.run_status && <Tag color="default">{String(event.data.run_status)}</Tag>}
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {event.timestamp ? new Date(event.timestamp).toLocaleString() : '-'}
                      </Text>
                    </Space>
                    <Text>{dynamicEventSummary(event)}</Text>
                  </Space>
                </List.Item>
              )}
            />
          )}
        </div>
      </Space>
    );
  };

  const previewName = artifactPreview
    ? artifactPreview.artifact.path || artifactPreview.artifact.name || artifactPreview.artifact.sha256 || 'Artifact'
    : 'Artifact';

  return (
    <>
      <Drawer
        title={
          <Space>
            <HistoryOutlined />
            Runs
          </Space>
        }
        open={open}
        onClose={onClose}
        width={1040}
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={() => loadRuns()} loading={loading}>
              Refresh
            </Button>
            <Button onClick={runDryCleanup} loading={cleanupLoading}>
              Dynamic Dry Run
            </Button>
            <Button onClick={() => cleanupStaticRuns(true)} loading={staticCleanupLoading}>
              Static Dry Run
            </Button>
            <Button onClick={() => cleanupArtifacts(true)} loading={artifactCleanupLoading}>
              Artifact Dry Run
            </Button>
            <Popconfirm
              title="Delete old static runs?"
              description="Deletes terminal static runs older than 7 days while keeping the latest 100."
              okText="Delete"
              okButtonProps={{ danger: true }}
              onConfirm={() => cleanupStaticRuns(false)}
            >
              <Button danger icon={<DeleteOutlined />} loading={staticCleanupLoading}>
                Clean Static Runs
              </Button>
            </Popconfirm>
            <Popconfirm
              title="Delete orphan artifacts?"
              description="Deletes CAS artifacts older than 7 days that are not referenced by any service workspace run."
              okText="Delete"
              okButtonProps={{ danger: true }}
              onConfirm={() => cleanupArtifacts(false)}
            >
              <Button danger icon={<DeleteOutlined />} loading={artifactCleanupLoading}>
                Clean Artifacts
              </Button>
            </Popconfirm>
          </Space>
        }
      >
        <div style={{ display: 'grid', gridTemplateColumns: '320px minmax(0, 1fr)', gap: 20, height: '100%' }}>
          {renderRunList()}

          <div style={{ minWidth: 0, overflow: 'auto', paddingRight: 4 }}>
            {!selectedItem ? (
              <Empty description="Select a run" />
            ) : selectedItem.kind === 'static' ? (
              renderStaticDetails()
            ) : (
              renderDynamicDetails()
            )}
          </div>
        </div>
      </Drawer>

      <Modal
        open={Boolean(artifactPreview)}
        title={
          <Space direction="vertical" size={0}>
            <Text strong>{previewName}</Text>
            {artifactPreview?.href && (
              <Text copyable={{ text: artifactPreview.href }} type="secondary" style={{ fontSize: 12 }}>
                Preview URL
              </Text>
            )}
          </Space>
        }
        footer={[
          <Button key="close" onClick={() => setArtifactPreview(null)}>
            Close
          </Button>,
          <Button
            key="download"
            icon={<DownloadOutlined />}
            href={artifactPreview?.href}
            target="_blank"
            disabled={!artifactPreview?.href}
          >
            Download
          </Button>,
        ]}
        onCancel={() => setArtifactPreview(null)}
        width={860}
        destroyOnClose
      >
        {!artifactPreview ? null : artifactPreview.loading ? (
          <Alert type="info" showIcon message="Loading artifact preview..." />
        ) : artifactPreview.error ? (
          <Alert type="error" showIcon message="Preview failed" description={artifactPreview.error} />
        ) : artifactLooksImage(artifactPreview.artifact) ? (
          <div style={{ textAlign: 'center' }}>
            <img
              src={artifactPreview.href}
              alt={previewName}
              style={{ maxWidth: '100%', maxHeight: '70vh', objectFit: 'contain' }}
            />
          </div>
        ) : (
          <pre
            style={{
              margin: 0,
              maxHeight: '70vh',
              overflow: 'auto',
              padding: 12,
              border: '1px solid #f0f0f0',
              borderRadius: 6,
              background: '#fafafa',
              fontSize: 12,
              lineHeight: 1.5,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }}
          >
            {artifactPreview.content || ''}
          </pre>
        )}
      </Modal>
    </>
  );
}
