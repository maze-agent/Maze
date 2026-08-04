import { useEffect, useState } from 'react';
import { Modal, Typography, Spin, Alert, Button, Tabs, List, Tag, Space } from 'antd';
import { CheckCircleOutlined, CloseCircleOutlined, LoadingOutlined, FileTextOutlined, CodeOutlined, DownloadOutlined } from '@ant-design/icons';
import { useWorkflowStore } from '@/stores/workflowStore';
import { api } from '@/api/client';
import type { RunArtifact, UnifiedRunEvent } from '@/types/workflow';
import ResultDisplay from './ResultDisplay';

const { Text, Title } = Typography;

type WorkflowEvent = UnifiedRunEvent;

type RunViewerStatus = 'connecting' | 'running' | 'completed' | 'failed' | 'canceled' | 'interrupted';

function toRunViewerStatus(status?: string | null): RunViewerStatus {
  if (status === 'succeeded') return 'completed';
  if (status === 'failed') return 'failed';
  if (status === 'cancelled' || status === 'timed_out') return 'canceled';
  if (status === 'interrupted') return 'interrupted';
  if (status === 'created' || status === 'queued' || status === 'running') return 'running';
  return 'connecting';
}

function formatEventValue(value: any): string {
  if (value === undefined || value === null || value === '') return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (typeof value === 'object') {
    const directMessage = value.message || value.error || value.error_message || value.detail;
    if (directMessage && typeof directMessage !== 'object') {
      return String(directMessage);
    }
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function normalizeWorkflowEvent(event: any): WorkflowEvent | null {
  if (!event || typeof event !== 'object' || !event.type) return null;
  return {
    ...event,
    type: String(event.type),
    data: event.data && typeof event.data === 'object' ? event.data : {},
    timestamp: event.timestamp || new Date().toISOString(),
  };
}

export default function ResultsModal() {
  const {
    selectedRunId,
    runViewerOpen,
    staticRuns,
    staticRunEvents,
    setStaticRunEvents,
    closeRunViewer,
  } = useWorkflowStore();
  const selectedRun = selectedRunId
    ? staticRuns.find((run) => run.run_id === selectedRunId) || null
    : null;
  const [status, setStatus] = useState<RunViewerStatus>('connecting');
  const [results, setResults] = useState<any>(null);
  const [error, setError] = useState<string>('');
  const [events, setEvents] = useState<WorkflowEvent[]>([]);
  const [artifacts, setArtifacts] = useState<RunArtifact[]>([]);

  useEffect(() => {
    if (!runViewerOpen || !selectedRunId) {
      setStatus('connecting');
      setResults(null);
      setError('');
      setEvents([]);
      return undefined;
    }

    if (selectedRun) {
      setStatus(toRunViewerStatus(selectedRun.status));
      setResults(selectedRun.final_result ?? selectedRun.result_summary ?? null);
      setError(formatEventValue(
        selectedRun.error_summary
          ?? selectedRun.failure_reason
          ?? selectedRun.cancel_reason,
      ));
    } else {
      setStatus('connecting');
      setResults(null);
      setError('');
    }

    const knownEvents = staticRunEvents[selectedRunId];
    if (knownEvents !== undefined) {
      setEvents(knownEvents.map(normalizeWorkflowEvent).filter(Boolean) as WorkflowEvent[]);
      return undefined;
    }

    let canceled = false;
    api.getRunEvents(selectedRunId)
      .then((result) => {
        if (canceled) return;
        const nextEvents = (result.events || []).map(normalizeWorkflowEvent).filter(Boolean) as WorkflowEvent[];
        setStaticRunEvents(selectedRunId, nextEvents);
        setEvents(nextEvents);
      })
      .catch((fetchError) => {
        if (!canceled) {
          console.error('Failed to load workflow run events:', fetchError);
        }
      });

    return () => {
      canceled = true;
    };
  }, [runViewerOpen, selectedRun, selectedRunId, setStaticRunEvents, staticRunEvents]);

  useEffect(() => {
    if (!runViewerOpen || !selectedRunId) {
      setArtifacts([]);
      return undefined;
    }

    let canceled = false;
    api.getRunArtifacts(selectedRunId)
      .then((result) => {
        if (!canceled) {
          setArtifacts(result.artifacts || []);
        }
      })
      .catch((fetchError) => {
        if (!canceled) {
          console.error('Failed to load workflow run artifacts:', fetchError);
          setArtifacts([]);
        }
      });

    return () => {
      canceled = true;
    };
  }, [runViewerOpen, selectedRun?.status, selectedRunId]);

  const traceback = events.reduce<string>((latest, event) => (
    formatEventValue(event.data?.traceback) || latest
  ), '');

  const handleClose = () => {
    closeRunViewer();
    setStatus('connecting');
    setResults(null);
    setError('');
    setEvents([]);
    setArtifacts([]);
  };

  const getEventTag = (type: string) => {
    if (type === 'start_workflow') return <Tag color="green">Workflow Started</Tag>;
    if (type === 'start_dynamic_run') return <Tag color="green">Dynamic Run</Tag>;
    if (type === 'register_task_spec') return <Tag color="cyan">Task Spec</Tag>;
    if (type === 'append_task') return <Tag color="blue">Task Appended</Tag>;
    if (type === 'task_ready') return <Tag color="gold">Task Ready</Tag>;
    if (type === 'start_task') return <Tag color="processing">Task Started</Tag>;
    if (type === 'finish_task') return <Tag color="success">Task Finished</Tag>;
    if (type === 'task_exception') return <Tag color="error">Task Failed</Tag>;
    if (type === 'finish_workflow') return <Tag color="success">Workflow Done</Tag>;
    if (type === 'cancel_workflow') return <Tag color="orange">Run Canceled</Tag>;
    if (type === 'timeout_workflow') return <Tag color="volcano">Run Timed Out</Tag>;
    if (type === 'interrupt_workflow') return <Tag color="magenta">Interrupted</Tag>;
    if (type === 'cancel_dynamic_run') return <Tag color="orange">Run Canceled</Tag>;
    if (type === 'timeout_dynamic_run') return <Tag color="volcano">Run Timed Out</Tag>;
    if (type === 'workflow_completed') return <Tag color="success">Workflow Done</Tag>;
    if (type === 'workflow_failed') return <Tag color="error">Workflow Failed</Tag>;
    if (type === 'workflow_canceled') return <Tag color="orange">Workflow Canceled</Tag>;
    if (type === 'workflow_interrupted') return <Tag color="magenta">Interrupted</Tag>;
    if (type === 'stream_warning') return <Tag color="gold">Stream</Tag>;
    if (type === 'building') return <Tag color="blue">Building</Tag>;
    if (type === 'workflow_started') return <Tag color="green">Started</Tag>;
    return <Tag>{type}</Tag>;
  };

  const renderEventSummary = (event: WorkflowEvent) => {
    const data = event.data || {};
    const shortTaskId = data.task_id ? `${String(data.task_id).slice(0, 8)}...` : '';
    if (event.type === 'start_workflow') {
      return 'Workflow started';
    }
    if (event.type === 'start_dynamic_run') {
      return `Dynamic run ${data.run_id ? String(data.run_id).slice(0, 8) : ''} started`;
    }
    if (event.type === 'register_task_spec') {
      return `Task spec registered: ${formatEventValue(data.task_name || data.task_spec_id) || 'unknown'}`;
    }
    if (event.type === 'append_task') {
      return `Task ${shortTaskId} appended${data.status ? ` (${formatEventValue(data.status)})` : ''}`;
    }
    if (event.type === 'task_ready') {
      return `Task ${shortTaskId} is ready to run`;
    }
    if (event.type === 'start_task') {
      const node = data.node_id ? ` on ${String(data.node_id).slice(0, 8)}...` : '';
      return `Task ${shortTaskId} started${node}`;
    }
    if (event.type === 'finish_task') {
      return `Task ${shortTaskId} finished`;
    }
    if (event.type === 'task_exception') {
      return `Task ${shortTaskId} failed: ${formatEventValue(data.error || data.result) || 'Unknown error'}`;
    }
    if (event.type === 'finish_workflow') {
      return 'All workflow tasks finished';
    }
    if (event.type === 'cancel_workflow') {
      return `Workflow run canceled${data.reason ? `: ${formatEventValue(data.reason)}` : ''}`;
    }
    if (event.type === 'timeout_workflow') {
      return `Workflow run timed out${data.timeout_seconds ? ` after ${data.timeout_seconds}s` : ''}`;
    }
    if (event.type === 'interrupt_workflow') {
      return `Workflow run interrupted${data.reason ? `: ${formatEventValue(data.reason)}` : ''}`;
    }
    if (event.type === 'cancel_dynamic_run') {
      return `Dynamic run canceled${data.reason ? `: ${formatEventValue(data.reason)}` : ''}`;
    }
    if (event.type === 'timeout_dynamic_run') {
      return `Dynamic run timed out${data.timeout_seconds ? ` after ${data.timeout_seconds}s` : ''}`;
    }
    if (event.type === 'building') {
      return formatEventValue(data.message) || 'Building workflow...';
    }
    if (event.type === 'workflow_started') {
      return 'Workflow started';
    }
    if (event.type === 'workflow_completed') {
      return 'Workflow completed';
    }
    if (event.type === 'workflow_failed') {
      return formatEventValue(data.error) || 'Workflow failed';
    }
    if (event.type === 'workflow_canceled') {
      return formatEventValue(data.message || data.error) || 'Workflow run was canceled';
    }
    if (event.type === 'workflow_interrupted') {
      return formatEventValue(data.message || data.error) || 'Workflow run was interrupted';
    }
    if (event.type === 'stream_warning') {
      return formatEventValue(data.message) || 'Live result stream disconnected; polling will continue.';
    }
    return formatEventValue(data.message) || event.type.split('_').join(' ');
  };

  const renderEvents = () => (
    <List
      size="small"
      dataSource={events}
      locale={{ emptyText: 'Waiting for execution events...' }}
      style={{
        marginTop: '16px',
        maxHeight: '260px',
        overflow: 'auto',
        border: '1px solid #f0f0f0',
        borderRadius: '6px',
      }}
      renderItem={(event, index) => (
        <List.Item style={{ padding: '8px 12px' }}>
          <Space direction="vertical" size={2} style={{ width: '100%' }}>
            <Space size={8} wrap>
              <Text type="secondary" style={{ width: '28px', fontSize: '12px' }}>
                #{event.seq || index + 1}
              </Text>
              {getEventTag(event.type)}
              {event.data?.run_status && (
                <Tag color="default">{String(event.data.run_status)}</Tag>
              )}
              {event.timestamp && (
                <Text type="secondary" style={{ fontSize: '12px' }}>
                  {new Date(event.timestamp).toLocaleTimeString()}
                </Text>
              )}
            </Space>
            <Text style={{ fontSize: '13px' }}>{renderEventSummary(event)}</Text>
          </Space>
        </List.Item>
      )}
    />
  );

  const renderArtifacts = () => {
    const artifactItems = artifacts.map((artifact) => {
      const task = artifact.task_id
        ? selectedRun?.task_nodes?.[artifact.task_id]
        : undefined;
      return {
        ...artifact,
        taskLabel: task?.task_name || artifact.task_id || 'Workflow',
      };
    });

    if (artifactItems.length === 0) {
      return null;
    }

    return (
      <div style={{ marginTop: '16px' }}>
        <Title level={5}>Files</Title>
        <List
          size="small"
          dataSource={artifactItems}
          renderItem={(artifact: any) => (
            <List.Item
              actions={[
                <Button
                  key="download"
                  size="small"
                  icon={<DownloadOutlined />}
                  disabled={!artifact.sha256}
                  onClick={() => {
                    if (artifact.sha256) {
                      window.open(api.getArtifactDownloadUrl(artifact.sha256), '_blank');
                    }
                  }}
                >
                  Download
                </Button>,
              ]}
            >
              <List.Item.Meta
                title={artifact.path}
                description={`${artifact.taskLabel}${artifact.size ? ` · ${artifact.size} bytes` : ''}`}
              />
            </List.Item>
          )}
        />
      </div>
    );
  };

  const shouldShow = runViewerOpen && !!selectedRunId;

  return (
    <Modal
      title={
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          {status === 'running' && <LoadingOutlined style={{ color: '#1890ff' }} />}
          {status === 'completed' && <CheckCircleOutlined style={{ color: '#52c41a' }} />}
          {status === 'failed' && <CloseCircleOutlined style={{ color: '#ff4d4f' }} />}
          {(status === 'canceled' || status === 'interrupted') && <CloseCircleOutlined style={{ color: '#fa8c16' }} />}
          Workflow Run
          {selectedRunId && (
            <Text type="secondary" style={{ marginLeft: '8px', fontSize: '12px' }}>
              {selectedRunId.slice(0, 8)}...
            </Text>
          )}
        </div>
      }
      open={shouldShow}
      onCancel={handleClose}
      footer={[
        <Button key="close" onClick={handleClose}>
          Close
        </Button>,
      ]}
      width={800}
    >
      {status === 'connecting' && (
        <div style={{ textAlign: 'center', padding: '40px' }}>
          <Spin size="large" />
          <div style={{ marginTop: '16px' }}>
            <Text type="secondary">Loading run...</Text>
          </div>
        </div>
      )}

      {status === 'running' && (
        <div>
          <div style={{ textAlign: 'center', padding: '20px 0 8px' }}>
            <Spin size="large" />
            <div style={{ marginTop: '16px' }}>
              <Text type="secondary">Workflow run is active. You can close this viewer and reopen it from Runs.</Text>
            </div>
          </div>
          {renderEvents()}
        </div>
      )}

      {status === 'completed' && (
        <div>
          <Alert
            message="Workflow completed successfully"
            type="success"
            showIcon
            style={{ marginBottom: '16px' }}
          />
          {renderEvents()}
          {renderArtifacts()}
          {(results ?? selectedRun?.final_result ?? selectedRun?.result_summary) != null && (
            <Tabs
              defaultActiveKey="formatted"
              style={{ marginTop: '16px' }}
              items={[
                {
                  key: 'formatted',
                  label: (
                    <span>
                      <FileTextOutlined />
                      Formatted
                    </span>
                  ),
                  children: (
                    <div style={{
                      maxHeight: '500px',
                      overflow: 'auto'
                    }}>
                      <ResultDisplay data={results ?? selectedRun?.final_result ?? selectedRun?.result_summary} />
                    </div>
                  ),
                },
                {
                  key: 'raw',
                  label: (
                    <span>
                      <CodeOutlined />
                      Raw JSON
                    </span>
                  ),
                  children: (
                    <div style={{
                      background: '#f5f5f5',
                      padding: '16px',
                      borderRadius: '4px',
                      maxHeight: '500px',
                      overflow: 'auto'
                    }}>
                      <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordWrap: 'break-word', fontSize: '12px' }}>
                        {JSON.stringify(results ?? selectedRun?.final_result ?? selectedRun?.result_summary, null, 2)}
                      </pre>
                    </div>
                  ),
                },
              ]}
            />
          )}
        </div>
      )}

      {(status === 'failed' || status === 'canceled' || status === 'interrupted') && (
        <div>
          <Alert
            message={
              status === 'failed'
                ? 'Workflow execution failed'
                : status === 'canceled'
                  ? 'Workflow run was canceled'
                  : 'Workflow run was interrupted'
            }
            description={
              error || (
                status === 'interrupted'
                  ? 'The backend stopped before this run finished.'
                  : undefined
              )
            }
            type={status === 'failed' ? 'error' : 'warning'}
            showIcon
            style={{ marginBottom: '16px' }}
          />
          {renderEvents()}
          {traceback && (
            <>
              <Title level={5}>Error Details</Title>
              <div style={{
                background: '#fff2f0',
                padding: '12px',
                borderRadius: '4px',
                maxHeight: '300px',
                overflow: 'auto',
                border: '1px solid #ffccc7'
              }}>
                <pre style={{ margin: 0, fontSize: '12px', color: '#cf1322' }}>
                  {traceback}
                </pre>
              </div>
            </>
          )}
        </div>
      )}
    </Modal>
  );
}
