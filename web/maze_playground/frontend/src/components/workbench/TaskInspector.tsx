import { useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { Button, Empty, Input, Select, Space, Tag, Typography } from 'antd';
import {
  AppstoreOutlined,
  CopyOutlined,
  EditOutlined,
  FileDoneOutlined,
  NodeIndexOutlined,
} from '@ant-design/icons';
import { api } from '@/api/client';
import { useWorkflowStore } from '@/stores/workflowStore';
import type {
  StaticWorkflowRunEvent,
  StaticWorkflowRunNode,
  WorkflowEdge,
  WorkflowNode,
} from '@/types/workflow';

const { Text } = Typography;

type InspectorTab = 'overview' | 'definition' | 'resources' | 'runtime' | 'artifacts';
type TaskState = 'pending' | 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'draft' | 'validated';
type ImplementationType = 'Python function' | 'command' | 'container image' | 'local LLM inference';

type ArtifactItem = {
  id: string;
  name: string;
  type: 'file' | 'dataset' | 'model' | 'report' | 'log';
  size?: string;
  status: 'pending' | 'produced' | 'failed';
  uri?: string;
  sha256?: string;
  path?: string;
  runId?: string;
  taskId?: string;
  producerTaskId?: string;
  mime?: string;
  storagePath?: string;
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
  kind: 'cpu' | 'gpu' | 'io' | 'llm';
  state: TaskState;
  isDynamic?: boolean;
  description?: string;
  config: {
    implementationType: ImplementationType;
    image?: string;
    command?: string;
    functionName?: string;
    entryPoint?: string;
    arguments?: unknown;
    environment?: unknown;
    inputBindings?: unknown;
    outputBindings?: unknown;
    artifactOutputs?: unknown;
    priority?: 'Low' | 'Normal' | 'High' | 'Critical';
    timeoutSeconds?: number;
    maxAttempts?: number;
    retryBackoffSeconds?: number;
    maxConcurrency?: number;
    placementConstraints?: unknown;
    requiredCapabilities?: unknown;
    localityHints?: unknown;
  };
  resources: {
    cpu?: number;
    memoryGiB?: number;
    gpu?: number;
    gpuMemoryGiB?: number;
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

const implementationOptions: Array<{ value: ImplementationType; label: string }> = [
  { value: 'Python function', label: 'Python function' },
  { value: 'command', label: 'Command' },
  { value: 'container image', label: 'Container image' },
  { value: 'local LLM inference', label: 'Local LLM inference' },
];

function normalizeState(status?: string | null): TaskState {
  if (!status) return 'pending';
  if (status === 'completed') return 'succeeded';
  if (status === 'canceled') return 'cancelled';
  if (status === 'timed_out' || status === 'interrupted') return 'failed';
  if (['pending', 'queued', 'running', 'succeeded', 'failed', 'cancelled', 'draft', 'validated'].includes(status)) {
    return status as TaskState;
  }
  return 'pending';
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
  const explicitKind = (node.data as any).taskKind;
  if (['cpu', 'gpu', 'io', 'llm'].includes(explicitKind)) return explicitKind;
  const resources = node.data.resources;
  const label = `${node.data.label} ${node.data.taskRef || ''} ${node.data.functionName || ''} ${node.data.taskPath || ''}`.toLowerCase();
  if (resources?.gpu || label.includes('gpu') || label.includes('cuda')) return 'gpu';
  if (label.includes('llm') || label.includes('model') || label.includes('inference')) return 'llm';
  if (label.includes('file') || label.includes('io') || label.includes('input') || label.includes('artifact')) return 'io';
  return 'cpu';
}

function taskTypeLabel(kind: WorkbenchTask['kind']) {
  if (kind === 'gpu') return 'GPU';
  if (kind === 'io') return 'I/O';
  if (kind === 'llm') return 'LLM';
  return 'CPU';
}

function inferImplementationType(node: WorkflowNode): ImplementationType {
  const data = node.data as any;
  if (implementationOptions.some((option) => option.value === data.implementationType)) {
    return data.implementationType;
  }
  if (node.data.execBackend === 'docker' || data.image) return 'container image';
  if (data.prompt || String(node.data.taskRef || node.data.functionName || '').toLowerCase().includes('llm')) {
    return 'local LLM inference';
  }
  if (data.command) return 'command';
  return 'Python function';
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

function firstTaskStartedTime(taskNodes?: Record<string, StaticWorkflowRunNode>) {
  const startedTimes = Object.values(taskNodes || {})
    .map((task) => toEpochMilliseconds(task.started_time))
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

function renderable(value: ReactNode) {
  return value !== undefined && value !== null && value !== '';
}

function jsonText(value: unknown, fallback: unknown = {}) {
  const source = value === undefined || value === null ? fallback : value;
  try {
    return JSON.stringify(source, null, 2);
  } catch {
    return String(source);
  }
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
    task.resources.cpu ? `${task.resources.cpu} vCPU` : null,
    task.resources.memoryGiB ? `${task.resources.memoryGiB} GiB` : null,
    task.resources.gpu ? `${task.resources.gpu} GPU` : null,
    task.resources.gpuMemoryGiB ? `${task.resources.gpuMemoryGiB} GiB GPU` : null,
  ].filter(Boolean) as string[];
}

function retryPolicyLabel(task: WorkbenchTask) {
  const attempts = task.config.maxAttempts || task.runtime.maxAttempts;
  const backoff = task.config.retryBackoffSeconds;
  if (!attempts && !backoff) return undefined;
  return [
    attempts ? `${attempts} attempts` : null,
    backoff ? `${backoff}s backoff` : null,
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
  runtime: StaticWorkflowRunNode | null,
  runId: string | null | undefined,
  edges: WorkflowEdge[],
  nodes: WorkflowNode[],
  _events: StaticWorkflowRunEvent[],
): WorkbenchTask {
  const data = node.data as any;
  const kind = taskKind(node);
  const state = normalizeState(runtime?.status);
  const resources = node.data.resources || runtime?.resources || {};
  const cpuMem = Number((resources as any).cpu_mem || 0);
  const gpuMem = Number((resources as any).gpu_mem || 0);
  const selectedNode = runtime?.schedule_decision?.selected_node;
  const queueReason = runtime?.pending_reason || runtime?.schedule_decision?.reason || undefined;
  const manifest = (runtime as any)?.file_manifest || {};
  const artifactRecords = [
    ...((runtime?.artifacts || []) as any[]),
    ...((manifest.files || []) as any[]),
  ];
  const seenArtifacts = new Set<string>();
  const realArtifacts = artifactRecords.flatMap((artifact, index) => {
    const pathValue = artifactPath(artifact);
    const key = artifact?.sha256 || artifact?.uri || artifact?.storage_path || pathValue || `artifact-${index}`;
    if (seenArtifacts.has(key)) return [];
    seenArtifacts.add(key);
    const taskId = artifact?.task_id
      || artifact?.producer_task_id
      || manifest.task_id
      || (runtime as any)?.maze_task_id
      || (runtime as any)?.task_id
      || runtime?.node_id
      || node.id;
    const artifactRunId = artifact?.run_id || manifest.run_id || runId || undefined;

    return [{
      id: key,
      name: artifact?.name || pathValue?.split('/').pop() || pathValue || artifact?.sha256 || `artifact-${index}`,
      type: 'file' as const,
      size: formatFileSize(artifact?.size),
      status: 'produced' as const,
      uri: artifact?.uri || artifact?.storage_uri || artifact?.storage_path || pathValue,
      sha256: artifact?.sha256,
      path: pathValue,
      runId: artifactRunId,
      taskId,
      producerTaskId: artifact?.producer_task_id,
      mime: artifact?.mime,
      storagePath: artifact?.storage_path,
      createdAt: formatTimestamp(artifact?.created_time || manifest.created_time || runtime?.finished_time),
      producedBy: taskId || node.id,
    }];
  });

  return {
    id: node.id,
    name: node.data.label,
    kind,
    state,
    isDynamic: Boolean(data.dynamic || data.runtimeAppended),
    description: data.description || data.summary || data.prompt,
    config: {
      implementationType: inferImplementationType(node),
      image: data.image || (node.data.execBackend === 'docker' ? 'docker sandbox' : undefined),
      command: data.command || node.data.taskRef || node.data.taskPath || node.data.functionName,
      functionName: node.data.functionName,
      entryPoint: data.entryPoint || node.data.functionName || node.data.taskRef,
      arguments: data.arguments || data.args || node.data.inputs,
      environment: data.environment || data.env,
      inputBindings: node.data.inputs,
      outputBindings: node.data.outputs,
      artifactOutputs: data.artifacts || data.artifactOutputs,
      priority: data.priority || 'Normal',
      timeoutSeconds: node.data.taskTimeout || runtime?.timeout_seconds || undefined,
      maxAttempts: data.maxAttempts || (runtime as any)?.max_attempts || undefined,
      retryBackoffSeconds: data.retryBackoffSeconds || runtime?.retry_wait_seconds || undefined,
      maxConcurrency: data.maxConcurrency,
      placementConstraints: data.placementConstraints,
      requiredCapabilities: data.requiredCapabilities,
      localityHints: data.localityHints,
    },
    resources: {
      cpu: Number((resources as any).cpu || 0) || undefined,
      memoryGiB: cpuMem ? Number((cpuMem / 1024).toFixed(2)) : undefined,
      gpu: Number((resources as any).gpu || 0) || undefined,
      gpuMemoryGiB: gpuMem ? Number((gpuMem / 1024).toFixed(2)) : undefined,
    },
    dependencies: {
      upstream: dependencyLabels(node.id, edges, nodes, 'upstream'),
      downstream: dependencyLabels(node.id, edges, nodes, 'downstream'),
    },
    runtime: {
      createdAt: formatTimestamp(runtime?.created_time),
      startedAt: formatTimestamp(runtime?.started_time),
      finishedAt: formatTimestamp(runtime?.finished_time),
      duration: formatDuration(runtime?.duration_seconds) || durationBetween(runtime?.started_time, runtime?.finished_time),
      queueTime: durationBetween(runtime?.created_time, runtime?.started_time) || formatDuration((runtime as any)?.queue_time_seconds),
      queueTimeRecorded: Boolean(runtime?.created_time || (runtime as any)?.queue_time_seconds),
      attempt: (runtime as any)?.attempt || undefined,
      maxAttempts: data.maxAttempts || (runtime as any)?.max_attempts || undefined,
      retries: (runtime as any)?.attempt ? Math.max(0, Number((runtime as any).attempt) - 1) : undefined,
      exitCode: (runtime as any)?.exit_code,
      failureReason: errorMessage(runtime?.error) || errorMessage(runtime?.last_error),
      queueReason,
      lastHeartbeat: formatTimestamp((runtime as any)?.last_heartbeat || (runtime as any)?.heartbeat_time),
      schedulingReason: runtime?.schedule_decision?.reason || undefined,
    },
    placement: {
      worker: runtime?.node_ip || selectedNode?.node_ip || runtime?.node_id_runtime || undefined,
      node: runtime?.node_id_runtime || selectedNode?.node_id || undefined,
      gpuDevice: runtime?.gpu_id ?? selectedNode?.gpu_id ?? undefined,
      zone: (runtime as any)?.zone,
      host: (runtime as any)?.host,
      address: (runtime as any)?.address || runtime?.node_ip || selectedNode?.node_ip || undefined,
      reason: runtime?.schedule_decision?.reason || undefined,
      scheduledAt: formatTimestamp(runtime?.started_time),
    },
    queueInfo: state === 'queued' || state === 'pending'
      ? {
        reason: queueReason,
        required: (resources as any).gpu ? `${(resources as any).gpu} GPU` : (resources as any).cpu ? `${(resources as any).cpu} CPU` : undefined,
        available: runtime?.schedule_decision?.candidate_nodes?.[0]?.available_resources
          ? JSON.stringify(runtime.schedule_decision.candidate_nodes[0].available_resources)
          : undefined,
        queuedFor: durationBetween(runtime?.created_time, runtime?.started_time),
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

function JsonEditor({
  label,
  value,
  fallback,
  disabled,
  onCommit,
}: {
  label: string;
  value: unknown;
  fallback?: unknown;
  disabled: boolean;
  onCommit: (value: unknown) => void;
}) {
  return (
    <FieldRow label={label}>
      <Input.TextArea
        key={`${label}-${disabled}-${jsonText(value, fallback)}`}
        size="small"
        autoSize={{ minRows: 2, maxRows: 5 }}
        defaultValue={jsonText(value, fallback)}
        disabled={disabled}
        onBlur={(event) => {
          try {
            const text = event.target.value.trim();
            onCommit(text ? JSON.parse(text) : undefined);
          } catch {
            window.alert(`${label} must be valid JSON.`);
          }
        }}
      />
    </FieldRow>
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
        <Text type="secondary">{task.description || task.config.command || 'No description provided.'}</Text>
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
          ['CPU', task.resources.cpu ? `${task.resources.cpu} vCPU` : undefined],
          ['Memory', formatStorageGiB(task.resources.memoryGiB)],
          ['GPU', task.resources.gpu ? `${task.resources.gpu}` : undefined],
          ['GPU Memory', formatStorageGiB(task.resources.gpuMemoryGiB)],
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
  readOnly,
  onOpenNodePanel,
  onUpdate,
}: {
  task: WorkbenchTask;
  taskNode: WorkflowNode;
  readOnly: boolean;
  onOpenNodePanel?: () => void;
  onUpdate: (updates: Record<string, unknown>) => void;
}) {
  const executionMode = task.config.implementationType;
  const isPythonTask = executionMode === 'Python function';
  const isCommandTask = executionMode === 'command';
  const isContainerTask = executionMode === 'container image';
  const isLlmTask = executionMode === 'local LLM inference';
  const sourceRef = taskNode.data.taskPath || taskNode.data.taskRef;
  const functionName = task.config.functionName
    || taskNode.data.functionName
    || (taskNode.data.taskRef ? String(taskNode.data.taskRef).split('.').pop() : '');

  return (
    <>
      {readOnly && <RunModeNotice />}
      <section className="workbench-inspector-section">
        <div className="workbench-inspector-section-title workbench-inspector-section-title-row">
          <span>TASK DEFINITION</span>
          {onOpenNodePanel && (
            <Button size="small" type="primary" icon={<EditOutlined />} disabled={readOnly} onClick={onOpenNodePanel}>
              Edit task
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
              { value: 'llm', label: 'LLM' },
            ]}
            onChange={(value) => onUpdate({ taskKind: value })}
          />
        </FieldRow>
        <FieldRow label="Execution mode">
          <Select
            size="small"
            value={executionMode}
            disabled={readOnly}
            options={implementationOptions}
            onChange={(value) => onUpdate({ implementationType: value })}
          />
        </FieldRow>
        {(isPythonTask || isLlmTask) && (
          <FieldRow label={isLlmTask ? 'Inference function' : 'Function name'}>
            <Input size="small" value={functionName} disabled={readOnly} onChange={(event) => onUpdate({ functionName: event.target.value || undefined })} />
          </FieldRow>
        )}
        {sourceRef && (
          <FieldRow label="Task source">
            <Input size="small" value={sourceRef} disabled />
          </FieldRow>
        )}
        {isCommandTask && (
          <FieldRow label="Command">
            <Input size="small" value={task.config.command || ''} disabled={readOnly} onChange={(event) => onUpdate({ command: event.target.value || undefined })} />
          </FieldRow>
        )}
        {isContainerTask && (
          <>
            <FieldRow label="Image">
              <Input size="small" value={task.config.image || ''} disabled={readOnly} onChange={(event) => onUpdate({ image: event.target.value || undefined })} />
            </FieldRow>
            <FieldRow label="Entry point">
              <Input size="small" value={task.config.entryPoint || ''} disabled={readOnly} onChange={(event) => onUpdate({ entryPoint: event.target.value || undefined })} />
            </FieldRow>
          </>
        )}
        <FieldRow label="Arguments">
          <Input.TextArea
            size="small"
            autoSize={{ minRows: 2, maxRows: 4 }}
            value={typeof task.config.arguments === 'string' ? task.config.arguments : jsonText(task.config.arguments, [])}
            disabled={readOnly}
            onChange={(event) => {
              const text = event.target.value;
              try {
                onUpdate({ arguments: text.trim() ? JSON.parse(text) : undefined });
              } catch {
                onUpdate({ arguments: text });
              }
            }}
          />
        </FieldRow>
      </section>

      <section className="workbench-inspector-section">
        <div className="workbench-inspector-section-title">BINDINGS</div>
        <FieldRow label="Inputs">
          <InlineChips labels={unknownItems(taskNode.data.inputs)} empty="No inputs" />
        </FieldRow>
        <FieldRow label="Outputs">
          <InlineChips labels={unknownItems(taskNode.data.outputs)} empty="No outputs" />
        </FieldRow>
        <FieldRow label="Artifacts">
          <InlineChips labels={unknownItems(task.config.artifactOutputs)} empty="No artifact outputs" />
        </FieldRow>
        <details className="workbench-inspector-advanced">
          <summary>Advanced raw spec</summary>
          <JsonEditor label="Environment" value={task.config.environment} fallback={{}} disabled={readOnly} onCommit={(value) => onUpdate({ environment: value, env: value })} />
          <JsonEditor label="Input bindings" value={taskNode.data.inputs} fallback={[]} disabled={readOnly} onCommit={(value) => onUpdate({ inputs: Array.isArray(value) ? value : [] })} />
          <JsonEditor label="Output bindings" value={taskNode.data.outputs} fallback={[]} disabled={readOnly} onCommit={(value) => onUpdate({ outputs: Array.isArray(value) ? value : [] })} />
          <JsonEditor label="Artifact outputs" value={task.config.artifactOutputs} fallback={[]} disabled={readOnly} onCommit={(value) => onUpdate({ artifactOutputs: value, artifacts: value })} />
        </details>
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
  onUpdate,
  onUpdateResources,
}: {
  task: WorkbenchTask;
  readOnly: boolean;
  onUpdate: (updates: Record<string, unknown>) => void;
  onUpdateResources: (updates: Record<string, number | undefined>) => void;
}) {
  return (
    <>
      {readOnly && <RunModeNotice />}
      <section className="workbench-inspector-section workbench-inspector-resource-section">
        <div className="workbench-inspector-section-title">RESOURCE PROFILE / REQUESTED RESOURCES</div>
        <FieldRow label="CPU cores">
          <Input size="small" type="number" min={0} value={task.resources.cpu ?? ''} disabled={readOnly} onChange={(event) => onUpdateResources({ cpu: Number(event.target.value) || undefined })} />
        </FieldRow>
        <FieldRow label="Memory GiB">
          <Input size="small" type="number" min={0} value={task.resources.memoryGiB ?? ''} disabled={readOnly} onChange={(event) => onUpdateResources({ cpu_mem: event.target.value ? Number(event.target.value) * 1024 : undefined })} />
        </FieldRow>
        <FieldRow label="GPU count">
          <Input size="small" type="number" min={0} value={task.resources.gpu ?? ''} disabled={readOnly} onChange={(event) => onUpdateResources({ gpu: Number(event.target.value) || undefined })} />
        </FieldRow>
        <FieldRow label="GPU memory GiB">
          <Input size="small" type="number" min={0} value={task.resources.gpuMemoryGiB ?? ''} disabled={readOnly} onChange={(event) => onUpdateResources({ gpu_mem: event.target.value ? Number(event.target.value) * 1024 : undefined })} />
        </FieldRow>
        <FieldRow label="Timeout sec">
          <Input size="small" type="number" min={0} value={task.config.timeoutSeconds ?? ''} disabled={readOnly} onChange={(event) => onUpdate({ taskTimeout: Number(event.target.value) || undefined })} />
        </FieldRow>
        <FieldRow label="Max attempts">
          <Input size="small" type="number" min={0} value={task.config.maxAttempts ?? ''} disabled={readOnly} onChange={(event) => onUpdate({ maxAttempts: Number(event.target.value) || undefined })} />
        </FieldRow>
        <FieldRow label="Priority">
          <Select
            size="small"
            value={task.config.priority || 'Normal'}
            disabled={readOnly}
            options={['Low', 'Normal', 'High', 'Critical'].map((value) => ({ value, label: value }))}
            onChange={(value) => onUpdate({ priority: value })}
          />
        </FieldRow>
        <details className="workbench-inspector-advanced">
          <summary>Advanced scheduling</summary>
          <JsonEditor label="Placement constraints" value={task.config.placementConstraints} fallback={{}} disabled={readOnly} onCommit={(value) => onUpdate({ placementConstraints: value })} />
          <JsonEditor label="Required capabilities" value={task.config.requiredCapabilities} fallback={[]} disabled={readOnly} onCommit={(value) => onUpdate({ requiredCapabilities: value })} />
          <JsonEditor label="Locality hints" value={task.config.localityHints} fallback={{}} disabled={readOnly} onCommit={(value) => onUpdate({ localityHints: value })} />
          <FieldRow label="Retry backoff sec">
            <Input size="small" type="number" min={0} value={task.config.retryBackoffSeconds ?? ''} disabled={readOnly} onChange={(event) => onUpdate({ retryBackoffSeconds: Number(event.target.value) || undefined })} />
          </FieldRow>
          <FieldRow label="Max concurrency">
            <Input size="small" type="number" min={0} value={task.config.maxConcurrency ?? ''} disabled={readOnly} onChange={(event) => onUpdate({ maxConcurrency: Number(event.target.value) || undefined })} />
          </FieldRow>
        </details>
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
          ['CPU', task.resources.cpu ? `${task.resources.cpu} vCPU` : undefined],
          ['Memory', formatStorageGiB(task.resources.memoryGiB)],
          ['GPU', task.resources.gpu ? `${task.resources.gpu}` : undefined],
          ['GPU Memory', formatStorageGiB(task.resources.gpuMemoryGiB)],
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

function artifactDownloadHref(artifact: ArtifactItem, workspaceDir?: string, disposition: 'inline' | 'attachment' = 'attachment') {
  let href: string | null = null;
  const taskId = artifact.taskId || artifact.producerTaskId || artifact.producedBy;
  if (artifact.runId && taskId && artifact.path) {
    href = api.getStaticRunArtifactDownloadUrl(artifact.runId, taskId, artifact.path, workspaceDir || undefined);
  } else if (artifact.sha256) {
    href = api.getArtifactDownloadUrl(artifact.sha256);
  } else if (artifact.uri && /^https?:\/\//i.test(artifact.uri)) {
    href = artifact.uri;
  }
  return href ? withDisposition(href, disposition) : null;
}

function ArtifactsPanel({ task, workspaceDir }: { task: WorkbenchTask; workspaceDir?: string }) {
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
          const openHref = artifactDownloadHref(artifact, workspaceDir, 'inline');
          const downloadHref = artifactDownloadHref(artifact, workspaceDir, 'attachment');
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
  resourceEstimate: { cpu: number; gpu: number; memoryGiB: number };
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
          ['Estimated GPU', resourceEstimate.gpu || '-'],
          ['Estimated Memory', resourceEstimate.memoryGiB ? `${resourceEstimate.memoryGiB.toFixed(2)} GiB` : '-'],
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

interface TaskInspectorProps {
  onOpenNodePanel?: () => void;
}

export default function TaskInspector({ onOpenNodePanel }: TaskInspectorProps) {
  const [activeTab, setActiveTab] = useState<InspectorTab>('overview');
  const {
    selectedNode,
    nodes,
    edges,
    workflowName,
    workflowSaveState,
    workflowDraftError,
    workspaceDir,
    activeRunId,
    selectedRunId,
    staticRuns,
    staticRunEvents,
    isRunning,
    updateNode,
  } = useWorkflowStore();

  const currentSelectedNode = selectedNode
    ? nodes.find((node) => node.id === selectedNode.id) || selectedNode
    : null;
  const visibleRunId = selectedRunId || activeRunId;
  const visibleRun = visibleRunId ? staticRuns.find((run) => run.run_id === visibleRunId) : null;
  const runtime = currentSelectedNode && visibleRun?.task_nodes?.[currentSelectedNode.id]
    ? visibleRun.task_nodes[currentSelectedNode.id]
    : null;
  const events = visibleRunId ? (staticRunEvents[visibleRunId] || []) : [];
  const runTiming = {
    createdAt: formatTimestamp(visibleRun?.created_time),
    submittedAt: formatTimestamp(visibleRun?.submitted_time || visibleRun?.created_time),
    startedAt: formatTimestamp(visibleRun?.started_time || firstTaskStartedTime(visibleRun?.task_nodes)),
  };
  const task = currentSelectedNode ? buildTask(currentSelectedNode, runtime, visibleRunId, edges, nodes, events) : null;
  const isRunMode = Boolean(visibleRunId || isRunning);
  const isSpecReadOnly = isRunMode;

  const dynamicCount = useMemo(
    () => nodes.filter((node) => Boolean((node.data as any).dynamic || (node.data as any).runtimeAppended)).length,
    [nodes],
  );
  const resourceEstimate = useMemo(() => ({
    cpu: nodes.reduce((sum, node) => sum + Number(node.data.resources?.cpu || 0), 0),
    gpu: nodes.reduce((sum, node) => sum + Number(node.data.resources?.gpu || 0), 0),
    memoryGiB: nodes.reduce((sum, node) => sum + Number(node.data.resources?.cpu_mem || 0) / 1024, 0),
  }), [nodes]);
  const latestRun = visibleRun || staticRuns[0];

  const headerTitle = task?.name || workflowName;
  const headerState = task?.state || (workflowSaveState === 'saved_workflow' ? 'validated' : 'draft');
  const headerKind = task ? 'Task' : 'Workflow';
  const modeLabel = isRunMode ? 'Run mode' : 'Design mode';

  function updateTaskData(updates: Record<string, unknown>) {
    if (!task || isSpecReadOnly) return;
    updateNode(task.id, updates as Partial<WorkflowNode['data']>);
  }

  function updateTaskResources(updates: Record<string, number | undefined>) {
    if (!task || !currentSelectedNode || isSpecReadOnly) return;
    updateNode(task.id, {
      resources: {
        ...(currentSelectedNode.data.resources || { cpu: 1, cpu_mem: 0, gpu: 0, gpu_mem: 0 }),
        ...updates,
      },
    });
  }

  return (
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
                readOnly={isSpecReadOnly}
                onOpenNodePanel={onOpenNodePanel}
                onUpdate={updateTaskData}
              />
            )}
            {activeTab === 'resources' && (
              <ResourcesPanel
                task={task}
                readOnly={isSpecReadOnly}
                onUpdate={updateTaskData}
                onUpdateResources={updateTaskResources}
              />
            )}
            {activeTab === 'runtime' && <RuntimePanel task={task} runTiming={runTiming} />}
            {activeTab === 'artifacts' && (
              <ArtifactsPanel task={task} workspaceDir={visibleRun?.workspace_dir || workspaceDir || undefined} />
            )}
          </>
        ) : (
          <WorkflowSummaryPanel
            nodes={nodes}
            edges={edges}
            dynamicCount={dynamicCount}
            resourceEstimate={resourceEstimate}
            workflowSaveState={workflowSaveState}
            workflowDraftError={workflowDraftError}
            latestRun={latestRun}
          />
        )}
      </div>
    </div>
  );
}
