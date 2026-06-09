import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Drawer, Progress, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { ReloadOutlined } from '@ant-design/icons';
import { api } from '@/api/client';
import type {
  ClusterQueueTask,
  ClusterQueuesResponse,
  ClusterResourceNode,
  ClusterResourcesResponse,
} from '@/types/workflow';

const { Text } = Typography;

interface ClusterResourcesDrawerProps {
  open: boolean;
  onClose: () => void;
}

function formatBytes(value?: number | null): string {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return '0 B';
  }

  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let current = bytes;
  let unitIndex = 0;
  while (current >= 1024 && unitIndex < units.length - 1) {
    current /= 1024;
    unitIndex += 1;
  }

  return `${current >= 10 ? current.toFixed(1) : current.toFixed(2)} ${units[unitIndex]}`;
}

function formatGpuMemory(value?: number | null): string {
  const mib = Number(value || 0);
  if (!Number.isFinite(mib) || mib <= 0) {
    return '0 MiB';
  }
  if (mib >= 1024) {
    return `${(mib / 1024).toFixed(1)} GiB`;
  }
  return `${Math.round(mib)} MiB`;
}

function percent(available: number, total: number): number {
  if (!total || total <= 0) {
    return 0;
  }
  const used = Math.max(total - available, 0);
  return Math.min(100, Math.round((used / total) * 100));
}

function shortId(value?: string | null): string {
  return value ? `${value.slice(0, 10)}...` : '-';
}

function formatTime(value?: number | null): string {
  if (!value) return '-';
  return new Date(value * 1000).toLocaleString();
}

function formatDurationSeconds(value?: number | null): string {
  if (value === undefined || value === null) return '-';
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return '-';
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < 60) return `${seconds.toFixed(2)}s`;
  return `${Math.floor(seconds / 60)}m ${(seconds % 60).toFixed(0)}s`;
}

function cpuTotals(node: ClusterResourceNode) {
  const total = node.resources?.cpu?.total ?? node.ray_resources?.CPU ?? 0;
  const available = node.resources?.cpu?.available;
  return { total, available };
}

function gpuTotals(node: ClusterResourceNode) {
  const total = node.resources?.gpu?.total_count ?? node.ray_resources?.GPU ?? 0;
  const available = node.resources?.gpu?.available_count;
  return { total, available };
}

type QueueRow = ClusterQueueTask & {
  queue_bucket: string;
  row_key: string;
};

function queueStatusColor(status?: string): string {
  if (status === 'running') return 'processing';
  if (status === 'retrying') return 'orange';
  if (status === 'pending') return 'volcano';
  if (status === 'ready') return 'blue';
  return 'default';
}

