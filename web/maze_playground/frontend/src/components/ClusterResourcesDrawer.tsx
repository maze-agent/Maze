import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Collapse, Drawer, Empty, Form, Input, InputNumber, Modal, Progress, Select, Space, Table, Tabs, Tag, Tooltip, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { DeleteOutlined, PlayCircleOutlined, PlusOutlined, ReloadOutlined, StopOutlined, ToolOutlined } from '@ant-design/icons';
import { api } from '@/api/client';
import type {
  ClusterQueueTask,
  ClusterQueuesResponse,
  ClusterResourceNode,
  ClusterResourcesResponse,
  WorkerProfile,
  WorkerProfileDraftTestResponse,
} from '@/types/workflow';

const { Text } = Typography;
const RESOURCE_REFRESH_INTERVAL_MS = 3000;
const QUEUE_REFRESH_INTERVAL_MS = 1000;
const QUEUE_REFRESH_OPTIONS = [
  { label: '250 ms', value: 250 },
  { label: '500 ms', value: 500 },
  { label: '1 s', value: 1000 },
  { label: '2 s', value: 2000 },
  { label: '5 s', value: 5000 },
  { label: 'Paused', value: 0 },
];

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

function gpuMemoryTotals(node: ClusterResourceNode) {
  const gpu = node.resources?.gpu;
  const devices = gpu?.devices || [];
  if (devices.length > 0) {
    return devices.reduce(
      (acc, device) => ({
        total: acc.total + Number(device.total_memory || 0),
        available: acc.available + Number(device.available_memory || 0),
      }),
      { total: 0, available: 0 },
    );
  }
  return {
    total: Number(gpu?.total_memory || 0),
    available: Number(gpu?.available_memory || 0),
  };
}

type QueueRow = ClusterQueueTask & {
  queue_bucket: string;
  status_bucket: string;
  row_key: string;
};

const RESOURCE_QUEUE_NAMES = ['gpu', 'cpu', 'io'];

type WorkerAction = 'test' | 'start' | 'restart' | 'stop' | 'logs';

const WORKER_ACTION_LABELS: Record<WorkerAction, string> = {
  test: 'Test',
  start: 'Start',
  restart: 'Restart',
  stop: 'Stop process',
  logs: 'Logs',
};

function queueStatusColor(status?: string): string {
  if (status === 'running') return 'processing';
  if (status === 'retrying') return 'orange';
  if (status === 'pending') return 'volcano';
  if (status === 'ready') return 'blue';
  return 'default';
}

