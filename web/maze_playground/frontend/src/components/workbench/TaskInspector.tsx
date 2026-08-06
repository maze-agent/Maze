import { useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { Button, Empty, Input, Popconfirm, Select, Space, Tag, Typography } from 'antd';
import {
  AppstoreOutlined,
  CopyOutlined,
  DeleteOutlined,
  EditOutlined,
  FileDoneOutlined,
  NodeIndexOutlined,
} from '@ant-design/icons';
import { api } from '@/api/client';
import { useWorkflowStore } from '@/stores/workflowStore';
import CustomTaskEditor from '@/components/CustomTaskEditor';
import { syncWorkflowInputEdges } from '@/utils/workflowBindings';
import {
  latestRunForWorkflow,
  runWorkflowGraph,
  runWorkflowName,
} from '@/utils/runSnapshot';
import type {
  FaultToleranceTrace,
  LocalModel,
  RunArtifact,
  UnifiedRunTaskSnapshot,
  WorkflowEdge,
  WorkflowNode,
} from '@/types/workflow';

const { Text } = Typography;

type InspectorTab = 'overview' | 'definition' | 'resources' | 'runtime' | 'artifacts';
type TaskState = 'created' | 'pending' | 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'draft' | 'validated';

type ArtifactItem = {
  id: string;
  name: string;
  type: 'file' | 'dataset' | 'model' | 'report' | 'log';
  size?: string;
  status: 'pending' | 'produced' | 'failed';
  uri?: string;
  sha256?: string;
  path?: string;
  createdAt?: string;
  producedBy?: string;
};

type RunTimingContext = {
  createdAt?: string;
  submittedAt?: string;
  startedAt?: string;
};

export type WorkbenchTask = {
  id: string;
  name: string;
  kind: 'cpu' | 'gpu' | 'io';
  state: TaskState;
  isDynamic?: boolean;
  description?: string;
  config: {
    functionName?: string;
    timeoutSeconds?: number;
    maxRetries?: number;
    retryBackoffSeconds?: number;
    localModel?: string;
  };
  resources: {
    cpuNum?: number;
    gpuMemoryGiB?: number;
    ioNum?: number;
  };
  dependencies: {
    upstream: string[];
    downstream: string[];
  };
  runtime: {
    createdAt?: string;
    queueTimeRecorded?: boolean;
    startedAt?: string;
    finishedAt?: string;
    duration?: string;
    queueTime?: string;
    attempt?: number;
    maxAttempts?: number;
    retries?: number;
    exitCode?: number | string;
    failureReason?: string;
    queueReason?: string;
    lastHeartbeat?: string;
    schedulingReason?: string;
  };
  faultTolerance?: FaultToleranceTrace;
  placement?: {
    worker?: string;
    node?: string;
    gpuDevice?: string | number;
    zone?: string;
    host?: string;
    address?: string;
    reason?: string;
    scheduledAt?: string;
  };
  queueInfo?: {
    reason?: string;
    required?: string;
    available?: string;
    queuedFor?: string;
    blockingTasks?: string[];
  };
  dynamicPatch?: {
    patchId: string;
    triggeredByTaskId?: string;
    reason?: string;
    appendedAt?: string;
    patchType?: 'append_task' | 'append_subdag' | 'append_edge';
  };
  tags?: Array<{ key: string; value: string }>;
  artifacts?: ArtifactItem[];
};

const tabs: Array<{ key: InspectorTab; label: string }> = [
  { key: 'overview', label: 'Overview' },
  { key: 'definition', label: 'Definition' },
  { key: 'resources', label: 'Resources' },
  { key: 'runtime', label: 'Runtime' },
  { key: 'artifacts', label: 'Artifacts' },
];

function normalizeState(status?: string | null): TaskState {
  if (!status) return 'draft';
  if (status === 'completed') return 'succeeded';
  if (status === 'canceled') return 'cancelled';
  if (status === 'timed_out' || status === 'interrupted') return 'failed';
  if (['created', 'pending', 'queued', 'running', 'succeeded', 'failed', 'cancelled', 'draft', 'validated'].includes(status)) {
    return status as TaskState;
  }
  return 'draft';
}

function statusColor(status?: string | null) {
  const normalized = normalizeState(status);
  if (['succeeded', 'validated'].includes(normalized)) return 'green';
  if (normalized === 'running') return 'blue';
  if (normalized === 'queued') return 'gold';
  if (normalized === 'failed') return 'red';
  return 'default';
}

function taskKind(node: WorkflowNode): WorkbenchTask['kind'] {
  const explicitKind = (node.data as any).task_kind || (node.data as any).taskKind;
  if (['cpu', 'gpu', 'io'].includes(explicitKind)) return explicitKind;
  const resources = node.data.resources;
  const label = `${node.data.label} ${node.data.taskRef || ''} ${node.data.functionName || ''} ${node.data.taskPath || ''}`.toLowerCase();
  if (
    resources?.gpu_mem
    || node.data.modelAnchor
    || node.data.localModel
    || label.includes('gpu')
    || label.includes('cuda')
    || label.includes('llm')
    || label.includes('model')
    || label.includes('inference')
  ) return 'gpu';
  if (label.includes('file') || label.includes('io') || label.includes('input') || label.includes('artifact')) return 'io';
  return 'cpu';
}

function taskTypeLabel(kind: WorkbenchTask['kind']) {
  if (kind === 'gpu') return 'GPU';
  if (kind === 'io') return 'I/O';
  return 'CPU';
}

function toEpochMilliseconds(value?: number | string | null) {
  if (!value) return undefined;
  if (typeof value === 'number') {
    return value < 1_000_000_000_000 ? value * 1000 : value;
  }
  const numeric = Number(value);
  if (Number.isFinite(numeric)) {
    return numeric < 1_000_000_000_000 ? numeric * 1000 : numeric;
  }
  const parsed = Date.parse(String(value));
  return Number.isNaN(parsed) ? undefined : parsed;
}

function formatTimestamp(value?: number | string | null) {
  const milliseconds = toEpochMilliseconds(value);
  if (milliseconds === undefined) return undefined;
  const date = new Date(milliseconds);
  if (Number.isNaN(date.getTime())) return String(value);
  const pad = (part: number, length = 2) => String(part).padStart(length, '0');
  return [
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`,
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`,
  ].join(' ');
}

function formatDuration(seconds?: number | null) {
  if (seconds === undefined || seconds === null) return undefined;
  const value = Math.max(0, Number(seconds));
  if (!Number.isFinite(value)) return undefined;
  if (value < 1) return `${Math.round(value * 1000)} ms`;
  if (value < 60) return `${value.toFixed(3).replace(/\.?0+$/, '')}s`;
  const total = Math.round(value);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remaining = total % 60;
  if (hours > 0) return `${hours}h ${minutes}m ${remaining}s`;
  return `${minutes}m ${remaining}s`;
}

function durationBetween(started?: number | string | null, finished?: number | string | null) {
  const startMs = toEpochMilliseconds(started);
  const finishMs = toEpochMilliseconds(finished);
  if (startMs === undefined || finishMs === undefined) return undefined;
  return formatDuration((finishMs - startMs) / 1000);
}

function taskStartedTime(task?: UnifiedRunTaskSnapshot | null) {
  return task?.started_time ?? task?.start_time;
}

function taskFinishedTime(task?: UnifiedRunTaskSnapshot | null) {
  return task?.finished_time ?? task?.finish_time;
}

function firstTaskStartedTime(taskNodes?: Record<string, UnifiedRunTaskSnapshot>) {
  const startedTimes = Object.values(taskNodes || {})
    .map((task) => toEpochMilliseconds(taskStartedTime(task)))
    .filter((value): value is number => value !== undefined);
  if (startedTimes.length === 0) return undefined;
  return Math.min(...startedTimes);
}

function formatFileSize(size?: number | null) {
  if (size === undefined || size === null) return undefined;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function formatSecondsLabel(seconds?: number | null) {
  if (seconds === undefined || seconds === null) return undefined;
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} hr`;
}

function formatStorageGiB(value?: number | null) {
  if (value === undefined || value === null || Number(value) === 0) return undefined;
  return `${value} GiB`;
}

function formatGiBFromMiB(value?: number | null) {
  if (!value) return undefined;
  return `${(value / 1024).toFixed(1)} GiB`;
}

function localModelLabel(model: LocalModel) {
  return [
    model.name,
    model.model_type,
    model.estimated_params_label,
    model.estimated_weight_memory || formatGiBFromMiB(model.estimated_gpu_mem_mb),
  ].filter(Boolean).join(' · ');
}

function modelAnchor(model: LocalModel) {
  return {
    local_model: model.id,
    model_scope: model.model_scope || 'head',
    backend: model.backend || 'transformers',
    estimated_weight_memory_bytes: model.estimated_weight_memory_bytes,
    estimated_gpu_mem_mb: model.estimated_gpu_mem_mb,
    estimated_params: model.estimated_params,
  };
}

function renderable(value: ReactNode) {
  return value !== undefined && value !== null && value !== '';
}

function errorMessage(value: any) {
  if (!value) return undefined;
  if (typeof value === 'string') return value;
  if (value.message) return String(value.message);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function compactJson(value: unknown) {
  if (value === undefined || value === null || value === '') return '-';
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function faultText(entry: any, key: 'failure' | 'diagnosis' | 'repair_action' | 'retry' | 'outcome') {
  const value = entry?.[key];
  if (!value) return '-';
  if (key === 'failure') {
    return [value.error_type, value.message].filter(Boolean).join(': ') || compactJson(value);
  }
  if (key === 'diagnosis') {
    return [value.category, value.reason, value.recoverable === false ? 'not recoverable' : null].filter(Boolean).join(' / ') || compactJson(value);
  }
  if (key === 'repair_action') {
    return [value.type, value.applied === false ? 'not applied' : null, value.reason].filter(Boolean).join(' / ') || compactJson(value);
  }
  if (key === 'retry') {
    return value.scheduled ? `scheduled${value.next_attempt ? ` -> attempt ${value.next_attempt}` : ''}` : 'not scheduled';
  }
  if (key === 'outcome') {
    return value.status || compactJson(value);
  }
  return compactJson(value);
}

function dependencyLabels(nodeId: string, edges: WorkflowEdge[], nodes: WorkflowNode[], direction: 'upstream' | 'downstream') {
  const ids = edges
    .filter((edge) => direction === 'upstream' ? edge.target === nodeId : edge.source === nodeId)
    .map((edge) => direction === 'upstream' ? edge.source : edge.target);
  return ids.map((id) => nodes.find((node) => node.id === id)?.data.label || id);
}

function unknownItems(value: unknown): string[] {
  if (value === undefined || value === null || value === '') return [];
  if (Array.isArray(value)) {
    return value
      .map((item) => {
        if (item === undefined || item === null || item === '') return '';
        if (typeof item === 'string' || typeof item === 'number' || typeof item === 'boolean') return String(item);
        if (typeof item === 'object') {
          const record = item as Record<string, unknown>;
          return String(record.name || record.key || record.id || record.path || record.relativePath || JSON.stringify(record));
        }
        return String(item);
      })
      .filter(Boolean);
  }
  if (typeof value === 'object') {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, entry]) => {
        if (entry === undefined || entry === null || entry === '') return key;
        if (typeof entry === 'object') return key;
        return `${key}: ${String(entry)}`;
      });
  }
  return [String(value)];
}

function resourceParts(task: WorkbenchTask) {
  return [
    task.resources.cpuNum ? `${task.resources.cpuNum} CPU` : null,
    task.resources.gpuMemoryGiB ? `${task.resources.gpuMemoryGiB} GiB GPU` : null,
    task.resources.ioNum ? `${task.resources.ioNum} I/O` : null,
  ].filter(Boolean) as string[];
}

function retryPolicyLabel(task: WorkbenchTask) {
  const maxRetries = task.config.maxRetries;
  const backoff = task.config.retryBackoffSeconds;
  if (maxRetries === undefined && backoff === undefined) return undefined;
  return [
    maxRetries === undefined ? null : `${maxRetries} retries`,
    backoff === undefined ? null : `${backoff}s backoff`,
  ].filter(Boolean).join(', ');
}

function artifactPath(artifact: any) {
  return artifact?.path || artifact?.relative_path || artifact?.name || artifact?.filename;
}

function shortArtifactId(value?: string) {
  if (!value) return '-';
  return value.length > 18 ? `${value.slice(0, 8)}...${value.slice(-4)}` : value;
}

function buildTask(
  node: WorkflowNode,
  runtime: UnifiedRunTaskSnapshot | null,
  artifactRecords: RunArtifact[],
  edges: WorkflowEdge[],
  nodes: WorkflowNode[],
): WorkbenchTask {
  const data = node.data as any;
  const kind = runtime?.task_kind || taskKind(node);
  const state = runtime?.status
    ? normalizeState(runtime.status)
    : node.data.configured ? 'validated' : 'draft';
  const resources = runtime?.resources || node.data.resources || {};
  const cpuNum = Number((resources as any).cpu_num ?? (resources as any).cpu ?? 1);
  const gpuMem = Number((resources as any).gpu_mem || 0);
  const ioNum = Number((resources as any).io_num || 0);
  const selectedNode = runtime?.selected_node || runtime?.schedule_decision?.selected_node;
  const queueReason = runtime?.pending_reason || runtime?.schedule_decision?.reason || undefined;
  const startedTime = taskStartedTime(runtime);
  const finishedTime = taskFinishedTime(runtime);
  const maxRetries = data.maxRetries;
  const seenArtifacts = new Set<string>();
  const realArtifacts = artifactRecords.flatMap((artifact, index) => {
    const pathValue = artifactPath(artifact);
    const key = artifact?.sha256 || artifact?.uri || artifact?.storage_path || pathValue || `artifact-${index}`;
    if (seenArtifacts.has(key)) return [];
    seenArtifacts.add(key);
    const taskId = artifact?.task_id
      || artifact?.producer_task_id
      || runtime?.task_id
      || node.id;

    return [{
      id: key,
      name: artifact?.name || pathValue?.split('/').pop() || pathValue || artifact?.sha256 || `artifact-${index}`,
      type: 'file' as const,
      size: formatFileSize(artifact?.size),
      status: 'produced' as const,
      uri: artifact?.uri || artifact?.storage_uri || artifact?.storage_path || pathValue,
      sha256: artifact?.sha256,
      path: pathValue,
      createdAt: formatTimestamp(artifact?.created_time || finishedTime),
      producedBy: taskId || node.id,
    }];
  });

  return {
    id: runtime?.task_id || node.id,
    name: runtime?.task_name || node.data.label,
    kind,
    state,
    isDynamic: Boolean(data.dynamic || data.runtimeAppended),
    description: data.description || data.summary || data.prompt,
    config: {
      functionName: node.data.functionName,
      timeoutSeconds: node.data.taskTimeout || runtime?.timeout_seconds || undefined,
      maxRetries,
      retryBackoffSeconds: data.retryBackoffSeconds,
      localModel: data.localModel,
    },
    resources: {
      cpuNum: cpuNum || undefined,
      gpuMemoryGiB: gpuMem ? Number((gpuMem / 1024).toFixed(2)) : undefined,
      ioNum: ioNum || undefined,
    },
    dependencies: {
      upstream: runtime?.parents
        ? runtime.parents.map((id) => nodes.find((item) => item.id === id)?.data.label || id)
        : dependencyLabels(node.id, edges, nodes, 'upstream'),
      downstream: runtime?.children
        ? runtime.children.map((id) => nodes.find((item) => item.id === id)?.data.label || id)
        : dependencyLabels(node.id, edges, nodes, 'downstream'),
    },
    runtime: {
      createdAt: formatTimestamp(runtime?.created_time),
      startedAt: formatTimestamp(startedTime),
      finishedAt: formatTimestamp(finishedTime),
      duration: formatDuration(runtime?.duration_seconds) || durationBetween(startedTime, finishedTime),
      queueTime: durationBetween(runtime?.created_time, startedTime) || formatDuration((runtime as any)?.queue_time_seconds),
      queueTimeRecorded: Boolean(runtime?.created_time || (runtime as any)?.queue_time_seconds),
      attempt: runtime?.attempt || undefined,
      maxAttempts: maxRetries === undefined ? undefined : Number(maxRetries) + 1,
      retries: runtime?.attempt ? Math.max(0, Number(runtime.attempt) - 1) : undefined,
      exitCode: (runtime as any)?.exit_code,
      failureReason: errorMessage(runtime?.error) || errorMessage(runtime?.last_error),
      queueReason,
      lastHeartbeat: formatTimestamp((runtime as any)?.last_heartbeat || (runtime as any)?.heartbeat_time),
      schedulingReason: runtime?.schedule_decision?.reason || undefined,
    },
    faultTolerance: runtime?.fault_tolerance,
    placement: {
      worker: selectedNode?.node_ip || selectedNode?.node_id || undefined,
      node: selectedNode?.node_id || undefined,
      gpuDevice: selectedNode?.gpu_id ?? undefined,
      zone: (runtime as any)?.zone,
      host: (runtime as any)?.host,
      address: selectedNode?.node_ip || undefined,
      reason: runtime?.schedule_decision?.reason || undefined,
      scheduledAt: formatTimestamp(startedTime),
    },
    queueInfo: state === 'queued' || state === 'pending'
      ? {
        reason: queueReason,
        required: resourceParts({
          resources: {
            cpuNum: cpuNum || undefined,
            gpuMemoryGiB: gpuMem ? Number((gpuMem / 1024).toFixed(2)) : undefined,
            ioNum: ioNum || undefined,
          },
        } as WorkbenchTask).join(' / ') || undefined,
        available: runtime?.schedule_decision?.candidate_nodes?.[0]?.available_resources
          ? JSON.stringify(runtime.schedule_decision.candidate_nodes[0].available_resources)
          : undefined,
        queuedFor: durationBetween(runtime?.created_time, startedTime),
        blockingTasks: (runtime as any)?.blocking_tasks,
      }
      : undefined,
    dynamicPatch: Boolean(data.dynamic || data.runtimeAppended)
      ? {
        patchId: data.patchId || `patch_${node.id.slice(-6)}`,
        triggeredByTaskId: data.triggeredByTaskId,
        reason: data.dynamicReason || 'Appended at runtime',
        appendedAt: formatTimestamp(data.appendedAt),
        patchType: data.patchType || 'append_task',
      }
      : undefined,
    tags: [
      { key: 'category', value: node.data.category },
      { key: 'kind', value: kind },
      { key: 'configured', value: node.data.configured ? 'yes' : 'no' },
    ],
    artifacts: realArtifacts,
  };
}

function KeyValueSection({ title, rows }: { title: string; rows: Array<[string, ReactNode]> }) {
  return (
    <section className="workbench-inspector-section">
      <div className="workbench-inspector-section-title">{title}</div>
      <div className="workbench-inspector-grid">
        {rows.map(([label, value]) => (
          <div className="workbench-inspector-kv" key={label}>
            <span>{label}</span>
            <strong>{renderable(value) ? value : '-'}</strong>
          </div>
        ))}
      </div>
    </section>
  );
}

function InlineChips({ labels, empty = '-' }: { labels: string[]; empty?: string }) {
  if (labels.length === 0) {
    return <Text type="secondary">{empty}</Text>;
  }
  return (
    <Space size={[4, 4]} wrap>
      {labels.map((label) => <Tag key={label}>{label}</Tag>)}
    </Space>
  );
}

function DependencyChips({ labels }: { labels: string[] }) {
  return <InlineChips labels={labels} />;
}

function InspectorNote({ tone = 'info', children }: { tone?: 'info' | 'warning'; children: ReactNode }) {
  return (
    <div className={`workbench-inspector-note workbench-inspector-note-${tone}`}>
      {children}
    </div>
  );
}

function RunModeNotice() {
  return (
    <InspectorNote tone="warning">
      Changes will create a new workflow draft or WorkflowPatch. They will not mutate an already running/completed task directly.
    </InspectorNote>
  );
}

function FieldRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="workbench-inspector-field">
      <span>{label}</span>
      {children}
    </div>
  );
}

function InspectorTabs({ activeTab, onChange }: { activeTab: InspectorTab; onChange: (tab: InspectorTab) => void }) {
  return (
    <nav className="workbench-inspector-tabs">
      {tabs.map((tab) => (
        <button
          key={tab.key}
          type="button"
          className={activeTab === tab.key ? 'is-active' : ''}
          onClick={() => onChange(tab.key)}
        >
          {tab.label}
        </button>
      ))}
    </nav>
  );
}

function OverviewPanel({ task }: { task: WorkbenchTask }) {
  return (
    <>
      <section className="workbench-inspector-section">
        <div className="workbench-inspector-section-title">SUMMARY</div>
        <div className="workbench-inspector-metric-grid">
          <div>
            <span>Type</span>
            <strong>{taskTypeLabel(task.kind)} Operator</strong>
          </div>
          <div>
            <span>State</span>
            <strong><Tag color={statusColor(task.state)}>{task.state}</Tag></strong>
          </div>
          <div>
            <span>Resources</span>
            <strong>{resourceParts(task).join(' / ') || '-'}</strong>
          </div>
          <div>
            <span>Attempts</span>
            <strong>{task.runtime.attempt ? `${task.runtime.attempt} / ${task.runtime.maxAttempts || '-'}` : '-'}</strong>
          </div>
        </div>
      </section>

      <section className="workbench-inspector-section">
        <div className="workbench-inspector-section-title">DESCRIPTION</div>
        <Text type="secondary">{task.description || 'No description provided.'}</Text>
      </section>

      <KeyValueSection
        title="RUNTIME SUMMARY"
        rows={[
          ['Started', task.runtime.startedAt],
          ['Duration', task.runtime.duration],
          ['Dynamic', task.isDynamic ? 'Yes' : 'No'],
          ['Retry Policy', retryPolicyLabel(task)],
        ]}
      />

      <section className="workbench-inspector-section">
        <div className="workbench-inspector-section-title">DEPENDENCIES</div>
        <div className="workbench-inspector-subsection">
          <Text type="secondary">Upstream</Text>
          <DependencyChips labels={task.dependencies.upstream} />
        </div>
        <div className="workbench-inspector-subsection">
          <Text type="secondary">Downstream</Text>
          <DependencyChips labels={task.dependencies.downstream} />
        </div>
      </section>

      <KeyValueSection
        title="RESOURCES (REQUESTED)"
        rows={[
          ['CPU', task.resources.cpuNum ? `${task.resources.cpuNum} cores` : undefined],
          ['GPU Memory', formatStorageGiB(task.resources.gpuMemoryGiB)],
          ['I/O', task.resources.ioNum ? `${task.resources.ioNum}` : undefined],
        ]}
      />

      <KeyValueSection
        title="PLACEMENT (ASSIGNED)"
        rows={[
          ['Worker', task.placement?.worker],
          ['GPU Device', task.placement?.gpuDevice],
          ['Address', task.placement?.address],
        ]}
      />

      {task.isDynamic && task.dynamicPatch && (
        <KeyValueSection
          title="DYNAMIC PATCH"
          rows={[
            ['Status', 'Appended at runtime'],
            ['Patch ID', task.dynamicPatch.patchId],
            ['Triggered By', task.dynamicPatch.triggeredByTaskId],
            ['Reason', task.dynamicPatch.reason],
            ['Appended At', task.dynamicPatch.appendedAt],
          ]}
        />
      )}
    </>
  );
}

function DefinitionPanel({
  task,
  taskNode,
  nodes,
  readOnly,
  onEditCode,
  onUpdate,
}: {
  task: WorkbenchTask;
  taskNode: WorkflowNode;
  nodes: WorkflowNode[];
  readOnly: boolean;
  onEditCode?: () => void;
  onUpdate: (updates: Record<string, unknown>) => void;
}) {
  const sourceRef = taskNode.data.taskPath || taskNode.data.taskRef;
  const functionName = task.config.functionName
    || taskNode.data.functionName
    || (taskNode.data.taskRef ? String(taskNode.data.taskRef).split('.').pop() : '');
  const sourceNodes = nodes.filter((node) => node.id !== taskNode.id && node.data.outputs.length > 0);

  function updateInput(index: number, updates: Partial<WorkflowNode['data']['inputs'][number]>) {
    onUpdate({
      inputs: taskNode.data.inputs.map((input, inputIndex) => (
        inputIndex === index ? { ...input, ...updates } : input
      )),
    });
  }

  return (
    <>
      {readOnly && <RunModeNotice />}
      <section className="workbench-inspector-section">
        <div className="workbench-inspector-section-title workbench-inspector-section-title-row">
          <span>TASK DEFINITION</span>
          {onEditCode && (
            <Button size="small" type="primary" icon={<EditOutlined />} disabled={readOnly} onClick={onEditCode}>
              Edit code
            </Button>
          )}
        </div>
        <FieldRow label="Display name">
          <Input size="small" value={task.name} disabled={readOnly} onChange={(event) => onUpdate({ label: event.target.value })} />
        </FieldRow>
        <FieldRow label="Task type">
          <Select
            size="small"
            value={task.kind}
            disabled={readOnly}
            options={[
              { value: 'cpu', label: 'CPU' },
              { value: 'gpu', label: 'GPU' },
              { value: 'io', label: 'I/O' },
            ]}
            onChange={(value) => onUpdate({ task_kind: value })}
          />
        </FieldRow>
        <FieldRow label="Function name">
          <Input size="small" value={functionName} disabled={readOnly} onChange={(event) => onUpdate({ functionName: event.target.value || undefined })} />
        </FieldRow>
        {sourceRef && (
          <FieldRow label="Task source">
            <Input size="small" value={sourceRef} disabled />
          </FieldRow>
        )}
      </section>

      <section className="workbench-inspector-section">
        <div className="workbench-inspector-section-title">BINDINGS</div>
        {taskNode.data.inputs.length === 0 && <Text type="secondary">No inputs</Text>}
        {taskNode.data.inputs.map((input, index) => {
          const sourceNode = nodes.find((node) => node.id === input.taskSource?.taskId);
          return (
            <div key={input.name} className="workbench-inspector-subsection">
              <Text type="secondary">{input.name} ({input.dataType})</Text>
              <Space direction="vertical" size={6} style={{ width: '100%' }}>
                <Select
                  size="small"
                  value={input.source}
                  disabled={readOnly}
                  options={[
                    { value: 'user', label: 'User input' },
                    { value: 'task', label: 'From task', disabled: sourceNodes.length === 0 },
                  ]}
                  onChange={(source) => {
                    if (source === 'user') {
                      updateInput(index, { source, taskSource: undefined });
                      return;
                    }
                    const nextSource = sourceNode || sourceNodes[0];
                    updateInput(index, {
                      source,
                      taskSource: nextSource ? {
                        taskId: nextSource.id,
                        outputKey: nextSource.data.outputs[0]?.name || '',
                      } : undefined,
                    });
                  }}
                />
                {input.source === 'user' ? (
                  <Input
                    size="small"
                    value={input.value || ''}
                    disabled={readOnly}
                    placeholder="Value"
                    onChange={(event) => updateInput(index, { value: event.target.value })}
                  />
                ) : (
                  <Space.Compact block>
                    <Select
                      size="small"
                      value={input.taskSource?.taskId}
                      disabled={readOnly}
                      placeholder="Source task"
                      options={sourceNodes.map((node) => ({ value: node.id, label: node.data.label }))}
                      onChange={(taskId) => {
                        const nextSource = nodes.find((node) => node.id === taskId);
                        updateInput(index, {
                          taskSource: {
                            taskId,
                            outputKey: nextSource?.data.outputs[0]?.name || '',
                          },
                        });
                      }}
                    />
                    <Select
                      size="small"
                      value={input.taskSource?.outputKey}
                      disabled={readOnly || !sourceNode}
                      placeholder="Output"
                      options={(sourceNode?.data.outputs || []).map((output) => ({
                        value: output.name,
                        label: output.name,
                      }))}
                      onChange={(outputKey) => updateInput(index, {
                        taskSource: input.taskSource
                          ? { ...input.taskSource, outputKey }
                          : undefined,
                      })}
                    />
                  </Space.Compact>
                )}
              </Space>
            </div>
          );
        })}
        <FieldRow label="Outputs">
          <InlineChips labels={unknownItems(taskNode.data.outputs)} empty="No outputs" />
        </FieldRow>
      </section>

      <section className="workbench-inspector-section">
        <div className="workbench-inspector-section-title">DEPENDENCIES</div>
        <div className="workbench-inspector-subsection">
          <Text type="secondary">Upstream</Text>
          <DependencyChips labels={task.dependencies.upstream} />
        </div>
        <div className="workbench-inspector-subsection">
          <Text type="secondary">Downstream</Text>
          <DependencyChips labels={task.dependencies.downstream} />
        </div>
      </section>
      {!readOnly && (
        <InspectorNote>
          You are in Design mode. Changes update the workflow spec and apply to future runs.
        </InspectorNote>
      )}
    </>
  );
}

function ResourcesPanel({
  task,
  readOnly,
  localModels,
  onUpdate,
  onUpdateResources,
}: {
  task: WorkbenchTask;
  readOnly: boolean;
  localModels: LocalModel[];
  onUpdate: (updates: Record<string, unknown>) => void;
  onUpdateResources: (updates: Record<string, number | undefined>) => void;
}) {
  return (
    <>
      {readOnly && <RunModeNotice />}
      <section className="workbench-inspector-section workbench-inspector-resource-section">
        <div className="workbench-inspector-section-title">RESOURCE PROFILE / REQUESTED RESOURCES</div>
        <FieldRow label="CPU cores">
          <Input size="small" type="number" min={1} value={task.resources.cpuNum ?? ''} disabled={readOnly} onChange={(event) => onUpdateResources({ cpu_num: Number(event.target.value) || undefined })} />
        </FieldRow>
        <FieldRow label="GPU memory GiB">
          <Input size="small" type="number" min={0} value={task.resources.gpuMemoryGiB ?? ''} disabled={readOnly} onChange={(event) => onUpdateResources({ gpu_mem: event.target.value ? Number(event.target.value) * 1024 : undefined })} />
        </FieldRow>
        <FieldRow label="I/O units">
          <Input size="small" type="number" min={0} value={task.resources.ioNum ?? ''} disabled={readOnly} onChange={(event) => onUpdateResources({ io_num: Number(event.target.value) || undefined })} />
        </FieldRow>
        <FieldRow label="Local model">
          <Select
            allowClear
            size="small"
            placeholder="None"
            value={task.config.localModel}
            disabled={readOnly}
            options={localModels.map((model) => ({
              value: model.id,
              label: localModelLabel(model),
            }))}
            onChange={(value) => {
              const model = localModels.find((item) => item.id === value);
              onUpdate({
                localModel: value || undefined,
                modelAnchor: model ? modelAnchor(model) : undefined,
                ...(model ? { task_kind: 'gpu' } : {}),
              });
              if (model) {
                onUpdateResources({
                  cpu_num: task.resources.cpuNum || 1,
                  gpu_mem: Math.max(
                    Math.round((task.resources.gpuMemoryGiB || 0) * 1024),
                    model.estimated_gpu_mem_mb || 0,
                  ) || undefined,
                });
              }
            }}
          />
        </FieldRow>
        <FieldRow label="Timeout sec">
          <Input size="small" type="number" min={0} value={task.config.timeoutSeconds ?? ''} disabled={readOnly} onChange={(event) => onUpdate({ taskTimeout: Number(event.target.value) || undefined })} />
        </FieldRow>
        <FieldRow label="Max retries">
          <Input
            size="small"
            type="number"
            min={0} step={1} placeholder="Inherited"
            value={task.config.maxRetries ?? ''}
            disabled={readOnly}
            onChange={(event) => onUpdate({
              maxRetries: event.target.value === '' ? undefined : Math.max(0, Math.trunc(Number(event.target.value))),
            })}
          />
        </FieldRow>
        <FieldRow label="Retry backoff sec">
          <Input
            size="small"
            type="number"
            min={0} placeholder="Inherited"
            value={task.config.retryBackoffSeconds ?? ''}
            disabled={readOnly}
            onChange={(event) => onUpdate({
              retryBackoffSeconds: event.target.value === '' ? undefined : Math.max(0, Number(event.target.value)),
            })}
          />
        </FieldRow>
      </section>
      {!readOnly && (
        <InspectorNote>
          You are in Design mode. Resource changes update the workflow spec and apply to future runs.
        </InspectorNote>
      )}
    </>
  );
}

function RuntimePanel({ task, runTiming }: { task: WorkbenchTask; runTiming: RunTimingContext }) {
  const taskQueueTime = task.runtime.queueTimeRecorded ? task.runtime.queueTime : 'Not recorded';
  const faultAttempts = task.faultTolerance?.attempts || [];
  return (
    <>
      <KeyValueSection
        title="STATUS"
        rows={[
          ['State', <Tag color={statusColor(task.state)}>{task.state}</Tag>],
          ['Task Created', task.runtime.createdAt || 'Not recorded'],
          ['Started', task.runtime.startedAt],
          ['Finished', task.runtime.finishedAt],
          ['Duration', task.runtime.duration],
          ['Queue Time', taskQueueTime],
          ['Attempt', task.runtime.attempt ? `${task.runtime.attempt} / ${task.runtime.maxAttempts || '-'}` : undefined],
          ['Retry Count', task.runtime.retries],
          ['Exit Code', task.runtime.exitCode],
          ['Failure', task.runtime.failureReason],
        ]}
      />
      <KeyValueSection
        title="PLACEMENT"
        rows={[
          ['Worker', task.placement?.worker],
          ['GPU Device', task.placement?.gpuDevice],
          ['Host', task.placement?.host],
          ['Address', task.placement?.address],
          ['Last Heartbeat', task.runtime.lastHeartbeat],
        ]}
      />
      <KeyValueSection
        title="RESOURCES (ALLOCATED)"
        rows={[
          ['CPU', task.resources.cpuNum ? `${task.resources.cpuNum} cores` : undefined],
          ['GPU Memory', formatStorageGiB(task.resources.gpuMemoryGiB)],
          ['I/O', task.resources.ioNum ? `${task.resources.ioNum}` : undefined],
        ]}
      />
      <KeyValueSection
        title="TIMING"
        rows={[
          ['Run Created', runTiming.createdAt],
          ['Run Submitted', runTiming.submittedAt || runTiming.createdAt],
          ['Workflow Started', runTiming.startedAt],
          ['Task Started', task.runtime.startedAt],
          ['Finished', task.runtime.finishedAt],
          ['Duration', task.runtime.duration],
          ['Queue Time', taskQueueTime],
          ['Task Timeout', formatSecondsLabel(task.config.timeoutSeconds)],
        ]}
      />
      <section className="workbench-inspector-section">
        <div className="workbench-inspector-section-title">FAULT TOLERANCE TRACE</div>
        {faultAttempts.length === 0 ? (
          <Text type="secondary">No fault-tolerance action recorded.</Text>
        ) : (
          <div className="workbench-inspector-subsection">
            <Space size={[4, 4]} wrap>
              <Tag color={task.faultTolerance?.status === 'recovered' ? 'green' : task.faultTolerance?.status === 'failed' ? 'red' : 'blue'}>
                {task.faultTolerance?.status || 'recorded'}
              </Tag>
              <Tag>{faultAttempts.length} event{faultAttempts.length > 1 ? 's' : ''}</Tag>
            </Space>
            {faultAttempts.map((entry, index) => (
              <div className="workbench-inspector-note" key={`${entry?.attempt || 'attempt'}-${index}`}>
                <Text strong>Attempt {entry?.attempt || index + 1}</Text>
                <div>Failure: {faultText(entry, 'failure')}</div>
                <div>Diagnosis: {faultText(entry, 'diagnosis')}</div>
                <div>Repair: {faultText(entry, 'repair_action')}</div>
                <div>Retry: {faultText(entry, 'retry')}</div>
                <div>Outcome: {faultText(entry, 'outcome')}</div>
              </div>
            ))}
          </div>
        )}
      </section>
      <section className="workbench-inspector-section">
        <div className="workbench-inspector-section-title">DEPENDENCIES</div>
        <div className="workbench-inspector-runtime-links">
          <span>Upstream <strong>{task.dependencies.upstream.length}</strong></span>
          <span>Downstream <strong>{task.dependencies.downstream.length}</strong></span>
        </div>
      </section>
    </>
  );
}

function withDisposition(href: string, disposition: 'inline' | 'attachment') {
  const separator = href.includes('?') ? '&' : '?';
  return `${href}${separator}disposition=${disposition}`;
}

function artifactDownloadHref(artifact: ArtifactItem, disposition: 'inline' | 'attachment' = 'attachment') {
  let href: string | null = null;
  if (artifact.sha256) {
    href = api.getArtifactDownloadUrl(artifact.sha256);
  } else if (artifact.uri && /^https?:\/\//i.test(artifact.uri)) {
    href = artifact.uri;
  }
  return href ? withDisposition(href, disposition) : null;
}

function ArtifactsPanel({ task }: { task: WorkbenchTask }) {
  const artifacts = task.artifacts || [];
  if (artifacts.length === 0) {
    return (
      <section className="workbench-inspector-section">
        <div className="workbench-inspector-section-title">TASK ARTIFACTS</div>
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No artifacts produced by this task yet." />
        <InspectorNote>
          Artifacts here are workflow run outputs produced by this task, not a general workspace file manager.
        </InspectorNote>
      </section>
    );
  }
  return (
    <section className="workbench-inspector-section">
      <div className="workbench-inspector-section-title">TASK ARTIFACTS</div>
      <div className="workbench-inspector-artifacts">
        {artifacts.map((artifact) => {
          const openHref = artifactDownloadHref(artifact, 'inline');
          const downloadHref = artifactDownloadHref(artifact, 'attachment');
          return (
            <div className="workbench-inspector-artifact" key={artifact.id}>
              <div className="workbench-inspector-artifact-main">
                <strong>{artifact.name}</strong>
                <Text type="secondary" ellipsis>{artifact.uri || artifact.path || '-'}</Text>
              </div>
              <span className="workbench-inspector-artifact-size">{artifact.size || '-'}</span>
              <div className="workbench-inspector-artifact-meta">
                <Tag color={artifact.status === 'produced' ? 'green' : artifact.status === 'failed' ? 'red' : 'default'}>
                  {artifact.type}
                </Tag>
                <span>{artifact.createdAt || '-'}</span>
                <span title={artifact.producedBy || task.id}>
                  {shortArtifactId(artifact.producedBy || task.id)}
                </span>
              </div>
              <Space size={4} className="workbench-inspector-artifact-actions">
                <Button size="small" href={openHref || undefined} target={openHref ? '_blank' : undefined} disabled={!openHref}>
                  Open
                </Button>
                <Button size="small" href={downloadHref || undefined} target={downloadHref ? '_blank' : undefined} disabled={!downloadHref}>
                  Download
                </Button>
              </Space>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function WorkflowSummaryPanel({
  nodes,
  edges,
  dynamicCount,
  resourceEstimate,
  workflowSaveState,
  workflowDraftError,
  latestRun,
}: {
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  dynamicCount: number;
  resourceEstimate: { cpu: number; gpuMemoryGiB: number; io: number };
  workflowSaveState: string;
  workflowDraftError: string | null;
  latestRun: any;
}) {
  return (
    <>
      <div className="workbench-summary-grid">
        <div>
          <AppstoreOutlined />
          <strong>{nodes.length}</strong>
          <span>tasks</span>
        </div>
        <div>
          <NodeIndexOutlined />
          <strong>{edges.length}</strong>
          <span>edges</span>
        </div>
        <div>
          <FileDoneOutlined />
          <strong>{dynamicCount}</strong>
          <span>dynamic</span>
        </div>
      </div>
      <KeyValueSection
        title="WORKFLOW SUMMARY"
        rows={[
          ['Validation Status', <Tag color={workflowSaveState === 'error' ? 'red' : 'blue'}>{workflowSaveState}</Tag>],
          ['Validation Issue', workflowDraftError],
          ['Task Count', nodes.length],
          ['Dynamic Task Count', dynamicCount],
          ['Current Run Status', latestRun ? <Tag color={statusColor(latestRun.status)}>{latestRun.status}</Tag> : undefined],
          ['Estimated CPU', resourceEstimate.cpu || '-'],
          ['Estimated GPU Memory', resourceEstimate.gpuMemoryGiB ? `${resourceEstimate.gpuMemoryGiB.toFixed(2)} GiB` : '-'],
          ['Estimated I/O', resourceEstimate.io || '-'],
        ]}
      />
      <KeyValueSection
        title="CURRENT RUN"
        rows={[
          ['Run ID', latestRun?.run_id],
          ['Workflow', latestRun?.workflow_name],
          ['Created', formatTimestamp(latestRun?.created_time)],
          ['Running Tasks', latestRun?.task_counts?.running],
          ['Queued Tasks', latestRun?.task_counts?.queued],
          ['Succeeded Tasks', latestRun?.task_counts?.succeeded || latestRun?.task_counts?.completed],
          ['Failed Tasks', latestRun?.task_counts?.failed],
        ]}
      />
    </>
  );
}

export default function TaskInspector() {
  const [activeTab, setActiveTab] = useState<InspectorTab>('overview');
  const [localModels, setLocalModels] = useState<LocalModel[]>([]);
  const [taskArtifacts, setTaskArtifacts] = useState<RunArtifact[]>([]);
  const [editorOpen, setEditorOpen] = useState(false);
  const {
    selectedNode,
    nodes,
    edges,
    workflowName,
    workflowSaveState,
    workflowDraftError,
    workflowId,
    workspaceId,
    workspaceDir,
    currentWorkspaceWorkflowPath,
    selectedRunId,
    selectedRunTaskId,
    staticRuns,
    updateNode,
    deleteNode,
    setEdges,
  } = useWorkflowStore();

  const identity = useMemo(() => ({
    workflowId,
    workflowPath: currentWorkspaceWorkflowPath,
    workspaceId,
    workspaceDir,
  }), [currentWorkspaceWorkflowPath, workflowId, workspaceDir, workspaceId]);
  const historicalRun = selectedRunId
    ? staticRuns.find((run) => run.run_id === selectedRunId) || null
    : null;
  const designRun = useMemo(
    () => latestRunForWorkflow(staticRuns, identity),
    [identity, staticRuns],
  );
  const isRunMode = Boolean(selectedRunId);
  const visibleRun = isRunMode ? historicalRun : designRun;
  const visibleRunId = visibleRun?.run_id || null;
  const runGraph = useMemo(
    () => historicalRun ? runWorkflowGraph(historicalRun) : { nodes: [], edges: [] },
    [historicalRun],
  );
  const inspectorNodes = isRunMode ? runGraph.nodes : nodes;
  const inspectorEdges = isRunMode ? runGraph.edges : edges;
  const currentSelectedNode = isRunMode
    ? inspectorNodes.find((node) => node.id === selectedRunTaskId) || null
    : selectedNode
      ? nodes.find((node) => node.id === selectedNode.id) || selectedNode
      : null;
  const runtime = currentSelectedNode && visibleRun?.task_nodes?.[currentSelectedNode.id]
    ? visibleRun.task_nodes[currentSelectedNode.id]
    : null;
  const runtimeTaskId = runtime?.task_id || currentSelectedNode?.id;
  const runTiming = {
    createdAt: formatTimestamp(visibleRun?.created_time),
    submittedAt: formatTimestamp(visibleRun?.submitted_time ?? visibleRun?.created_time),
    startedAt: formatTimestamp(visibleRun?.started_time ?? firstTaskStartedTime(visibleRun?.task_nodes)),
  };
  const task = currentSelectedNode
    ? buildTask(currentSelectedNode, runtime, taskArtifacts, inspectorEdges, inspectorNodes)
    : null;
  const isSpecReadOnly = isRunMode;
  const canEditCode = Boolean(
    !isRunMode
    &&
    currentSelectedNode
    && ['custom', 'workspace'].includes(currentSelectedNode.data.category),
  );

  const dynamicCount = useMemo(
    () => inspectorNodes.filter((node) => Boolean((node.data as any).dynamic || (node.data as any).runtimeAppended)).length,
    [inspectorNodes],
  );
  const resourceEstimate = useMemo(() => ({
    cpu: inspectorNodes.reduce((sum, node) => sum + Number((node.data.resources as any)?.cpu_num ?? (node.data.resources as any)?.cpu ?? 0), 0),
    gpuMemoryGiB: inspectorNodes.reduce((sum, node) => sum + Number(node.data.resources?.gpu_mem || 0) / 1024, 0),
    io: inspectorNodes.reduce((sum, node) => sum + Number((node.data.resources as any)?.io_num || 0), 0),
  }), [inspectorNodes]);
  const latestRun = visibleRun;

  useEffect(() => {
    api.getModels()
      .then((result) => setLocalModels(result.models || []))
      .catch(() => setLocalModels([]));
  }, []);

  useEffect(() => {
    if (isRunMode) {
      setEditorOpen(false);
    }
  }, [isRunMode]);

  useEffect(() => {
    if (!visibleRunId || !runtimeTaskId) {
      setTaskArtifacts([]);
      return undefined;
    }
    let cancelled = false;
    setTaskArtifacts([]);
    api.getRunTaskArtifacts(visibleRunId, runtimeTaskId)
      .then((result) => {
        if (!cancelled) {
          setTaskArtifacts(result.artifacts || []);
        }
      })
      .catch((error) => {
        if (!cancelled) {
          console.debug('Failed to load task artifacts:', error);
          setTaskArtifacts([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [runtime?.status, runtimeTaskId, visibleRunId]);

  const visibleWorkflowName = isRunMode ? runWorkflowName(historicalRun) : workflowName;
  const headerTitle = task?.name || visibleWorkflowName;
  const headerState = task?.state || (isRunMode
    ? visibleRun?.status || 'draft'
    : workflowSaveState === 'saved_workflow' ? 'validated' : 'draft');
  const headerKind = task ? 'Task' : 'Workflow';
  const modeLabel = isRunMode ? 'Run mode' : 'Design mode';

  function updateTaskData(updates: Record<string, unknown>) {
    if (!task || isSpecReadOnly) return;
    if (Array.isArray(updates.inputs)) {
      setEdges(syncWorkflowInputEdges(
        nodes,
        edges,
        task.id,
        updates.inputs as WorkflowNode['data']['inputs'],
      ));
    }
    updateNode(task.id, updates as Partial<WorkflowNode['data']>);
  }

  function updateTaskResources(updates: Record<string, number | undefined>) {
    if (!task || !currentSelectedNode || isSpecReadOnly) return;
    updateNode(task.id, {
      resources: {
        ...(currentSelectedNode.data.resources || { cpu_num: 1, gpu_mem: 0, io_num: 0 }),
        ...updates,
      },
    });
  }

  return (
    <>
      <div className="workbench-inspector">
      <header className="workbench-inspector-header">
        <div className="workbench-inspector-title-block">
          <div className="workbench-inspector-title-row">
            <Text strong className="workbench-inspector-title">{headerTitle}</Text>
            <span className="workbench-inspector-badges">
              <Tag color={statusColor(headerState)}>{headerState}</Tag>
              <Tag color={task ? 'blue' : 'geekblue'}>{headerKind}</Tag>
              <Tag>{modeLabel}</Tag>
              {task?.isDynamic && <Tag color="purple">Dynamic</Tag>}
            </span>
          </div>
          <Space size={6} className="workbench-inspector-id">
            <Text type="secondary">ID: {task?.id || 'workflow'}</Text>
            <Button
              type="text"
              size="small"
              icon={<CopyOutlined />}
              aria-label="Copy object id"
              onClick={() => navigator.clipboard?.writeText(task?.id || 'workflow')}
            />
            {task && !isSpecReadOnly && (
              <Popconfirm
                title="Delete task"
                description="Delete this task and its connections?"
                okText="Delete"
                okButtonProps={{ danger: true }}
                onConfirm={() => {
                  setEditorOpen(false);
                  deleteNode(task.id);
                }}
              >
                <Button type="text" danger size="small" icon={<DeleteOutlined />} aria-label="Delete task" />
              </Popconfirm>
            )}
          </Space>
        </div>
      </header>

      {task && <InspectorTabs activeTab={activeTab} onChange={setActiveTab} />}

      <div className="workbench-inspector-content">
        {task && currentSelectedNode ? (
          <>
            {activeTab === 'overview' && <OverviewPanel task={task} />}
            {activeTab === 'definition' && (
              <DefinitionPanel
                task={task}
                taskNode={currentSelectedNode}
                nodes={inspectorNodes}
                readOnly={isSpecReadOnly}
                onEditCode={canEditCode ? () => setEditorOpen(true) : undefined}
                onUpdate={updateTaskData}
              />
            )}
            {activeTab === 'resources' && (
              <ResourcesPanel
                task={task}
                readOnly={isSpecReadOnly}
                localModels={localModels}
                onUpdate={updateTaskData}
                onUpdateResources={updateTaskResources}
              />
            )}
            {activeTab === 'runtime' && <RuntimePanel task={task} runTiming={runTiming} />}
            {activeTab === 'artifacts' && (
              <ArtifactsPanel task={task} />
            )}
          </>
        ) : (
          <WorkflowSummaryPanel
            nodes={inspectorNodes}
            edges={inspectorEdges}
            dynamicCount={dynamicCount}
            resourceEstimate={resourceEstimate}
            workflowSaveState={isRunMode ? 'run_snapshot' : workflowSaveState}
            workflowDraftError={isRunMode ? null : workflowDraftError}
            latestRun={latestRun}
          />
        )}
        </div>
      </div>
      {!isRunMode && currentSelectedNode && canEditCode && (
        <CustomTaskEditor
          node={currentSelectedNode}
          open={editorOpen}
          onClose={() => setEditorOpen(false)}
        />
      )}
    </>
  );
}