function compactJson(value: any): string {
  if (value === undefined || value === null || value === '') return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function errorSummary(error: any): string {
  if (!error) return '';
  if (typeof error === 'string') return error;
  return String(error.message || error.error || error.error_type || error.kind || compactJson(error));
}

function candidateRejectSummary(task: ClusterQueueTask): string[] {
  const candidates = task.schedule_decision?.candidate_nodes || [];
  return candidates
    .filter((candidate) => candidate.reject_reasons && candidate.reject_reasons.length > 0)
    .slice(0, 3)
    .map((candidate) => (
      `${candidate.node_ip || shortId(candidate.node_id)}: ${candidate.reject_reasons?.join(', ')}`
    ));
}

export default function ClusterResourcesDrawer({ open, onClose }: ClusterResourcesDrawerProps) {
  const [data, setData] = useState<ClusterResourcesResponse | null>(null);
  const [queues, setQueues] = useState<ClusterQueuesResponse['queues'] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [queueError, setQueueError] = useState<string | null>(null);

  const loadResources = useCallback(async () => {
    setLoading(true);
    const [resourcesResult, queuesResult] = await Promise.allSettled([
      api.getClusterResources(),
      api.getClusterQueues(),
    ]);

    if (resourcesResult.status === 'fulfilled') {
      setData(resourcesResult.value);
      setError(null);
    } else {
      console.error('Failed to load cluster resources:', resourcesResult.reason);
      setError(
        resourcesResult.reason?.response?.data?.error
        || resourcesResult.reason?.message
        || 'Failed to load cluster resources',
      );
    }

    if (queuesResult.status === 'fulfilled') {
      setQueues(queuesResult.value.queues);
      setQueueError(null);
    } else {
      console.error('Failed to load cluster queues:', queuesResult.reason);
      setQueueError(
        queuesResult.reason?.response?.data?.error
        || queuesResult.reason?.message
        || 'Failed to load cluster queues',
      );
    }

    setLoading(false);
  }, []);

  useEffect(() => {
    if (!open) {
      return;
    }

    loadResources();
    const timer = window.setInterval(loadResources, 3000);
    return () => window.clearInterval(timer);
  }, [loadResources, open]);

  const nodes = useMemo(() => {
    const registered = data?.cluster?.nodes || [];
    const unregistered = data?.cluster?.unregistered_ray_nodes || [];
    return [...registered, ...unregistered];
  }, [data]);

  const totals = useMemo(() => nodes.reduce(
    (acc, node) => {
      const cpu = cpuTotals(node);
      const gpu = gpuTotals(node);
      acc.nodes += 1;
      acc.registered += node.registered ? 1 : 0;
      acc.cpu += cpu.total || 0;
      acc.gpu += gpu.total || 0;
      return acc;
    },
    { nodes: 0, registered: 0, cpu: 0, gpu: 0 },
  ), [nodes]);

  const queueRows = useMemo<QueueRow[]>(() => {
    if (!queues) return [];

    const buildRows = (bucket: string, tasks: ClusterQueueTask[] = []) => tasks.map((task, index) => ({
      ...task,
      queue_bucket: bucket,
      row_key: `${bucket}:${task.workflow_id}:${task.task_id}:${index}`,
    }));

    return [
      ...buildRows('ready', queues.ready_tasks),
      ...buildRows('pending', queues.pending_tasks),
      ...buildRows('retrying', queues.retrying_tasks),
      ...buildRows('running', queues.running_tasks),
    ];
  }, [queues]);

  const columns: ColumnsType<ClusterResourceNode> = [
    {
      title: 'Node',
      dataIndex: 'node_ip',
      key: 'node',
      width: 220,
      render: (_, node) => (
        <Space direction="vertical" size={2}>
          <Space size={6}>
            <Text strong>{node.node_ip || '-'}</Text>
            <Tag color={node.role === 'head' ? 'blue' : 'default'}>{node.role}</Tag>
          </Space>
          <Text type="secondary" style={{ fontSize: 12 }} title={node.node_id}>
            {shortId(node.node_id)}
          </Text>
        </Space>
      ),
    },
    {
      title: 'State',
      key: 'state',
      width: 150,
      render: (_, node) => (
        <Space size={6} wrap>
          <Tag color={node.alive ? 'green' : 'red'}>{node.alive ? 'alive' : 'dead'}</Tag>
          <Tag color={node.registered ? 'geekblue' : 'orange'}>
            {node.registered ? 'registered' : 'ray only'}
          </Tag>
        </Space>
      ),
    },
    {
      title: 'CPU',
      key: 'cpu',
      width: 180,
      render: (_, node) => {
        const { total, available } = cpuTotals(node);
        const usedPercent = available === undefined ? 0 : percent(available, total);
        return (
          <Space direction="vertical" size={2} style={{ width: '100%' }}>
            <Text>{available === undefined ? `${total} total` : `${available} / ${total} available`}</Text>
            <Progress percent={usedPercent} showInfo={false} size="small" />
          </Space>
        );
      },
    },
    {
      title: 'CPU Memory',
      key: 'memory',
      width: 180,
      render: (_, node) => {
        const total = node.resources?.cpu_mem?.total ?? node.ray_resources?.memory ?? 0;
        const available = node.resources?.cpu_mem?.available;
        const usedPercent = available === undefined ? 0 : percent(available, total);
        return (
          <Space direction="vertical" size={2} style={{ width: '100%' }}>
            <Text>{available === undefined ? `${formatBytes(total)} total` : `${formatBytes(available)} / ${formatBytes(total)}`}</Text>
            <Progress percent={usedPercent} showInfo={false} size="small" />
          </Space>
        );
      },
    },
    {
      title: 'GPU',
      key: 'gpu',
      render: (_, node) => {
        const { total, available } = gpuTotals(node);
        const devices = node.resources?.gpu?.devices || [];
        if (!total) {
          return <Text type="secondary">No GPU</Text>;
        }

        return (
          <Space direction="vertical" size={4}>
            <Text>{available === undefined ? `${total} total` : `${available} / ${total} available`}</Text>
            <Space size={4} wrap>
              {devices.length > 0 ? devices.map((device) => (
                <Tag key={String(device.gpu_id)} color={device.available_count > 0 ? 'green' : 'volcano'}>
                  GPU {device.gpu_id}: {formatGpuMemory(device.available_memory)} / {formatGpuMemory(device.total_memory)}
                </Tag>
              )) : (
                <Tag color="default">{total} Ray GPU</Tag>
              )}
            </Space>
          </Space>
        );
      },
    },
    {
      title: 'Sandbox',
      key: 'sandbox',
      width: 220,
      render: (_, node) => (
        <Space size={4} wrap>
          <Tag color={node.capabilities?.workspace_sandbox ? 'green' : 'default'}>workspace</Tag>
          <Tag
            color={node.capabilities?.docker_sandbox ? 'green' : 'orange'}
            title={node.capabilities?.docker_reason || undefined}
          >
            docker {node.capabilities?.docker_sandbox ? 'ready' : 'off'}
          </Tag>
        </Space>
      ),
    },
  ];

  const queueColumns: ColumnsType<QueueRow> = [
    {
      title: 'Queue',
      dataIndex: 'queue_bucket',
      key: 'queue_bucket',
      width: 110,
      render: (value: string) => <Tag color={queueStatusColor(value)}>{value}</Tag>,
    },
    {
      title: 'Task',
      key: 'task',
      width: 230,
      render: (_, task) => (
        <Space direction="vertical" size={2}>
          <Text copyable={{ text: task.task_id }} strong>{shortId(task.task_id)}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>{shortId(task.workflow_id)}</Text>
        </Space>
      ),
    },
    {
      title: 'Attempt',
      key: 'attempt',
      width: 120,
      render: (_, task) => (
        <Space direction="vertical" size={2}>
          <Text>{task.attempt ?? 0} / {task.max_retries ?? 0}</Text>
          {task.retry_wait_seconds ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              wait {formatDurationSeconds(task.retry_wait_seconds)}
            </Text>
          ) : null}
          {task.next_eligible_time ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              next {formatTime(task.next_eligible_time)}
            </Text>
          ) : null}
        </Space>
      ),
    },
    {
      title: 'Placement',
      key: 'placement',
      width: 190,
      render: (_, task) => {
        const selected = task.selected_node || task.schedule_decision?.selected_node;
        return (
          <Space direction="vertical" size={2}>
            <Text>{selected?.node_ip || '-'}</Text>
            <Space size={4} wrap>
              {selected?.gpu_id !== undefined && selected?.gpu_id !== null && <Tag color="gold">GPU {selected.gpu_id}</Tag>}
              {task.resources?.cpu !== undefined && <Tag>CPU {task.resources.cpu}</Tag>}
              {task.resources?.gpu !== undefined && <Tag>GPU req {task.resources.gpu}</Tag>}
            </Space>
          </Space>
        );
      },
    },
    {
      title: 'Timing',
      key: 'timing',
      width: 170,
      render: (_, task) => (
        <Space direction="vertical" size={2}>
          <Text>{task.elapsed_seconds !== undefined ? formatDurationSeconds(task.elapsed_seconds) : formatTime(task.next_eligible_time)}</Text>
          {task.timeout_seconds !== undefined && task.timeout_seconds !== null && (
            <Text type="secondary" style={{ fontSize: 12 }}>timeout {formatDurationSeconds(task.timeout_seconds)}</Text>
          )}
        </Space>
      ),
    },
    {
      title: 'Reason',
      key: 'reason',
      render: (_, task) => {
        const rejects = candidateRejectSummary(task);
        const reason = task.pending_reason
          || task.schedule_decision?.reason
          || errorSummary(task.last_error)
          || '-';
        return (
          <Space direction="vertical" size={2} style={{ maxWidth: 360 }}>
            <Text style={{ fontSize: 12 }} ellipsis={{ tooltip: reason }}>{reason}</Text>
            {task.runtime_status && (
              <Text type="secondary" style={{ fontSize: 12 }}>runtime {task.runtime_status}</Text>
            )}
            {rejects.map((reject) => (
              <Text key={reject} type="secondary" style={{ fontSize: 12 }} ellipsis={{ tooltip: reject }}>
                {reject}
              </Text>
            ))}
            {task.last_error && reason !== errorSummary(task.last_error) && (
              <Text type="danger" style={{ fontSize: 12 }} ellipsis={{ tooltip: errorSummary(task.last_error) }}>
                {errorSummary(task.last_error)}
              </Text>
            )}
          </Space>
        );
      },
    },
  ];

  return (
    <Drawer
      title="Cluster Resources"
      open={open}
      onClose={onClose}
      width="min(960px, 100vw)"
      extra={(
        <Button icon={<ReloadOutlined />} onClick={loadResources} loading={loading}>
          Refresh
        </Button>
      )}
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {error && <Alert type="error" message={error} showIcon />}
        {queueError && <Alert type="warning" message={queueError} showIcon />}
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
            gap: 12,
          }}
        >
          {[
            ['Nodes', totals.nodes],
            ['Registered', `${totals.registered}/${totals.nodes}`],
            ['CPU', totals.cpu],
            ['GPU', totals.gpu],
          ].map(([label, value]) => (
            <div
              key={label}
              style={{
                border: '1px solid #f0f0f0',
                borderRadius: 6,
                padding: '10px 12px',
                minHeight: 68,
              }}
            >
              <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>{label}</Text>
              <Text strong style={{ fontSize: 22 }}>{value}</Text>
            </div>
          ))}
        </div>
        {(data?.cluster?.unregistered_ray_nodes?.length || 0) > 0 && (
          <Alert
            type="warning"
            showIcon
            message="Some Ray nodes are not registered with Maze"
            description="Run maze start --worker --addr <head-ip>:8000 on each worker, or use the distributed GPU smoke registration helper."
          />
        )}
        <Table
          rowKey={(node) => `${node.node_id}-${node.registered ? 'maze' : 'ray'}`}
          loading={loading && !data}
          columns={columns}
          dataSource={nodes}
          pagination={false}
          size="middle"
          scroll={{ x: 880 }}
        />
        <Space style={{ justifyContent: 'space-between', width: '100%' }} align="center">
          <Space wrap>
            <Text strong>Scheduler Queues</Text>
            {queues?.scheduling_policy && <Tag color="geekblue">{queues.scheduling_policy}</Tag>}
            {queues?.snapshot_time && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {formatTime(queues.snapshot_time)}
              </Text>
            )}
          </Space>
          <Space size={6} wrap>
            {[
              ['ready', queues?.counts.ready || 0],
              ['pending', queues?.counts.pending || 0],
              ['retrying', queues?.counts.retrying || 0],
              ['running', queues?.counts.running || 0],
              ['total', queues?.counts.total_queued || 0],
            ].map(([label, value]) => (
              <Tag key={label} color={queueStatusColor(String(label))}>
                {label}: {value}
              </Tag>
            ))}
            {(queues?.stopped_workflow_ids?.length || 0) > 0 && (
              <Tag color="default">stopped: {queues?.stopped_workflow_ids?.length}</Tag>
            )}
          </Space>
        </Space>
        <Table
          rowKey="row_key"
          loading={loading && !queues}
          columns={queueColumns}
          dataSource={queueRows}
          pagination={queueRows.length > 8 ? { pageSize: 8, size: 'small' } : false}
          size="small"
          scroll={{ x: 980 }}
        />
      </Space>
    </Drawer>
  );
}