function queueResourceColor(queueName?: string): string {
  if (queueName === 'gpu') return 'gold';
  if (queueName === 'io') return 'cyan';
  if (queueName === 'cpu') return 'blue';
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
  const [form] = Form.useForm();
  const [data, setData] = useState<ClusterResourcesResponse | null>(null);
  const [queues, setQueues] = useState<ClusterQueuesResponse['queues'] | null>(null);
  const [workerProfiles, setWorkerProfiles] = useState<WorkerProfile[]>([]);
  const [loading, setLoading] = useState(false);
  const [queueLoading, setQueueLoading] = useState(false);
  const [workersLoading, setWorkersLoading] = useState(false);
  const [manualRefreshLoading, setManualRefreshLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [queueError, setQueueError] = useState<string | null>(null);
  const [workerError, setWorkerError] = useState<string | null>(null);
  const [workerModalOpen, setWorkerModalOpen] = useState(false);
  const [workerTestLoading, setWorkerTestLoading] = useState(false);
  const [workerDraftTest, setWorkerDraftTest] = useState<WorkerProfileDraftTestResponse['test'] | null>(null);
  const [actionKey, setActionKey] = useState('');
  const [nodeActionKey, setNodeActionKey] = useState('');
  const [selectedWorkerIds, setSelectedWorkerIds] = useState<string[]>([]);
  const [logModal, setLogModal] = useState<{ title: string; output: string } | null>(null);
  const [activeTab, setActiveTab] = useState('resources');
  const [queueRefreshIntervalMs, setQueueRefreshIntervalMs] = useState(QUEUE_REFRESH_INTERVAL_MS);
  const resourceRequestRef = useRef<Promise<void> | null>(null);
  const queueRequestRef = useRef<Promise<void> | null>(null);

  const loadResources = useCallback((showLoading = false) => {
    if (resourceRequestRef.current) {
      return resourceRequestRef.current;
    }
    if (showLoading) {
      setLoading(true);
    }
    const request = (async () => {
      try {
        const result = await api.getClusterResources();
        setData(result);
        setError(null);
      } catch (reason: any) {
        console.error('Failed to load cluster resources:', reason);
        setError(
          reason?.response?.data?.error
          || reason?.message
          || 'Failed to load cluster resources',
        );
      } finally {
        resourceRequestRef.current = null;
        if (showLoading) {
          setLoading(false);
        }
      }
    })();
    resourceRequestRef.current = request;
    return request;
  }, []);

  const loadQueues = useCallback((showLoading = false) => {
    if (queueRequestRef.current) {
      return queueRequestRef.current;
    }
    if (showLoading) {
      setQueueLoading(true);
    }
    const request = (async () => {
      try {
        const result = await api.getClusterQueues();
        setQueues(result.queues);
        setQueueError(null);
      } catch (reason: any) {
        console.error('Failed to load cluster queues:', reason);
        setQueueError(
          reason?.response?.data?.error
          || reason?.message
          || 'Failed to load cluster queues',
        );
      } finally {
        queueRequestRef.current = null;
        if (showLoading) {
          setQueueLoading(false);
        }
      }
    })();
    queueRequestRef.current = request;
    return request;
  }, []);

  const loadWorkerProfiles = useCallback(async (showLoading = false) => {
    if (showLoading) {
      setWorkersLoading(true);
    }
    try {
      const result = await api.listWorkerProfiles();
      setWorkerProfiles(result.profiles || []);
      setWorkerError(null);
    } catch (reason: any) {
      console.error('Failed to load worker profiles:', reason);
      setWorkerError(reason?.response?.data?.error || reason?.message || 'Failed to load worker profiles');
    } finally {
      if (showLoading) {
        setWorkersLoading(false);
      }
    }
  }, []);

  const refreshActiveTab = useCallback(async () => {
    setManualRefreshLoading(true);
    try {
      if (activeTab === 'workers') {
        await Promise.all([loadResources(), loadWorkerProfiles(true)]);
      } else {
        await Promise.all([loadResources(true), loadQueues(true)]);
      }
    } finally {
      setManualRefreshLoading(false);
    }
  }, [activeTab, loadQueues, loadResources, loadWorkerProfiles]);

  useEffect(() => {
    if (!open) {
      return;
    }

    if (activeTab === 'workers') {
      void loadResources();
      void loadWorkerProfiles(true);
      return;
    }

    let cancelled = false;
    let resourceTimer: number | undefined;
    let queueTimer: number | undefined;

    const pollResources = async (showLoading = false) => {
      await loadResources(showLoading);
      if (!cancelled) {
        resourceTimer = window.setTimeout(pollResources, RESOURCE_REFRESH_INTERVAL_MS);
      }
    };
    const pollQueues = async (showLoading = false) => {
      await loadQueues(showLoading);
      if (!cancelled && queueRefreshIntervalMs > 0) {
        queueTimer = window.setTimeout(pollQueues, queueRefreshIntervalMs);
      }
    };

    void pollResources(true);
    if (queueRefreshIntervalMs > 0) {
      void pollQueues(true);
    } else {
      void loadQueues(true);
    }
    return () => {
      cancelled = true;
      if (resourceTimer !== undefined) {
        window.clearTimeout(resourceTimer);
      }
      if (queueTimer !== undefined) {
        window.clearTimeout(queueTimer);
      }
    };
  }, [activeTab, loadQueues, loadResources, loadWorkerProfiles, open, queueRefreshIntervalMs]);

  const nodes = useMemo(() => {
    const registered = data?.cluster?.nodes || [];
    const unregistered = data?.cluster?.unregistered_ray_nodes || [];
    return [...registered, ...unregistered];
  }, [data]);

  const totals = useMemo(() => nodes.reduce(
    (acc, node) => {
      const cpu = cpuTotals(node);
      const gpu = gpuTotals(node);
      const gpuMemory = gpuMemoryTotals(node);
      acc.nodes += 1;
      acc.registered += node.registered ? 1 : 0;
      acc.cpu += cpu.total || 0;
      acc.gpu += gpu.total || 0;
      acc.gpuMemoryTotal += gpuMemory.total || 0;
      acc.gpuMemoryAvailable += gpuMemory.available || 0;
      return acc;
    },
    { nodes: 0, registered: 0, cpu: 0, gpu: 0, gpuMemoryTotal: 0, gpuMemoryAvailable: 0 },
  ), [nodes]);

  const defaultHeadUrl = useMemo(() => {
    const headIp = data?.cluster?.head_node_ip;
    return headIp ? `http://${headIp}:8000` : 'http://127.0.0.1:8000';
  }, [data?.cluster?.head_node_ip]);

  const defaultWorkerFormValues = useMemo(() => ({
    port: 22,
    username: 'root',
    remoteProjectDir: '/root/data/Maze',
    condaEnv: 'maze',
    condaSh: '/root/miniconda3/etc/profile.d/conda.sh',
    heartbeatInterval: 10,
    authMethod: 'password',
  }), []);

  const openWorkerModal = () => {
    form.resetFields();
    form.setFieldsValue(defaultWorkerFormValues);
    setWorkerDraftTest(null);
    setWorkerModalOpen(true);
  };

  const queueRows = useMemo<QueueRow[]>(() => {
    if (!queues) return [];

    if (queues.queues) {
      return RESOURCE_QUEUE_NAMES.flatMap((queueName) => (
        (queues.queues?.[queueName]?.tasks || []).map((task, index) => ({
          ...task,
          queue_bucket: queueName,
          status_bucket: task.status || 'ready',
          row_key: `${queueName}:${task.workflow_id}:${task.task_id}:${index}`,
        }))
      ));
    }

    const buildRows = (status: string, tasks: ClusterQueueTask[] = []) => tasks.map((task, index) => {
      const queueName = task.queue_name || task.task_kind || 'cpu';
      return {
        ...task,
        queue_bucket: queueName,
        status_bucket: status,
        row_key: `${queueName}:${status}:${task.workflow_id}:${task.task_id}:${index}`,
      };
    });

    return [
      ...buildRows('ready', queues.ready_tasks),
      ...buildRows('pending', queues.pending_tasks),
      ...buildRows('retrying', queues.retrying_tasks),
    ];
  }, [queues]);

  const runningRows = useMemo<QueueRow[]>(() => (
    (queues?.running_tasks || []).map((task, index) => {
      const queueName = task.queue_name || task.task_kind || 'cpu';
      return {
        ...task,
        queue_bucket: queueName,
        status_bucket: 'running',
        row_key: `running:${queueName}:${task.workflow_id}:${task.task_id}:${index}`,
      };
    })
  ), [queues?.running_tasks]);

  const resourceQueueRows = useMemo(() => (
    RESOURCE_QUEUE_NAMES.reduce<Record<string, QueueRow[]>>((acc, queueName) => {
      acc[queueName] = [
        ...queueRows.filter((row) => row.queue_bucket === queueName),
        ...runningRows.filter((row) => row.queue_bucket === queueName),
      ];
      return acc;
    }, {})
  ), [queueRows, runningRows]);

  const schedulingAlgorithm = useMemo(() => {
    return queues?.scheduling_algorithm || '-';
  }, [queues?.scheduling_algorithm]);

  const buildWorkerProfileFromValues = (values: any) => {
    return {
      id: values.id,
      name: values.name,
      host: values.host,
      port: values.port,
      username: values.username,
      remoteProjectDir: values.remoteProjectDir,
      condaEnv: values.condaEnv,
      condaSh: values.condaSh,
      headUrl: defaultHeadUrl,
      heartbeatInterval: values.heartbeatInterval,
      auth: {
        method: values.authMethod,
        privateKeyPath: values.privateKeyPath,
      },
    };
  };

  const testWorkerProfileDraft = async () => {
    try {
      const values = await form.validateFields(['name', 'host', 'port', 'username', 'authMethod', 'password', 'privateKeyPath', 'remoteProjectDir', 'condaEnv', 'condaSh', 'heartbeatInterval']);
      setWorkerTestLoading(true);
      setWorkerDraftTest(null);
      const result = await api.testWorkerProfileDraft({
        profile: buildWorkerProfileFromValues(values),
        password: values.password,
        timeoutMs: 30000,
      });
      setWorkerDraftTest(result.test || null);
      if (result.test?.ok) {
        message.success('Worker connection test passed');
      } else {
        message.warning('Worker connection test completed with warnings');
      }
      await loadWorkerProfiles();
    } catch (reason: any) {
      if (reason?.errorFields) {
        return;
      }
      const result = reason?.response?.data?.result;
      const output = result ? [result.stdout, result.stderr].filter(Boolean).join('\n') : '';
      setWorkerDraftTest({
        ok: false,
        checks: [{
          name: 'test',
          ok: false,
          stderr: reason?.response?.data?.error || reason?.message || 'Worker connection test failed',
        }],
        result,
      });
      if (output) {
        setLogModal({ title: 'Worker connection test failed', output });
      }
      message.error(reason?.response?.data?.error || reason?.message || 'Worker connection test failed');
    } finally {
      setWorkerTestLoading(false);
    }
  };

  const saveWorkerProfile = async (values: any) => {
    const profile = buildWorkerProfileFromValues(values);
    try {
      await api.saveWorkerProfile({ profile, password: values.password });
      message.success('Worker profile saved');
      setWorkerModalOpen(false);
      setWorkerDraftTest(null);
      form.resetFields();
      form.setFieldsValue(defaultWorkerFormValues);
      await loadWorkerProfiles();
    } catch (reason: any) {
      message.error(reason?.response?.data?.error || reason?.message || 'Failed to save worker profile');
    }
  };

  const runWorkerAction = async (profile: WorkerProfile, action: WorkerAction, password?: string) => {
    const key = `${profile.id}:${action}`;
    setActionKey(key);
    try {
      const result = await api.runWorkerProfileAction(profile.id, action, { password, timeoutMs: 90000 });
      if (action === 'logs') {
        setLogModal({
          title: `${profile.name} logs`,
          output: [result.result?.stdout, result.result?.stderr].filter(Boolean).join('\n') || 'No output',
        });
      } else {
        message.success(`${WORKER_ACTION_LABELS[action]} sent to ${profile.name}`);
      }
      await Promise.all([loadWorkerProfiles(), loadResources()]);
    } catch (reason: any) {
      const output = reason?.response?.data?.result;
      const text = output ? [output.stdout, output.stderr].filter(Boolean).join('\n') : '';
      if (text) {
        setLogModal({ title: `${profile.name} ${WORKER_ACTION_LABELS[action]} failed`, output: text });
      }
      message.error(reason?.response?.data?.error || reason?.message || `Failed to ${WORKER_ACTION_LABELS[action].toLowerCase()} worker`);
    } finally {
      setActionKey('');
    }
  };

  const setNodeDisabled = async (node: ClusterResourceNode, disabled: boolean) => {
    const key = `${node.node_id}:${disabled ? 'disable' : 'enable'}`;
    setNodeActionKey(key);
    try {
      const result = await api.setClusterNodeDisabled(node.node_id, disabled);
      if (result.cluster) {
        setData({ status: 'success', cluster: result.cluster });
      } else {
        await loadResources();
      }
      message.success(`${disabled ? 'Drain' : 'Enable'} sent to ${node.node_ip || shortId(node.node_id)}`);
    } catch (reason: any) {
      message.error(reason?.response?.data?.error || reason?.message || 'Failed to update node scheduling');
    } finally {
      setNodeActionKey('');
    }
  };

  const unlockWorkerProfile = (profile: WorkerProfile) => {
    if (profile.auth?.method === 'key') {
      confirmWorkerAction(profile, 'test');
      return;
    }
    Modal.confirm({
      title: `Unlock ${profile.name}`,
      content: (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Text type="secondary">
            This tests SSH and keeps the password only in the current backend session.
          </Text>
          <Input.Password
            autoFocus
            placeholder="SSH password"
            onChange={(event) => {
              (unlockWorkerProfile as any).password = event.target.value;
            }}
          />
        </Space>
      ),
      onOk: () => runWorkerAction(profile, 'test', (unlockWorkerProfile as any).password || ''),
      afterClose: () => {
        (unlockWorkerProfile as any).password = '';
      },
    });
  };

  const confirmWorkerAction = (profile: WorkerProfile, action: WorkerAction) => {
    if (profile.auth?.method === 'password' && !profile.auth?.hasPassword && action !== 'logs') {
      Modal.confirm({
        title: `Password for ${profile.name}`,
        content: (
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Text type="secondary">
              {WORKER_ACTION_LABELS[action]} runs over SSH on the worker. The password is kept only in the current backend session.
            </Text>
            <Input.Password
              autoFocus
              placeholder="SSH password for this action"
              onChange={(event) => {
                (confirmWorkerAction as any).password = event.target.value;
              }}
            />
          </Space>
        ),
        onOk: () => runWorkerAction(profile, action, (confirmWorkerAction as any).password || ''),
        afterClose: () => {
          (confirmWorkerAction as any).password = '';
        },
      });
      return;
    }
    runWorkerAction(profile, action);
  };

  const deleteWorkerProfile = async (profile: WorkerProfile) => {
    Modal.confirm({
      title: `Delete ${profile.name}?`,
      okButtonProps: { danger: true },
      onOk: async () => {
        await api.deleteWorkerProfile(profile.id);
        message.success('Worker profile deleted');
        await loadWorkerProfiles();
      },
    });
  };

  const executeBulkWorkerAction = async (
    action: WorkerAction,
    passwordByProfileId?: Record<string, string>,
  ) => {
    const key = `bulk:${action}`;
    setActionKey(key);
    try {
      const result = await api.runWorkerProfilesBulkAction({
        action,
        profileIds: selectedWorkerIds,
        passwordByProfileId,
        timeoutMs: 90000,
      });
      const failed = (result.results || []).filter((item) => !item.ok);
      if (failed.length > 0) {
        setLogModal({
          title: `${action} selected workers`,
          output: failed.map((item) => `${item.profileId}: ${item.error || 'failed'}`).join('\n'),
        });
        message.warning(`${failed.length} worker action failed`);
      } else {
        message.success(`${action} sent to ${selectedWorkerIds.length} worker${selectedWorkerIds.length === 1 ? '' : 's'}`);
      }
      await Promise.all([loadWorkerProfiles(), loadResources()]);
    } catch (reason: any) {
      message.error(reason?.response?.data?.error || reason?.message || `Failed to ${action} selected workers`);
    } finally {
      setActionKey('');
    }
  };

  const runBulkWorkerAction = async (action: WorkerAction) => {
    if (selectedWorkerIds.length === 0) {
      message.warning('Select at least one worker profile');
      return;
    }

    const passwordProfiles = workerProfiles.filter(
      (profile) => selectedWorkerIds.includes(profile.id)
        && profile.auth?.method === 'password'
        && !profile.auth?.hasPassword
        && action !== 'logs',
    );
    if (passwordProfiles.length > 0) {
      Modal.confirm({
        title: `Password for ${passwordProfiles.length} selected worker${passwordProfiles.length === 1 ? '' : 's'}`,
        content: (
          <Input.Password
            autoFocus
            placeholder="SSH password for this batch action"
            onChange={(event) => {
              (runBulkWorkerAction as any).password = event.target.value;
            }}
          />
        ),
        onOk: () => {
          const password = (runBulkWorkerAction as any).password || '';
          const passwordByProfileId = Object.fromEntries(
            passwordProfiles.map((profile) => [profile.id, password]),
          );
          return executeBulkWorkerAction(action, passwordByProfileId);
        },
        afterClose: () => {
          (runBulkWorkerAction as any).password = '';
        },
      });
      return;
    }

    executeBulkWorkerAction(action);
  };

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
          {node.disabled && <Tag color="volcano">drained</Tag>}
          {node.stale && <Tag color="orange">stale</Tag>}
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
      width: 160,
      render: (_, node) => {
        const { total, available } = gpuTotals(node);
        if (!total) {
          return <Text type="secondary">No GPU</Text>;
        }

        return (
          <Space direction="vertical" size={2} style={{ width: '100%' }}>
            <Text>{available === undefined ? `${total} total` : `${available} / ${total} available`}</Text>
            <Progress percent={available === undefined ? 0 : percent(available, total)} showInfo={false} size="small" />
          </Space>
        );
      },
    },
    {
      title: 'GPU Memory Reserved',
      key: 'gpu_memory',
      width: 260,
      render: (_, node) => {
        const devices = node.resources?.gpu?.devices || [];
        const totals = gpuMemoryTotals(node);
        if (!totals.total) {
          return <Text type="secondary">-</Text>;
        }

        return (
          <Space direction="vertical" size={4} style={{ width: '100%' }}>
            <Text>{formatGpuMemory(Math.max(totals.total - totals.available, 0))} / {formatGpuMemory(totals.total)}</Text>
            <Progress percent={percent(totals.available, totals.total)} showInfo={false} size="small" />
            <Space size={4} wrap>
              {devices.map((device) => (
                <Tag key={String(device.gpu_id)} color={device.available_memory > 0 ? 'green' : 'volcano'}>
                  GPU {device.gpu_id}: {formatGpuMemory(Math.max(device.total_memory - device.available_memory, 0))} / {formatGpuMemory(device.total_memory)}
                </Tag>
              ))}
            </Space>
          </Space>
        );
      },
    },
    {
      title: 'Sandbox',
      key: 'sandbox',
      width: 120,
      render: (_, node) => (
        <Tag color={node.capabilities?.workspace_sandbox ? 'green' : 'default'}>workspace</Tag>
      ),
    },
    {
      title: 'Admission',
      key: 'scheduling',
      width: 150,
      render: (_, node) => {
        if (node.role === 'head' || !node.registered) {
          return <Text type="secondary">-</Text>;
        }
        return node.disabled ? (
          <Button
            size="small"
            loading={nodeActionKey === `${node.node_id}:enable`}
            onClick={() => setNodeDisabled(node, false)}
          >
            Enable
          </Button>
        ) : (
          <Tooltip title="Stop assigning new tasks without SSHing into the worker">
            <Button
              size="small"
              loading={nodeActionKey === `${node.node_id}:disable`}
              onClick={() => setNodeDisabled(node, true)}
            >
              Drain
            </Button>
          </Tooltip>
        );
      },
    },
  ];

  const renderQueueTaskRow = (task: QueueRow) => {
    const selected = task.selected_node || task.schedule_decision?.selected_node;
    const taskResources: Record<string, any> = task.resources || {};
    const cpuNum = taskResources.cpu_num ?? (taskResources as any).cpu;
    const reason = task.pending_reason
      || task.schedule_decision?.reason
      || errorSummary(task.last_error);
    const rejects = candidateRejectSummary(task);
    const lastError = errorSummary(task.last_error);
    const attempt = task.attempt ?? 0;
    const maxRetries = task.max_retries ?? 0;
    const attemptLabel = maxRetries > 0 ? `attempt ${attempt}/${maxRetries}` : `attempt ${attempt}`;
    const elapsed = task.elapsed_seconds !== undefined
      ? `elapsed ${formatDurationSeconds(task.elapsed_seconds)}`
      : '';
    const retryWait = task.retry_wait_seconds
      ? `retry in ${formatDurationSeconds(task.retry_wait_seconds)}`
      : '';
    const nextEligible = task.next_eligible_time ? `next ${formatTime(task.next_eligible_time)}` : '';
    const timeout = task.timeout_seconds !== undefined && task.timeout_seconds !== null
      ? `timeout ${formatDurationSeconds(task.timeout_seconds)}`
      : '';
    const borderColor = task.status_bucket === 'running'
      ? '#1677ff'
      : task.status_bucket === 'pending'
        ? '#fa541c'
        : task.status_bucket === 'retrying'
          ? '#fa8c16'
          : '#d9d9d9';

    return (
      <div
        key={task.row_key}
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
          gap: 12,
          alignItems: 'start',
          padding: '12px 14px',
          borderLeft: `3px solid ${borderColor}`,
          borderTop: '1px solid #f0f0f0',
          background: task.status_bucket === 'running' ? '#f6fbff' : '#fff',
        }}
      >
        <Space direction="vertical" size={4}>
          <Text type="secondary" style={{ fontSize: 11 }}>Task</Text>
          <Text copyable={{ text: task.task_id }} strong>
            {shortId(task.task_id)}
          </Text>
          <Text type="secondary" style={{ fontSize: 12 }}>{shortId(task.workflow_id)}</Text>
          <Space size={4} wrap>
            <Tag color={queueResourceColor(task.queue_bucket)}>{task.queue_bucket}</Tag>
            <Tag color={queueStatusColor(task.status_bucket)}>{task.status_bucket}</Tag>
            {task.task_type && <Tag>{task.task_type}</Tag>}
          </Space>
        </Space>
        <Space direction="vertical" size={4}>
          <Text type="secondary" style={{ fontSize: 11 }}>Attempt</Text>
          <Text>{attemptLabel}</Text>
          {[elapsed, retryWait, nextEligible, timeout].filter(Boolean).map((item) => (
            <Text key={item} type="secondary" style={{ fontSize: 12 }}>
              {item}
            </Text>
          ))}
        </Space>
        <Space direction="vertical" size={4}>
          <Text type="secondary" style={{ fontSize: 11 }}>Assigned Node</Text>
          <Text>{selected?.node_ip || '-'}</Text>
          <Space size={4} wrap>
            {selected?.gpu_id !== undefined && selected?.gpu_id !== null && <Tag color="gold">GPU {selected.gpu_id}</Tag>}
            {cpuNum !== undefined && <Tag>CPU {cpuNum}</Tag>}
            {taskResources.gpu_mem !== undefined && <Tag>VRAM {formatGpuMemory(taskResources.gpu_mem)}</Tag>}
            {taskResources.io_num !== undefined && <Tag>I/O {taskResources.io_num}</Tag>}
          </Space>
        </Space>
        <Space direction="vertical" size={4}>
          <Text type="secondary" style={{ fontSize: 11 }}>Status</Text>
          <Text style={{ fontSize: 12 }} ellipsis={{ tooltip: reason || '-' }}>
            {reason || '-'}
          </Text>
          {task.runtime_status && task.runtime_status !== task.status_bucket && (
            <Text type="secondary" style={{ fontSize: 12 }}>runtime {task.runtime_status}</Text>
          )}
          {rejects.map((reject) => (
            <Text key={reject} type="secondary" style={{ fontSize: 12 }} ellipsis={{ tooltip: reject }}>
              {reject}
            </Text>
          ))}
          {lastError && reason !== lastError && (
            <Text type="danger" style={{ fontSize: 12 }} ellipsis={{ tooltip: lastError }}>
              {lastError}
            </Text>
          )}
        </Space>
      </div>
    );
  };

  const workerColumns: ColumnsType<WorkerProfile> = [
    {
      title: 'Worker',
      key: 'worker',
      width: 230,
      render: (_, profile) => (
        <Space direction="vertical" size={2}>
          <Text strong>{profile.name}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>{profile.username}@{profile.host}:{profile.port}</Text>
        </Space>
      ),
    },
    {
      title: 'Runtime',
      key: 'runtime',
      render: (_, profile) => (
        <Space direction="vertical" size={2}>
          <Text style={{ fontSize: 12 }} ellipsis={{ tooltip: profile.remoteProjectDir }}>
            {profile.remoteProjectDir}
          </Text>
          <Text type="secondary" style={{ fontSize: 12 }} ellipsis={{ tooltip: profile.headUrl || defaultHeadUrl }}>
            head {profile.headUrl || defaultHeadUrl}
          </Text>
          <Space size={4} wrap>
            <Tag>{profile.condaEnv}</Tag>
            <Tag color={profile.auth?.method === 'key' ? 'geekblue' : 'purple'}>{profile.auth?.method || 'password'}</Tag>
            {profile.auth?.hasPassword && <Tag color="green">session secret</Tag>}
            {profile.auth?.method === 'password' && !profile.auth?.hasPassword && <Tag color="orange">secret needed</Tag>}
          </Space>
        </Space>
      ),
    },
    {
      title: 'Last Action',
      key: 'lastAction',
      width: 170,
      render: (_, profile) => (
        <Space direction="vertical" size={2}>
          <Space size={4} wrap>
            <Text>{profile.lastAction?.action || '-'}</Text>
              {profile.lastAction?.action && (
                <Tag color={profile.lastAction.ok === false ? 'red' : 'green'}>
                  {profile.lastAction.ok === false ? 'failed' : 'ok'}
              </Tag>
            )}
          </Space>
          <Text type="secondary" style={{ fontSize: 12 }}>{profile.lastAction?.at ? new Date(profile.lastAction.at).toLocaleString() : '-'}</Text>
        </Space>
      ),
    },
    {
      title: 'Actions',
      key: 'actions',
      width: 390,
      render: (_, profile) => (
        <Space size={6} wrap>
          <Button size="small" icon={<ToolOutlined />} loading={actionKey === `${profile.id}:test`} onClick={() => confirmWorkerAction(profile, 'test')}>Test</Button>
          <Button size="small" loading={actionKey === `${profile.id}:test`} onClick={() => unlockWorkerProfile(profile)}>Unlock</Button>
          <Button size="small" icon={<PlayCircleOutlined />} loading={actionKey === `${profile.id}:start`} onClick={() => confirmWorkerAction(profile, 'start')}>Start</Button>
          <Button size="small" icon={<ReloadOutlined />} loading={actionKey === `${profile.id}:restart`} onClick={() => confirmWorkerAction(profile, 'restart')}>Restart</Button>
          <Tooltip title="SSH into the worker and stop the remote process">
            <Button size="small" icon={<StopOutlined />} loading={actionKey === `${profile.id}:stop`} onClick={() => confirmWorkerAction(profile, 'stop')}>Stop process</Button>
          </Tooltip>
          <Button size="small" onClick={() => confirmWorkerAction(profile, 'logs')}>Logs</Button>
          <Button size="small" danger title="Delete worker profile" icon={<DeleteOutlined />} onClick={() => deleteWorkerProfile(profile)} />
        </Space>
      ),
    },
  ];

  const resourcesContent = (
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
          ['GPU Memory Reserved', `${formatGpuMemory(Math.max(totals.gpuMemoryTotal - totals.gpuMemoryAvailable, 0))} / ${formatGpuMemory(totals.gpuMemoryTotal)}`],
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
          description="Start or restart the worker from the Workers tab, or run maze start --worker --addr <head-ip>:8000 on that machine."
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
      <div
        style={{
          border: '1px solid #f0f0f0',
          borderRadius: 6,
          padding: 12,
          background: '#fafafa',
        }}
      >
        <Space style={{ justifyContent: 'space-between', width: '100%' }} align="center" wrap>
          <Space wrap>
            <Text strong>Scheduler Queues</Text>
            <Tag color={schedulingAlgorithm === 'HACS' ? 'purple' : 'blue'}>
              algorithm: {schedulingAlgorithm}
            </Tag>
            {queues?.scheduling_policy && (
              <Tag color="geekblue">node policy: {queues.scheduling_policy}</Tag>
            )}
          </Space>
          <Space size={8}>
            <Text type="secondary" style={{ fontSize: 12 }}>Queue refresh</Text>
            <Select
              size="small"
              value={queueRefreshIntervalMs}
              onChange={setQueueRefreshIntervalMs}
              options={QUEUE_REFRESH_OPTIONS}
              style={{ width: 108 }}
            />
          </Space>
        </Space>
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))',
            gap: 8,
            marginTop: 12,
          }}
        >
          {[
            { label: 'ready', value: queues?.counts.ready || 0, color: '#1677ff', background: '#f0f5ff', border: '#adc6ff' },
            { label: 'running', value: queues?.counts.running || 0, color: '#0958d9', background: '#e6f4ff', border: '#91caff' },
            { label: 'pending', value: queues?.counts.pending || 0, color: '#d4380d', background: '#fff2e8', border: '#ffbb96' },
            { label: 'retrying', value: queues?.counts.retrying || 0, color: '#d46b08', background: '#fff7e6', border: '#ffd591' },
            { label: 'total', value: queues?.counts.total_queued || 0, color: '#595959', background: '#fff', border: '#d9d9d9' },
          ].map((item) => (
            <div
              key={item.label}
              style={{
                border: `1px solid ${item.border}`,
                borderRadius: 6,
                background: item.background,
                padding: '6px 10px',
              }}
            >
              <Text type="secondary" style={{ display: 'block', fontSize: 11 }}>{item.label}</Text>
              <Text strong style={{ color: item.color, fontSize: 18 }}>{item.value}</Text>
            </div>
          ))}
        </div>
      </div>
      <Collapse size="small" bordered={false} defaultActiveKey={RESOURCE_QUEUE_NAMES} style={{ background: '#fff' }}>
        {RESOURCE_QUEUE_NAMES.map((queueName) => {
          const rows = resourceQueueRows[queueName] || [];
          const queueStats = queues?.queues?.[queueName] || queues?.counts.by_queue?.[queueName];
          const runningCount = rows.filter((row) => row.status_bucket === 'running').length;
          return (
            <Collapse.Panel
              key={queueName}
              header={(
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    gap: 12,
                    width: '100%',
                    paddingRight: 12,
                  }}
                >
                  <Space size={8}>
                    <Tag color={queueResourceColor(queueName)}>{queueName}</Tag>
                    <Text strong>{rows.length}</Text>
                    <Text type="secondary" style={{ fontSize: 12 }}>tasks</Text>
                  </Space>
                  <Space size={6} wrap>
                    <Tag color="blue">ready {queueStats?.ready || 0}</Tag>
                    <Tag color="processing">running {runningCount}</Tag>
                    <Tag color="volcano">pending {queueStats?.pending || 0}</Tag>
                    <Tag color="orange">retrying {queueStats?.retrying || 0}</Tag>
                  </Space>
                </div>
              )}
            >
              <div
                style={{
                  border: '1px solid #f0f0f0',
                  borderRadius: 6,
                  overflow: 'hidden',
                  background: '#fff',
                }}
              >
                {queueLoading && !queues ? (
                  <div style={{ padding: 16 }}>
                    <Text type="secondary">Loading queues...</Text>
                  </div>
                ) : rows.length === 0 ? (
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description="No queued or running tasks"
                    style={{ margin: '12px 0' }}
                  />
                ) : (
                  rows.map(renderQueueTaskRow)
                )}
              </div>
            </Collapse.Panel>
          );
        })}
      </Collapse>
    </Space>
  );

  const workersContent = (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      {workerError && <Alert type="error" message={workerError} showIcon />}
      <Space style={{ justifyContent: 'space-between', width: '100%' }} align="center" wrap>
        <Space wrap>
          <Button type="primary" icon={<PlusOutlined />} onClick={openWorkerModal}>Add Worker</Button>
          <Tag color="geekblue">head {defaultHeadUrl}</Tag>
          <Tag>{selectedWorkerIds.length} selected</Tag>
        </Space>
        <Space wrap>
          <Button size="small" icon={<ToolOutlined />} disabled={selectedWorkerIds.length === 0} loading={actionKey === 'bulk:test'} onClick={() => runBulkWorkerAction('test')}>Test selected</Button>
          <Button size="small" icon={<PlayCircleOutlined />} disabled={selectedWorkerIds.length === 0} loading={actionKey === 'bulk:start'} onClick={() => runBulkWorkerAction('start')}>Start selected</Button>
          <Button size="small" icon={<ReloadOutlined />} disabled={selectedWorkerIds.length === 0} loading={actionKey === 'bulk:restart'} onClick={() => runBulkWorkerAction('restart')}>Restart selected</Button>
          <Button size="small" icon={<StopOutlined />} disabled={selectedWorkerIds.length === 0} loading={actionKey === 'bulk:stop'} onClick={() => runBulkWorkerAction('stop')}>Stop processes</Button>
        </Space>
      </Space>
      <Table
        rowKey="id"
        loading={workersLoading}
        columns={workerColumns}
        dataSource={workerProfiles}
        rowSelection={{
          selectedRowKeys: selectedWorkerIds,
          onChange: (keys) => setSelectedWorkerIds(keys.map(String)),
        }}
        pagination={false}
        size="middle"
        scroll={{ x: 900 }}
      />
    </Space>
  );

  return (
    <Drawer
      title="Cluster"
      open={open}
      onClose={onClose}
      width="min(960px, 100vw)"
      extra={(
        <Button icon={<ReloadOutlined />} onClick={refreshActiveTab} loading={manualRefreshLoading}>
          Refresh
        </Button>
      )}
    >
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          { key: 'resources', label: 'Resources', children: resourcesContent },
          { key: 'workers', label: 'Workers', children: workersContent },
        ]}
      />
      <Modal
        title={logModal?.title}
        open={Boolean(logModal)}
        onCancel={() => setLogModal(null)}
        footer={null}
        width={760}
      >
        <pre style={{ whiteSpace: 'pre-wrap', maxHeight: 460, overflow: 'auto', fontSize: 12 }}>
          {logModal?.output}
        </pre>
      </Modal>
      <Modal
        title="Add Worker"
        open={workerModalOpen}
        onCancel={() => {
          setWorkerModalOpen(false);
          setWorkerDraftTest(null);
        }}
        footer={(
          <Space style={{ justifyContent: 'space-between', width: '100%' }} align="center">
            <Button loading={workerTestLoading} onClick={testWorkerProfileDraft}>
              Test Connection
            </Button>
            <Space>
              <Button onClick={() => {
                setWorkerModalOpen(false);
                setWorkerDraftTest(null);
              }}>
                Cancel
              </Button>
              <Button type="primary" onClick={() => form.submit()}>
                Save Worker
              </Button>
            </Space>
          </Space>
        )}
        width={720}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={saveWorkerProfile}
          initialValues={defaultWorkerFormValues}
          onValuesChange={() => setWorkerDraftTest(null)}
        >
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message={`Workers will join ${defaultHeadUrl}`}
          />
          {workerDraftTest && (
            <Alert
              type={workerDraftTest.ok ? 'success' : 'warning'}
              showIcon
              style={{ marginBottom: 16 }}
              message={workerDraftTest.ok ? 'Connection test passed' : 'Connection test has issues'}
              description={(
                <Space size={6} wrap>
                  {workerDraftTest.checks.map((check) => (
                    <Tag
                      key={check.name}
                      color={check.ok ? 'green' : check.warning ? 'orange' : 'red'}
                      title={check.stderr || check.stdout || undefined}
                    >
                      {check.name}: {check.ok ? 'ok' : check.warning ? 'warning' : 'failed'}
                    </Tag>
                  ))}
                </Space>
              )}
            />
          )}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 12 }}>
            <Form.Item name="name" label="Name" rules={[{ required: true }]}>
              <Input placeholder="gpu-worker-1" />
            </Form.Item>
            <Form.Item name="host" label="Host" rules={[{ required: true }]}>
              <Input placeholder="10.0.0.2 or ssh.example.com" />
            </Form.Item>
            <Form.Item name="port" label="SSH Port" rules={[{ required: true }]}>
              <InputNumber min={1} max={65535} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="username" label="User" rules={[{ required: true }]}>
              <Input />
            </Form.Item>
            <Form.Item name="authMethod" label="Auth">
              <Select options={[{ value: 'password', label: 'Password' }, { value: 'key', label: 'Private key' }]} />
            </Form.Item>
            <Form.Item name="password" label="Password">
              <Input.Password placeholder="Kept only in this backend session" />
            </Form.Item>
          </div>
          <Collapse
            size="small"
            ghost
            items={[
              {
                key: 'advanced',
                label: 'Advanced',
                children: (
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 12 }}>
                    <Form.Item name="privateKeyPath" label="Private Key Path">
                      <Input placeholder="/root/.ssh/id_rsa" />
                    </Form.Item>
                    <Form.Item name="remoteProjectDir" label="Project Dir" rules={[{ required: true }]}>
                      <Input />
                    </Form.Item>
                    <Form.Item name="condaEnv" label="Conda Env" rules={[{ required: true }]}>
                      <Input />
                    </Form.Item>
                    <Form.Item name="condaSh" label="Conda Init Script">
                      <Input />
                    </Form.Item>
                    <Form.Item name="heartbeatInterval" label="Heartbeat">
                      <InputNumber min={1} max={300} style={{ width: '100%' }} />
                    </Form.Item>
                  </div>
                ),
              },
            ]}
          />
        </Form>
      </Modal>
    </Drawer>
  );
}
