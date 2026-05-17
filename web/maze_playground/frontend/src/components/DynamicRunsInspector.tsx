import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
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
  NodeIndexOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import { api } from '@/api/client';
import type { DynamicRunEvent, DynamicRunSnapshot, DynamicRunStatus } from '@/types/workflow';

const { Text, Title } = Typography;

const terminalStatuses = new Set<DynamicRunStatus>([
  'finalized',
  'failed',
  'canceled',
  'timed_out',
  'interrupted',
]);

const statusColors: Record<DynamicRunStatus, string> = {
  created: 'default',
  running: 'processing',
  finalized: 'success',
  failed: 'error',
  canceled: 'orange',
  timed_out: 'volcano',
  interrupted: 'magenta',
};

interface DynamicRunsInspectorProps {
  open: boolean;
  onClose: () => void;
}

function shortId(value?: string) {
  if (!value) return '';
  return value.length > 12 ? `${value.slice(0, 8)}...` : value;
}

function formatTime(value?: number | null) {
  if (!value) return '-';
  return new Date(value * 1000).toLocaleString();
}

function eventSummary(event: DynamicRunEvent) {
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

export default function DynamicRunsInspector({ open, onClose }: DynamicRunsInspectorProps) {
  const [runs, setRuns] = useState<DynamicRunSnapshot[]>([]);
  const [selectedRun, setSelectedRun] = useState<DynamicRunSnapshot | null>(null);
  const [events, setEvents] = useState<DynamicRunEvent[]>([]);
  const [queryRunId, setQueryRunId] = useState('');
  const [loading, setLoading] = useState(false);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [cleanupLoading, setCleanupLoading] = useState(false);

  const selectedTaskNodes = useMemo(() => {
    if (!selectedRun?.task_nodes) return [];
    return Object.values(selectedRun.task_nodes);
  }, [selectedRun]);

  const selectedEdges = selectedRun?.graph?.edges || [];

  const loadRuns = async () => {
    setLoading(true);
    try {
      const result = await api.getDynamicRuns({ limit: 100 });
      setRuns(result.runs || []);
    } catch (error: any) {
      console.error('Failed to load dynamic runs:', error);
      message.error(error.response?.data?.error || 'Failed to load dynamic runs');
    } finally {
      setLoading(false);
    }
  };

  const loadRunDetails = async (runId: string) => {
    const id = runId.trim();
    if (!id) return;

    setDetailsLoading(true);
    try {
      const [runResult, eventResult] = await Promise.all([
        api.getDynamicRun(id),
        api.getDynamicRunEvents(id),
      ]);
      setSelectedRun(runResult.run);
      setEvents(eventResult.events || []);
      setQueryRunId(runResult.run.run_id);
    } catch (error: any) {
      console.error('Failed to load dynamic run:', error);
      message.error(error.response?.data?.error || 'Failed to load dynamic run');
    } finally {
      setDetailsLoading(false);
    }
  };

  const deleteSelectedRun = async () => {
    if (!selectedRun) return;

    try {
      await api.deleteDynamicRun(selectedRun.run_id);
      message.success('Dynamic run deleted');
      setSelectedRun(null);
      setEvents([]);
      await loadRuns();
    } catch (error: any) {
      console.error('Failed to delete dynamic run:', error);
      message.error(error.response?.data?.error || 'Failed to delete dynamic run');
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
      loadRuns();
    }
  }, [open]);

  return (
    <Drawer
      title={
        <Space>
          <NodeIndexOutlined />
          Dynamic Runs
        </Space>
      }
      open={open}
      onClose={onClose}
      width={980}
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={loadRuns} loading={loading}>
            Refresh
          </Button>
          <Button onClick={runDryCleanup} loading={cleanupLoading}>
            Cleanup Dry Run
          </Button>
        </Space>
      }
    >
      <div style={{ display: 'grid', gridTemplateColumns: '320px minmax(0, 1fr)', gap: '20px', height: '100%' }}>
        <div style={{ minWidth: 0 }}>
          <Input.Search
            placeholder="Enter dynamic run id"
            enterButton={<SearchOutlined />}
            value={queryRunId}
            onChange={(event) => setQueryRunId(event.target.value)}
            onSearch={loadRunDetails}
            loading={detailsLoading}
          />

          <List
            size="small"
            loading={loading}
            dataSource={runs}
            locale={{ emptyText: 'No dynamic runs found' }}
            style={{ marginTop: '16px', maxHeight: 'calc(100vh - 190px)', overflow: 'auto' }}
            renderItem={(run) => (
              <List.Item
                onClick={() => loadRunDetails(run.run_id)}
                style={{
                  cursor: 'pointer',
                  padding: '10px 8px',
                  background: selectedRun?.run_id === run.run_id ? '#f0f7ff' : undefined,
                  borderRadius: '6px',
                }}
              >
                <Space direction="vertical" size={2} style={{ width: '100%' }}>
                  <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                    <Text strong>{shortId(run.run_id)}</Text>
                    <Tag color={statusColors[run.status] || 'default'}>{run.status}</Tag>
                  </Space>
                  <Text type="secondary" style={{ fontSize: '12px' }}>
                    {formatTime(run.created_time)}
                  </Text>
                  <Text type="secondary" style={{ fontSize: '12px' }}>
                    {run.task_counts?.total || 0} task(s), {run.event_count || 0} event(s)
                  </Text>
                </Space>
              </List.Item>
            )}
          />
        </div>

        <div style={{ minWidth: 0, overflow: 'auto', paddingRight: '4px' }}>
          {!selectedRun ? (
            <Empty description="Select a dynamic run or search by run id" />
          ) : (
            <Space direction="vertical" size={16} style={{ width: '100%' }}>
              <Space style={{ justifyContent: 'space-between', width: '100%' }} align="start">
                <div>
                  <Title level={4} style={{ margin: 0 }}>Run {shortId(selectedRun.run_id)}</Title>
                  <Text copyable style={{ fontSize: '12px' }}>{selectedRun.run_id}</Text>
                </div>
                <Space>
                  <Button icon={<ReloadOutlined />} onClick={() => loadRunDetails(selectedRun.run_id)} loading={detailsLoading}>
                    Refresh
                  </Button>
                  <Popconfirm
                    title="Delete this dynamic run?"
                    description="Only terminal runs can be deleted."
                    onConfirm={deleteSelectedRun}
                    okText="Delete"
                    okButtonProps={{ danger: true }}
                  >
                    <Button
                      danger
                      icon={<DeleteOutlined />}
                      disabled={!terminalStatuses.has(selectedRun.status)}
                    >
                      Delete
                    </Button>
                  </Popconfirm>
                </Space>
              </Space>

              <Descriptions bordered size="small" column={2}>
                <Descriptions.Item label="Status">
                  <Tag color={statusColors[selectedRun.status] || 'default'}>{selectedRun.status}</Tag>
                </Descriptions.Item>
                <Descriptions.Item label="Schema">v{selectedRun.schema_version || 1}</Descriptions.Item>
                <Descriptions.Item label="Created">{formatTime(selectedRun.created_time)}</Descriptions.Item>
                <Descriptions.Item label="Updated">{formatTime(selectedRun.updated_time)}</Descriptions.Item>
                <Descriptions.Item label="Finished">{formatTime(selectedRun.finished_time)}</Descriptions.Item>
                <Descriptions.Item label="Timeout">{selectedRun.timeout_seconds ?? '-'}</Descriptions.Item>
                <Descriptions.Item label="Cancel Reason">{selectedRun.cancel_reason || '-'}</Descriptions.Item>
                <Descriptions.Item label="Failure Reason">{selectedRun.failure_reason || '-'}</Descriptions.Item>
              </Descriptions>

              <Space wrap>
                {['total', 'pending', 'submitted', 'running', 'completed', 'failed'].map((key) => (
                  <div
                    key={key}
                    style={{
                      width: '110px',
                      border: '1px solid #f0f0f0',
                      borderRadius: '6px',
                      padding: '8px 10px',
                      background: '#fff',
                    }}
                  >
                    <Statistic title={key} value={selectedRun.task_counts?.[key] || 0} />
                  </div>
                ))}
              </Space>

              <div>
                <Title level={5}>Task Graph</Title>
                {selectedTaskNodes.length === 0 ? (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No appended tasks" />
                ) : (
                  <List
                    size="small"
                    bordered
                    dataSource={selectedTaskNodes}
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
                            <Text type="secondary" style={{ fontSize: '12px' }}>
                              Parents: {parents.length ? parents.map(shortId).join(', ') : 'none'}
                            </Text>
                          </Space>
                        </List.Item>
                      );
                    }}
                  />
                )}
                {selectedEdges.length > 0 && (
                  <Text type="secondary" style={{ display: 'block', marginTop: '8px', fontSize: '12px' }}>
                    Edges: {selectedEdges.map((edge) => `${shortId(edge.source)} -> ${shortId(edge.target)}`).join(', ')}
                  </Text>
                )}
              </div>

              <Divider style={{ margin: '4px 0' }} />

              <div>
                <Title level={5}>Event Log</Title>
                {events.length === 0 ? (
                  <Alert type="info" showIcon message="No events recorded for this run" />
                ) : (
                  <List
                    size="small"
                    bordered
                    dataSource={events}
                    renderItem={(event) => (
                      <List.Item>
                        <Space direction="vertical" size={2} style={{ width: '100%' }}>
                          <Space wrap>
                            <Text type="secondary">#{event.seq || '-'}</Text>
                            <Tag>{event.type}</Tag>
                            {event.data?.run_status && <Tag color="default">{String(event.data.run_status)}</Tag>}
                            <Text type="secondary" style={{ fontSize: '12px' }}>
                              {event.timestamp ? new Date(event.timestamp).toLocaleString() : '-'}
                            </Text>
                          </Space>
                          <Text>{eventSummary(event)}</Text>
                        </Space>
                      </List.Item>
                    )}
                  />
                )}
              </div>
            </Space>
          )}
        </div>
      </div>
    </Drawer>
  );
}
