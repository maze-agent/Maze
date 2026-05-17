import { useEffect, useState } from 'react';
import { Button, Drawer, Empty, List, Popconfirm, Space, Tag, Typography, message } from 'antd';
import { DeleteOutlined, HistoryOutlined, ReloadOutlined } from '@ant-design/icons';
import { api } from '@/api/client';
import { useWorkflowStore } from '@/stores/workflowStore';
import type { StaticWorkflowRunSnapshot, StaticWorkflowRunStatus } from '@/types/workflow';

const { Text } = Typography;

const terminalStatuses = new Set<StaticWorkflowRunStatus>(['completed', 'failed', 'canceled', 'interrupted']);

const statusColors: Record<StaticWorkflowRunStatus, string> = {
  running: 'processing',
  completed: 'success',
  failed: 'error',
  canceled: 'orange',
  interrupted: 'magenta',
};

interface RunHistoryDrawerProps {
  open: boolean;
  onClose: () => void;
}

function shortId(value: string) {
  return value.length > 12 ? `${value.slice(0, 8)}...` : value;
}

function formatTime(value?: number | null) {
  if (!value) return '-';
  return new Date(value * 1000).toLocaleString();
}

export default function RunHistoryDrawer({ open, onClose }: RunHistoryDrawerProps) {
  const {
    workspaceDir,
    staticRuns,
    setStaticRuns,
    upsertStaticRun,
    setStaticRunEvents,
    removeStaticRun,
    openRunViewer,
  } = useWorkflowStore();
  const [loading, setLoading] = useState(false);

  const loadRuns = async () => {
    setLoading(true);
    try {
      const result = await api.getStaticWorkflowRuns({
        workspaceDir: workspaceDir || undefined,
        limit: 100,
      });
      setStaticRuns(result.runs || []);
    } catch (error: any) {
      console.error('Failed to load workflow runs:', error);
      message.error(error.response?.data?.error || 'Failed to load workflow runs');
    } finally {
      setLoading(false);
    }
  };

  const openRun = async (run: StaticWorkflowRunSnapshot) => {
    try {
      const [runResult, eventResult] = await Promise.all([
        api.getStaticWorkflowRun(run.run_id, workspaceDir || undefined),
        api.getStaticWorkflowRunEvents(run.run_id, workspaceDir || undefined),
      ]);
      upsertStaticRun(runResult.run);
      setStaticRunEvents(run.run_id, eventResult.events || []);
      openRunViewer(run.run_id);
    } catch (error: any) {
      console.error('Failed to open workflow run:', error);
      message.error(error.response?.data?.error || 'Failed to open workflow run');
    }
  };

  const deleteRun = async (run: StaticWorkflowRunSnapshot) => {
    try {
      await api.deleteStaticWorkflowRun(run.run_id, workspaceDir || undefined);
      removeStaticRun(run.run_id);
      message.success('Workflow run deleted');
      await loadRuns();
    } catch (error: any) {
      console.error('Failed to delete workflow run:', error);
      message.error(error.response?.data?.error || 'Failed to delete workflow run');
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
          <HistoryOutlined />
          Run History
        </Space>
      }
      open={open}
      onClose={onClose}
      width={520}
      extra={
        <Button icon={<ReloadOutlined />} onClick={loadRuns} loading={loading}>
          Refresh
        </Button>
      }
    >
      <List
        loading={loading}
        dataSource={staticRuns}
        locale={{ emptyText: <Empty description="No workflow runs yet" /> }}
        renderItem={(run) => (
          <List.Item
            actions={[
              <Button key="open" type="link" onClick={() => openRun(run)}>
                Open
              </Button>,
              <Popconfirm
                key="delete"
                title="Delete this workflow run?"
                disabled={!terminalStatuses.has(run.status)}
                onConfirm={() => deleteRun(run)}
                okButtonProps={{ danger: true }}
              >
                <Button
                  danger
                  type="text"
                  icon={<DeleteOutlined />}
                  disabled={!terminalStatuses.has(run.status)}
                />
              </Popconfirm>,
            ]}
          >
            <List.Item.Meta
              title={
                <Space>
                  <Text strong>{run.workflow_name || 'Workflow Run'}</Text>
                  <Tag color={statusColors[run.status] || 'default'}>{run.status}</Tag>
                </Space>
              }
              description={
                <Space direction="vertical" size={2}>
                  <Text type="secondary">{shortId(run.run_id)}</Text>
                  <Text type="secondary">
                    {formatTime(run.created_time)} · {run.task_counts?.completed || 0}/{run.task_counts?.total || 0} completed
                  </Text>
                </Space>
              }
            />
          </List.Item>
        )}
      />
    </Drawer>
  );
}
