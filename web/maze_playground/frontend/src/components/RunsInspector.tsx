import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
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
import type {
  DynamicRunEvent,
  DynamicRunSnapshot,
  DynamicRunStatus,
  RunArtifact,
  RunLogLine,
  UnifiedRunEvent,
  UnifiedRunSnapshot,
  UnifiedRunTaskSnapshot,
} from '@/types/workflow';

function formatJson(value: any) {
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

const { Text, Title } = Typography;

const staticTerminalStatuses = new Set([
  'succeeded',
  'failed',
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

const staticStatusColors: Record<string, string> = {
  created: 'default',
  queued: 'default',
  running: 'processing',
  succeeded: 'success',
  failed: 'error',
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
      status: string;
      run: UnifiedRunSnapshot;
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
  const cpuNum = resources.cpu_num ?? resources.cpu;
  if (cpuNum !== undefined) parts.push(`CPU ${cpuNum}`);
  if (resources.gpu_mem !== undefined) parts.push(`VRAM ${resources.gpu_mem}MB`);
  if (resources.io_num !== undefined) parts.push(`I/O ${resources.io_num}`);
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
    if (event.type === 'task_exception') {
      errors.push(compactText(event.data?.error || event.data?.result || event.data));
    }
  });
  return Array.from(new Set(errors)).slice(0, 5);
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
  return null;
}

function runModeTag(run?: DynamicRunSnapshot | null) {
  const mode = getRunMode(run);
  if (!mode) return null;
  return <Tag color="geekblue">{mode}</Tag>;
}

function isAppRun(run?: UnifiedRunSnapshot | DynamicRunSnapshot | null) {
  return Boolean((run as any)?.metadata?.run_kind === 'app' || (run as any)?.metadata?.app_spec);
}

function appRunName(run?: UnifiedRunSnapshot | null) {
  const metadata = run?.metadata || {};
  return String(metadata.app_name || metadata.workflow_name || (run as any)?.workflow_name || run?.workflow_id || 'Workflow Run');
}

function completedCount(counts?: Record<string, number>) {
  return Number(counts?.completed || counts?.succeeded || 0);
}

function dynamicTaskStatus(status?: string) {
  if (status === 'succeeded') return 'completed';
  if (status === 'queued') return 'submitted';
  return status || 'pending';
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
    mode: run.mode,
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

function staticEventSummary(event: UnifiedRunEvent) {
  const data = event.data || {};
  const taskName = data.node_label || data.task_name || data.node_id || 'task';

  switch (event.type) {
    case 'workflow_started':
      return 'Workflow run started';
    case 'building':
      return data.message || 'Building workflow';
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
    default:
      return event.type;
  }
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
    upsertStaticRun,
    setStaticRunEvents,
    setSelectedRunId,
  } = useWorkflowStore();
  const [runs, setRuns] = useState<UnifiedRunSnapshot[]>([]);
  const [selectedRunKey, setSelectedRunKey] = useState<string | null>(null);
  const [selectedStaticRun, setSelectedStaticRun] = useState<UnifiedRunSnapshot | null>(null);
  const [selectedDynamicRun, setSelectedDynamicRun] = useState<DynamicRunSnapshot | null>(null);
  const [selectedRunArtifacts, setSelectedRunArtifacts] = useState<RunArtifact[]>([]);
  const [selectedRunLogs, setSelectedRunLogs] = useState<RunLogLine[]>([]);
  const [staticEvents, setStaticEvents] = useState<UnifiedRunEvent[]>([]);
  const [dynamicEvents, setDynamicEvents] = useState<DynamicRunEvent[]>([]);
  const [lastAppliedFocusKey, setLastAppliedFocusKey] = useState<string | null>(null);
  const [filterText, setFilterText] = useState('');
  const [loading, setLoading] = useState(false);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [runActionLoading, setRunActionLoading] = useState(false);
  const [artifactPreview, setArtifactPreview] = useState<ArtifactPreviewState | null>(null);
  const [promotingArtifactKey, setPromotingArtifactKey] = useState<string | null>(null);
  const runsRequestRef = useRef<Promise<void> | null>(null);
  const detailRequestRef = useRef<{
    key: string;
    version: number;
    promise: Promise<void>;
  } | null>(null);
  const detailRequestVersionRef = useRef(0);

  const runItems = useMemo<RunItem[]>(() => {
    return runs.map((run): RunItem => {
      if (run.kind === 'dynamic') {
        const adapted = adaptDynamicRun(run);
        return {
          kind: 'dynamic',
          id: run.run_id,
          createdTime: run.created_time,
          updatedTime: run.updated_time,
          status: adapted.status,
          run: adapted,
        };
      }
      return {
        kind: 'static',
        id: run.run_id,
        createdTime: run.created_time,
        updatedTime: run.updated_time,
        status: run.status,
        run,
      };
    }).sort((a, b) => (
      (b.updatedTime || b.createdTime || 0) - (a.updatedTime || a.createdTime || 0)
    ));
  }, [runs]);

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

  const loadRuns = useCallback((silent = false) => {
    if (runsRequestRef.current) {
      return runsRequestRef.current;
    }
    if (!silent) {
      setLoading(true);
    }

    const request = (async () => {
      try {
        const result = await api.getRuns({ limit: 100, detail: false });
        setRuns(result.runs || []);
      } catch (error: any) {
        console.error('Failed to load runs:', error);
        if (!silent) {
          message.error(error.response?.data?.error || 'Failed to load runs');
        }
      } finally {
        runsRequestRef.current = null;
        if (!silent) {
          setLoading(false);
        }
      }
    })();
    runsRequestRef.current = request;
    return request;
  }, []);

  const loadRunDetails = useCallback((
    runId: string,
    kind: RunItem['kind'],
    silent = false,
  ) => {
    const key = `${kind}:${runId}`;
    if (detailRequestRef.current?.key === key) {
      return detailRequestRef.current.promise;
    }
    const version = ++detailRequestVersionRef.current;
    if (!silent) {
      setDetailsLoading(true);
    }

    const request = (async () => {
      try {
        const runResult = await api.getRun(runId);
        const [eventResult, artifactResult, logResult] = await Promise.allSettled([
          api.getRunEvents(runId),
          api.getRunArtifacts(runId),
          api.getRunLogs(runId, { tail: 500 }),
        ]);
        if (version !== detailRequestVersionRef.current) return;

        const run = runResult.run;
        const events = eventResult.status === 'fulfilled'
          ? (eventResult.value.events || []) as UnifiedRunEvent[]
          : [];
        const artifacts = artifactResult.status === 'fulfilled'
          ? (artifactResult.value.artifacts || []) as RunArtifact[]
          : [];
        const logs = logResult.status === 'fulfilled'
          ? (logResult.value.lines || []) as RunLogLine[]
          : [];
        setRuns((current) => [run, ...current.filter((item) => item.run_id !== run.run_id)]);
        setSelectedRunArtifacts(artifacts);
        setSelectedRunLogs(logs);

        if (kind === 'dynamic') {
          setSelectedDynamicRun(adaptDynamicRun(run));
          setDynamicEvents(events as DynamicRunEvent[]);
          setSelectedStaticRun(null);
          setStaticEvents([]);
        } else {
          setSelectedStaticRun(run);
          setStaticEvents(events);
          setSelectedDynamicRun(null);
          setDynamicEvents([]);
          upsertStaticRun(run);
          setStaticRunEvents(runId, events);
          setSelectedRunId(runId);
        }
      } catch (error: any) {
        if (version !== detailRequestVersionRef.current) return;
        console.error('Failed to open run:', error);
        setSelectedRunArtifacts([]);
        setSelectedRunLogs([]);
        if (!silent) {
          message.error(error.response?.data?.error || 'Failed to open run');
        }
      } finally {
        if (detailRequestRef.current?.version === version) {
          detailRequestRef.current = null;
          if (!silent) {
            setDetailsLoading(false);
          }
        }
      }
    })();
    detailRequestRef.current = { key, version, promise: request };
    return request;
  }, [setSelectedRunId, setStaticRunEvents, upsertStaticRun]);

  const selectRun = useCallback((item: RunItem, silent = false) => {
    setSelectedRunKey(runKey(item));
    void loadRunDetails(item.id, item.kind, silent);
  }, [loadRunDetails]);

  const selectedIsLoaded = (item: RunItem) => (
    item.kind === 'static'
      ? selectedStaticRun?.run_id === item.id
      : selectedDynamicRun?.run_id === item.id
  );

  const deleteSelectedRun = async () => {
    if (!selectedItem || selectedItem.kind !== 'dynamic') return;

    try {
      await api.deleteDynamicRun(selectedItem.id);
      setRuns((current) => current.filter((run) => run.run_id !== selectedItem.id));
      message.success('Dynamic run deleted');

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

  const cancelSelectedRun = async () => {
    if (!selectedItem) return;
    setRunActionLoading(true);
    try {
      await api.cancelRun(selectedItem.id, 'Canceled from Maze Playground');
      message.success('Run canceled');
      await loadRuns(true);
      await loadRunDetails(selectedItem.id, selectedItem.kind, true);
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
      await loadRunDetails(result.runId, 'static', true);
    } catch (error: any) {
      console.error('Failed to retry run:', error);
      message.error(error.response?.data?.error || 'Failed to retry run');
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
      void loadRunDetails(selectedItem.id, selectedItem.kind, true);
      void loadRuns(true);
    }, selectedItem.kind === 'static' ? 1200 : 1000);

    return () => window.clearInterval(timer);
  }, [loadRunDetails, loadRuns, open, selectedItem]);

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
    const uri = artifact.uri || artifact.storage_uri;
    if (uri && /^https?:\/\//i.test(uri)) {
      return uri;
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

  const renderRuntimeEvidence = ({
    title,
    run,
    nodes,
    events = [],
  }: {
    title: string;
    run?: UnifiedRunSnapshot | DynamicRunSnapshot | null;
    nodes: any[];
    events?: DynamicRunEvent[];
  }) => {
    const placements = collectPlacementSummary(nodes);
    const resources = collectResourceSummary(nodes);
    const sandboxes = collectSandboxSummary(nodes, events, run as DynamicRunSnapshot);
    const errors = collectRunErrors(run, nodes, events);
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
              onClick={() => loadRunDetails(selectedStaticRun.run_id, 'static')}
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

        <div>
          <Title level={5}>Final Result</Title>
          {selectedStaticRun.final_result == null && selectedStaticRun.result_summary == null ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No final result recorded" />
          ) : (
            renderJsonValue(selectedStaticRun.final_result ?? selectedStaticRun.result_summary)
          )}
        </div>

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
                        <Tag color={staticStatusColors[node.status] || 'default'}>{node.status}</Tag>
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
              onClick={() => loadRunDetails(selectedDynamicRun.run_id, 'dynamic')}
              loading={detailsLoading}
            >
              Refresh
            </Button>
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
        })}

        {renderRunLogs()}

        {renderArtifacts()}

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
