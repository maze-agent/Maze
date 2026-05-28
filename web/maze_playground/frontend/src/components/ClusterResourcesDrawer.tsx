import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Drawer, Progress, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { ReloadOutlined } from '@ant-design/icons';
import { api } from '@/api/client';
import type { ClusterResourceNode, ClusterResourcesResponse } from '@/types/workflow';

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

export default function ClusterResourcesDrawer({ open, onClose }: ClusterResourcesDrawerProps) {
  const [data, setData] = useState<ClusterResourcesResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadResources = useCallback(async () => {
    setLoading(true);
    try {
      const result = await api.getClusterResources();
      setData(result);
      setError(null);
    } catch (err: any) {
      console.error('Failed to load cluster resources:', err);
      setError(err.response?.data?.error || err.message || 'Failed to load cluster resources');
    } finally {
      setLoading(false);
    }
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
      </Space>
    </Drawer>
  );
}
