import { ReactNode, useEffect, useMemo, useState } from 'react';
import { Button, Tooltip } from 'antd';
import { DoubleLeftOutlined, DoubleRightOutlined } from '@ant-design/icons';
import { api } from '@/api/client';
import { useWorkflowStore } from '@/stores/workflowStore';
import RuntimeConsole from '@/components/workbench/RuntimeConsole';
import TaskInspector from '@/components/workbench/TaskInspector';
import type { RunArtifact, UnifiedRunEvent, UnifiedRunTaskSnapshot } from '@/types/workflow';
import type {
  RuntimeArtifact,
  RuntimeBreakdownItem,
  RuntimeEvent,
  RuntimeLevel,
  RuntimeLogLine,
  TimelineItem,
} from '@/components/workbench/types';

interface WorkbenchShellProps {
  topBar: ReactNode;
  leftSidebar: ReactNode;
  canvas: ReactNode;
  nodePanel: ReactNode;
  resultsModal: ReactNode;
  runsInspector: ReactNode;
  clusterDrawer: ReactNode;
  onOpenNodePanel?: () => void;
}

function normalizeTaskKey(value?: string | null) {
  return String(value || '')
    .toLowerCase()
    .replace(/^task[_-]/, '')
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '');
}

function formatClock(value?: number | string | null) {
  if (!value) return '-';
  const milliseconds = typeof value === 'number' && value < 1_000_000_000_000 ? value * 1000 : Number(value);
  const date = Number.isFinite(milliseconds) ? new Date(milliseconds) : new Date(String(value));
  if (Number.isNaN(date.getTime())) return '-';
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function formatDuration(seconds?: number | null) {
  if (seconds === undefined || seconds === null) return undefined;
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  return `${String(Math.floor(minutes / 60)).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
}

function durationFromTimes(started?: number | null, finished?: number | null) {
  if (!started || !finished) return undefined;
  return formatDuration(finished - started);
}

function formatFileSize(size?: number | null) {
  if (size === undefined || size === null) return undefined;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  return `${(size / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function shortRunId(value?: string | null) {
  if (!value) return null;
  return value.length > 18 ? `${value.slice(0, 12)}...` : value;
}

function taskState(status?: string | null): TimelineItem['state'] {
  if (status === 'completed') return 'succeeded';
  if (status === 'submitted') return 'queued';
  if (status === 'timed_out' || status === 'interrupted' || status === 'cancelled' || status === 'canceled') return 'failed';
  if (status === 'queued' || status === 'running' || status === 'succeeded' || status === 'failed') return status;
  return 'pending';
}

function eventLevel(event: UnifiedRunEvent): RuntimeLevel {
  const type = event.type.toLowerCase();
  if (type.includes('fail') || type.includes('error') || type.includes('exception') || type.includes('timeout')) return 'error';
  if (type.includes('retry') || type.includes('queue') || type.includes('pending') || type.includes('warn')) return 'warn';
  if (type.includes('heartbeat')) return 'debug';
  return 'info';
}

function eventCategory(type: string): RuntimeEvent['category'] {
  const lower = type.toLowerCase();
  if (lower.includes('schedule') || lower.includes('queue') || lower.includes('retry')) return 'scheduler';
  if (lower.includes('artifact') || lower.includes('file')) return 'artifact';
  if (lower.includes('dynamic') || lower.includes('patch')) return 'dynamic';
  if (lower.includes('worker') || lower.includes('heartbeat')) return 'worker';
  if (lower.includes('resource') || lower.includes('gpu') || lower.includes('cpu')) return 'resource';
  if (lower.includes('inference') || lower.includes('llm')) return 'inference';
  return 'task';
}

function taskLabel(taskId: string | undefined, taskNodes: Record<string, UnifiedRunTaskSnapshot>) {
  if (!taskId) return undefined;
  const task = taskNodes[taskId];
  return task?.task_name || taskId;
}

function eventTaskId(event: UnifiedRunEvent) {
  const data = event.data || {};
  return data.node_id || data.task_id || data.taskId || data.task_spec_id;
}

function eventDetails(event: UnifiedRunEvent) {
  const data = event.data || {};
  if (data.message) return String(data.message);
  if (event.type === 'start_task') {
    const decision = data.schedule_decision || {};
    const selected = decision.selected_node || {};
    const nodeIp = selected.node_ip || data.node_ip;
    const gpu = selected.gpu_id ?? data.gpu_id;
    return `Scheduled${nodeIp ? ` on ${nodeIp}` : ''}${gpu !== undefined && gpu !== null ? ` GPU ${gpu}` : ''}`;
  }
  if (event.type === 'finish_task') {
    return data.duration_ms ? `Finished in ${Math.round(data.duration_ms)} ms` : 'Task completed';
  }
  if (data.pending_reason) return String(data.pending_reason);
  if (data.reason) return String(data.reason);
  if (data.run_status) return `Run status: ${data.run_status}`;
  return event.type.replace(/_/g, ' ');
}

function eventTitle(type: string) {
  return type
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (match) => match.toUpperCase());
}

function runtimeEventsFromRun(events: UnifiedRunEvent[], taskNodes: Record<string, UnifiedRunTaskSnapshot>): RuntimeEvent[] {
  return events.slice().reverse().map((event, index) => {
    const taskId = eventTaskId(event);
    const data = event.data || {};
    return {
      id: `${event.seq || index}-${event.type}`,
      time: formatClock(event.timestamp),
      level: eventLevel(event),
      taskId,
      taskName: data.node_label || data.task_name || taskLabel(taskId, taskNodes),
      event: eventTitle(event.type),
      details: eventDetails(event),
      category: eventCategory(event.type),
    };
  });
}

function timelineFromRun(taskNodes: Record<string, UnifiedRunTaskSnapshot>): TimelineItem[] {
  return Object.entries(taskNodes).map(([taskId, task]) => {
    const selectedNode = task.selected_node || task.schedule_decision?.selected_node;
    const startedTime = task.started_time ?? task.start_time;
    const finishedTime = task.finished_time ?? task.finish_time;
    return {
      taskId,
      taskName: task.task_name || taskId,
      state: taskState(task.status),
      worker: selectedNode?.node_ip || selectedNode?.node_id || undefined,
      startedAt: formatClock(startedTime),
      endedAt: formatClock(finishedTime),
      duration: formatDuration(task.duration_seconds) || durationFromTimes(startedTime, finishedTime),
      queueReason: task.pending_reason || undefined,
    };
  });
}

function logsFromEvents(events: UnifiedRunEvent[], taskNodes: Record<string, UnifiedRunTaskSnapshot>): RuntimeLogLine[] {
  return events.slice().reverse().map((event, index) => {
    const taskId = eventTaskId(event) || 'workflow';
    return {
      id: `log-${event.seq || index}-${event.type}`,
      time: formatClock(event.timestamp),
      level: eventLevel(event),
      taskId,
      taskName: taskLabel(taskId, taskNodes) || 'Workflow',
      message: eventDetails(event),
    };
  });
}

function artifactType(name?: string): RuntimeArtifact['type'] {
  const lower = String(name || '').toLowerCase();
  if (lower.endsWith('.parquet')) return 'parquet';
  if (lower.endsWith('.json')) return 'json';
  if (lower.endsWith('.log')) return 'log';
  if (lower.endsWith('.npy') || lower.endsWith('.pt')) return 'tensor';
  return 'file';
}

function artifactsFromRun(
  artifacts: RunArtifact[],
  taskNodes: Record<string, UnifiedRunTaskSnapshot>,
): RuntimeArtifact[] {
  return artifacts.map((artifact, index) => {
    const taskId = artifact.task_id || artifact.producer_task_id || 'workflow';
    const name = artifact.name || artifact.path?.split('/').pop() || artifact.path || `artifact-${index}`;
    return {
      id: artifact.sha256 || artifact.path || `${taskId}-artifact-${index}`,
      name,
      taskId,
      taskName: taskLabel(taskId, taskNodes) || taskId,
      type: artifactType(name),
      size: formatFileSize(artifact.size),
      createdAt: formatClock(artifact.created_time),
      uri: artifact.sha256
        ? api.getArtifactDownloadUrl(artifact.sha256)
        : artifact.uri || artifact.storage_uri || artifact.storage_path || artifact.path,
    };
  });
}

function taskTypeForRuntimeTask(task: UnifiedRunTaskSnapshot): 'CPU' | 'GPU' | 'I/O' {
  const resources = task.resources || {};
  const label = `${task.task_name || ''} ${task.task_id || ''}`.toLowerCase();
  if (task.task_kind === 'gpu' || Number((resources as any).gpu || 0) > 0 || label.includes('gpu') || label.includes('cuda')) {
    return 'GPU';
  }
  if (task.task_kind === 'io') return 'I/O';
  if (
    label.includes('file')
    || label.includes('io')
    || label.includes('input')
    || label.includes('output')
    || label.includes('artifact')
    || label.includes('load')
    || label.includes('read')
    || label.includes('write')
  ) {
    return 'I/O';
  }
  return 'CPU';
}

function taskTypeBreakdownFromRun(taskNodes: Record<string, UnifiedRunTaskSnapshot>): RuntimeBreakdownItem[] {
  const counts = new Map<string, number>();
  Object.values(taskNodes).forEach((task) => {
    const state = taskState(task.status);
    if (state !== 'queued' && state !== 'pending') return;
    const type = taskTypeForRuntimeTask(task).toLowerCase();
    counts.set(type, (counts.get(type) || 0) + 1);
  });
  const total = Array.from(counts.values()).reduce((sum, value) => sum + value, 0);
  const colors: Record<string, string> = {
    cpu: '#2563eb',
    gpu: '#7c3aed',
    'i/o': '#12b76a',
  };
  return Array.from(counts.entries()).map(([reason, count]) => ({
    reason,
    count,
    percent: total ? Math.round((count / total) * 100) : 0,
    color: colors[reason] || '#667085',
  }));
}

export default function WorkbenchShell({
  topBar,
  leftSidebar,
  canvas,
  nodePanel,
  resultsModal,
  runsInspector,
  clusterDrawer,
  onOpenNodePanel,
}: WorkbenchShellProps) {
  const {
    nodes,
    selectedNode,
    activeRunId,
    selectedRunId,
    staticRuns,
    staticRunEvents,
    setStaticRunEvents,
    selectNode,
  } = useWorkflowStore();
  const [inspectorCollapsed, setInspectorCollapsed] = useState(false);
  const [runtimeArtifacts, setRuntimeArtifacts] = useState<RunArtifact[]>([]);
  const runtimeRunId = selectedRunId || activeRunId;
  const runtimeRun = runtimeRunId ? staticRuns.find((run) => run.run_id === runtimeRunId) : null;
  const runtimeEvents = runtimeRunId ? (staticRunEvents[runtimeRunId] || []) : [];

  useEffect(() => {
    if (!runtimeRun || runtimeEvents.length > 0) return;
    let cancelled = false;
    api.getRunEvents(runtimeRun.run_id)
      .then((result) => {
        if (!cancelled) {
          setStaticRunEvents(runtimeRun.run_id, result.events || []);
        }
      })
      .catch((error) => {
        console.debug('Failed to load runtime console events:', error);
      });
    return () => {
      cancelled = true;
    };
  }, [runtimeEvents.length, runtimeRun, setStaticRunEvents]);

  useEffect(() => {
    if (!runtimeRunId) {
      setRuntimeArtifacts([]);
      return undefined;
    }
    let cancelled = false;
    setRuntimeArtifacts([]);
    api.getRunArtifacts(runtimeRunId)
      .then((result) => {
        if (!cancelled) {
          setRuntimeArtifacts(result.artifacts || []);
        }
      })
      .catch((error) => {
        if (!cancelled) {
          console.debug('Failed to load runtime console artifacts:', error);
          setRuntimeArtifacts([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [runtimeRun?.status, runtimeRunId]);

  const runtimeTaskNodes = runtimeRun?.task_nodes || {};
  const consoleData = useMemo(() => ({
    events: runtimeEventsFromRun(runtimeEvents, runtimeTaskNodes),
    timeline: timelineFromRun(runtimeTaskNodes),
    logs: logsFromEvents(runtimeEvents, runtimeTaskNodes),
    artifacts: artifactsFromRun(runtimeArtifacts, runtimeTaskNodes),
    taskTypeBreakdown: taskTypeBreakdownFromRun(runtimeTaskNodes),
  }), [runtimeArtifacts, runtimeEvents, runtimeTaskNodes]);

  function handleSelectTask(taskId: string) {
    const targetKey = normalizeTaskKey(taskId);
    const node = nodes.find((candidate) => {
      const values = [
        candidate.id,
        candidate.data.label,
        candidate.data.taskRef,
        candidate.data.functionName,
        candidate.data.taskPath,
      ];
      return values.some((value) => {
        const key = normalizeTaskKey(value);
        return Boolean(key) && (key.includes(targetKey) || targetKey.includes(key));
      });
    });
    if (node) {
      selectNode(node);
    }
  }

  return (
    <div className="workbench-shell">
      <div className="workbench-topbar" data-workbench-region="TopBar">
        {topBar}
      </div>

      <div className="workbench-body" data-inspector-collapsed={inspectorCollapsed}>
        <aside className="workbench-left-sidebar" data-workbench-region="LeftSidebar">
          {leftSidebar}
        </aside>

        <main className="workbench-main">
          <section className="workbench-canvas" data-workbench-region="WorkflowCanvas">
            {canvas}
          </section>

          <RuntimeConsole
            runId={shortRunId(runtimeRun?.run_id)}
            runStatus={runtimeRun?.status}
            events={consoleData.events}
            timeline={consoleData.timeline}
            logs={consoleData.logs}
            artifacts={consoleData.artifacts}
            taskTypeBreakdown={consoleData.taskTypeBreakdown}
            selectedTaskId={selectedNode?.id}
            onSelectTask={handleSelectTask}
          />
        </main>

        <aside
          className="workbench-task-inspector"
          data-workbench-region="TaskInspector"
          data-inspector-collapsed={inspectorCollapsed}
        >
          <Tooltip title={inspectorCollapsed ? 'Open inspector' : 'Close inspector'} placement="left">
            <Button
              className="workbench-inspector-collapse-button"
              size="small"
              icon={inspectorCollapsed ? <DoubleLeftOutlined /> : <DoubleRightOutlined />}
              aria-label={inspectorCollapsed ? 'Open inspector' : 'Close inspector'}
              onClick={() => setInspectorCollapsed((value) => !value)}
            />
          </Tooltip>
          {!inspectorCollapsed && <TaskInspector onOpenNodePanel={onOpenNodePanel} />}
        </aside>
      </div>

      {nodePanel}
      {resultsModal}
      {runsInspector}
      {clusterDrawer}
    </div>
  );
}
