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
  Popconfirm,
  Space,
  Statistic,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  DeleteOutlined,
  HistoryOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import { api } from '@/api/client';
import { useWorkflowStore } from '@/stores/workflowStore';
import type {
  DynamicRunEvent,
  DynamicRunSnapshot,
  DynamicRunStatus,
  StaticWorkflowRunEvent,
  StaticWorkflowRunSnapshot,
  StaticWorkflowRunStatus,
} from '@/types/workflow';
import ReActRuntimeCanvas, {
  buildAgentTrace,
  formatJson,
  hasAgentTrace,
  type AgentTraceStep,
} from './ReActRuntimeCanvas';

const { Text, Title } = Typography;

const staticTerminalStatuses = new Set<StaticWorkflowRunStatus>([
  'completed',
  'failed',
  'canceled',
  'interrupted',
]);

const dynamicTerminalStatuses = new Set<DynamicRunStatus>([
  'finalized',
  'failed',
  'canceled',
  'timed_out',
  'interrupted',
]);

const staticStatusColors: Record<StaticWorkflowRunStatus, string> = {
  running: 'processing',
  completed: 'success',
  failed: 'error',
  canceled: 'orange',
  interrupted: 'magenta',
};

const dynamicStatusColors: Record<DynamicRunStatus, string> = {
  created: 'default',
  running: 'processing',
  finalized: 'success',
  failed: 'error',
  canceled: 'orange',
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

function getRunMode(run?: DynamicRunSnapshot | null) {
  if (!run) return null;
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
  const [staticEvents, setStaticEvents] = useState<StaticWorkflowRunEvent[]>([]);
  const [dynamicEvents, setDynamicEvents] = useState<DynamicRunEvent[]>([]);
  const [lastAppliedFocusKey, setLastAppliedFocusKey] = useState<string | null>(null);
  const [filterText, setFilterText] = useState('');
  const [loading, setLoading] = useState(false);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [cleanupLoading, setCleanupLoading] = useState(false);

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
        ? item.run.workflow_name || 'Workflow Run'
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

  const loadRuns = useCallback(async (silent = false) => {
    if (!silent) {
      setLoading(true);
    }

    try {
      const [staticResult, dynamicResult] = await Promise.all([
        api.getStaticWorkflowRuns({
          workspaceDir: workspaceDir || undefined,
          limit: 100,
        }),
        api.getDynamicRuns({ limit: 100 }),
      ]);
      setStaticRuns(staticResult.runs || []);
      setDynamicRuns(dynamicResult.runs || []);
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
  }, [setStaticRuns, workspaceDir]);

  const loadStaticRunDetails = useCallback(async (runId: string, silent = false) => {
    if (!silent) {
      setDetailsLoading(true);
    }

    try {
      const [runResult, eventResult] = await Promise.all([
        api.getStaticWorkflowRun(runId, workspaceDir || undefined),
        api.getStaticWorkflowRunEvents(runId, workspaceDir || undefined),
      ]);
      setSelectedStaticRun(runResult.run);
      setStaticEvents(eventResult.events || []);
      setSelectedDynamicRun(null);
      setDynamicEvents([]);
      upsertStaticRun(runResult.run);
      setStaticRunEvents(runId, eventResult.events || []);
    } catch (error: any) {
      console.error('Failed to open workflow run:', error);
      if (!silent) {
        message.error(error.response?.data?.error || 'Failed to open workflow run');
      }
    } finally {
      if (!silent) {
        setDetailsLoading(false);
      }
    }
  }, [setStaticRunEvents, upsertStaticRun, workspaceDir]);

  const loadDynamicRunDetails = useCallback(async (runId: string, silent = false) => {
    if (!silent) {
      setDetailsLoading(true);
    }

    try {
      const [runResult, eventResult] = await Promise.all([
        api.getDynamicRun(runId),
        api.getDynamicRunEvents(runId),
      ]);
      setSelectedDynamicRun(runResult.run);
      setDynamicEvents(eventResult.events || []);
      setSelectedStaticRun(null);
      setStaticEvents([]);
      setDynamicRuns((current) => [
        runResult.run,
        ...current.filter((run) => run.run_id !== runResult.run.run_id),
      ]);
    } catch (error: any) {
      console.error('Failed to open dynamic run:', error);
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
      message.info(`Cleanup dry run matched ${result.cleanup?.matched_count || 0} run(s)`);
    } catch (error: any) {
      console.error('Failed to run cleanup dry run:', error);
      message.error(error.response?.data?.error || 'Failed to run cleanup dry run');
    } finally {
      setCleanupLoading(false);
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
            ? item.run.workflow_name || 'Workflow Run'
            : `Dynamic ${shortId(item.id)}`;
          const detail = isStatic
            ? `${item.run.task_counts?.completed || 0}/${item.run.task_counts?.total || 0} completed`
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
                    {isStatic ? 'workflow' : 'dynamic'}
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

  const renderStaticDetails = () => {
    if (!selectedStaticRun) {
      return <Empty description="Select a workflow run" />;
    }

    return (
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Space style={{ justifyContent: 'space-between', width: '100%' }} align="start">
          <div>
            <Title level={4} style={{ margin: 0 }}>
              {selectedStaticRun.workflow_name || 'Workflow Run'}
            </Title>
            <Space size={6} wrap>
              <Tag color="blue">workflow</Tag>
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
          <Descriptions.Item label="Error" span={2}>{selectedStaticRun.error || '-'}</Descriptions.Item>
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
              <Statistic title={key} value={selectedStaticRun.task_counts?.[key] || 0} />
            </div>
          ))}
        </Space>

        <div>
          <Title level={5}>Task Nodes</Title>
          {selectedStaticTaskNodes.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No task nodes recorded" />
          ) : (
            <List
              size="small"
              bordered
              dataSource={selectedStaticTaskNodes}
              renderItem={(node) => (
                <List.Item>
                  <Space direction="vertical" size={2} style={{ width: '100%' }}>
                    <Space wrap>
                      <Text strong>{node.label || node.task_name || node.node_id}</Text>
                      <Tag color={staticStatusColors[node.status as StaticWorkflowRunStatus] || 'default'}>{node.status}</Tag>
                      {node.category && <Tag>{node.category}</Tag>}
                      {node.maze_task_id && <Tag>{shortId(node.maze_task_id)}</Tag>}
                    </Space>
                    {node.error && (
                      <Text type="danger" style={{ fontSize: 12 }}>{node.error}</Text>
                    )}
                    {node.artifacts && node.artifacts.length > 0 && (
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {node.artifacts.length} artifact(s)
                      </Text>
                    )}
                  </Space>
                </List.Item>
              )}
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
              onClick={() => loadDynamicRunDetails(selectedDynamicRun.run_id)}
              loading={detailsLoading}
            >
              Refresh
            </Button>
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
          <Descriptions.Item label="Cancel Reason">{selectedDynamicRun.cancel_reason || '-'}</Descriptions.Item>
          <Descriptions.Item label="Failure Reason">{selectedDynamicRun.failure_reason || '-'}</Descriptions.Item>
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
                {agentTrace.started.llm_task && (
                  <Tag color="cyan">LLM {String(agentTrace.started.llm_task)}</Tag>
                )}
              </Space>
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
                return (
                  <List.Item>
                    <Space direction="vertical" size={2} style={{ width: '100%' }}>
                      <Space wrap>
                        <Text strong>{node.task_name || 'task'}</Text>
                        <Tag>{shortId(node.task_id)}</Tag>
                        <Tag color="blue">{node.status}</Tag>
                        {node.request_id && <Tag color="cyan">{node.request_id}</Tag>}
                      </Space>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        Parents: {parents.length ? parents.map(shortId).join(', ') : 'none'}
                      </Text>
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

  return (
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
            Cleanup Dry Run
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
  );
}
