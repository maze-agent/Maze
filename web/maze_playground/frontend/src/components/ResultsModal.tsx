import { useEffect, useRef, useState } from 'react';
import { Modal, Typography, Spin, Alert, Button, Tabs, List, Tag, Space } from 'antd';
import { CheckCircleOutlined, CloseCircleOutlined, LoadingOutlined, FileTextOutlined, CodeOutlined, DownloadOutlined } from '@ant-design/icons';
import { useWorkflowStore } from '@/stores/workflowStore';
import { api } from '@/api/client';
import ResultDisplay from './ResultDisplay';

const { Text, Title } = Typography;

interface WorkflowEvent {
  type: string;
  data?: Record<string, any>;
  seq?: number;
  timestamp?: string;
}

type RunViewerStatus = 'connecting' | 'running' | 'completed' | 'failed' | 'canceled' | 'interrupted';

function toRunViewerStatus(status?: string | null): RunViewerStatus {
  if (status === 'completed' || status === 'succeeded') return 'completed';
  if (status === 'failed') return 'failed';
  if (status === 'canceled' || status === 'cancelled' || status === 'timed_out') return 'canceled';
  if (status === 'interrupted') return 'interrupted';
  return 'running';
}

function statusFromEvent(eventType?: string | null): RunViewerStatus | null {
  if (eventType === 'workflow_completed') return 'completed';
  if (eventType === 'workflow_failed' || eventType === 'task_exception') return 'failed';
  if (eventType === 'workflow_canceled' || eventType === 'timeout_dynamic_run') return 'canceled';
  if (eventType === 'workflow_interrupted') return 'interrupted';
  return null;
}

function isTerminalRunStatus(status?: string | null): boolean {
  return ['completed', 'succeeded', 'failed', 'canceled', 'cancelled', 'timed_out', 'interrupted'].includes(status || '');
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
    workflowId,
    isRunning,
    activeRunId,
    selectedRunId,
    runViewerOpen,
    staticRuns,
    staticRunEvents,
    workspaceDir,
    upsertStaticRun,
    addStaticRunEvent,
    setStaticRunEvents,
    closeRunViewer,
  } = useWorkflowStore();
  const [status, setStatus] = useState<RunViewerStatus>('connecting');
  const [results, setResults] = useState<any>(null);
  const [error, setError] = useState<string>('');
  const [traceback, setTraceback] = useState<string>('');
  const [ws, setWs] = useState<WebSocket | null>(null);
  const [events, setEvents] = useState<WorkflowEvent[]>([]);
  const manualSocketCloseRef = useRef(false);
  const terminalRunSeenRef = useRef(false);

  useEffect(() => {
    if (isRunning && workflowId && activeRunId) {
      manualSocketCloseRef.current = false;
      terminalRunSeenRef.current = false;
      setEvents([]);
      setResults(null);
      setError('');
      setTraceback('');

      const websocket = api.connectWebSocket(workflowId, {
        onConnected: () => {
          setStatus('running');
          setEvents((prev) => [...prev, { type: 'connected', timestamp: new Date().toISOString() }]);
        },
        onWorkflowStarted: () => {
          setStatus('running');
          setEvents((prev) => [...prev, { type: 'workflow_started', timestamp: new Date().toISOString() }]);
        },
        onBuilding: (message) => {
          setStatus('running');
          setEvents((prev) => [...prev, { type: 'building', data: { message }, timestamp: new Date().toISOString() }]);
        },
        onTaskUpdate: (event) => {
          const nextEvent = normalizeWorkflowEvent(event);
          if (!nextEvent) return;
          const eventStatus = statusFromEvent(nextEvent.type);
          if (eventStatus) {
            terminalRunSeenRef.current = true;
            setStatus(eventStatus);
          } else {
            setStatus((prev) => (isTerminalRunStatus(prev) ? prev : 'running'));
          }
          setEvents((prev) => [...prev, nextEvent]);
          if (activeRunId) {
            addStaticRunEvent(activeRunId, nextEvent);
          }
        },
        onRunUpdate: (payload) => {
          if (payload.run) {
            upsertStaticRun(payload.run);
            const nextStatus = toRunViewerStatus(payload.run.status);
            if (isTerminalRunStatus(payload.run.status)) {
              terminalRunSeenRef.current = true;
            }
            setStatus(nextStatus);
            if (payload.run.final_result) {
              setResults(payload.run.final_result);
            }
            if (payload.run.error) {
              setError(formatEventValue(payload.run.error));
            }
          }
        },
        onWorkflowCompleted: (res) => {
          terminalRunSeenRef.current = true;
          setStatus('completed');
          setResults(res);
          setEvents((prev) => [...prev, { type: 'workflow_completed', timestamp: new Date().toISOString() }]);
        },
        onWorkflowFailed: (err, trace) => {
          terminalRunSeenRef.current = true;
          setStatus('failed');
          setError(formatEventValue(err));
          setTraceback(trace || '');
          setEvents((prev) => [...prev, { type: 'workflow_failed', data: { error: err }, timestamp: new Date().toISOString() }]);
        },
        onError: (event, websocket) => {
          console.warn('Workflow result stream unavailable; polling will continue.', event);
          if (
            manualSocketCloseRef.current ||
            terminalRunSeenRef.current ||
            websocket.readyState === WebSocket.CLOSING ||
            websocket.readyState === WebSocket.CLOSED
          ) {
            return;
          }
          setStatus((prev) => (prev === 'connecting' ? 'running' : prev));
          setEvents((prev) => [
            ...prev,
            {
              type: 'stream_warning',
              data: { message: 'Live result stream disconnected; polling will continue.' },
              timestamp: new Date().toISOString(),
            },
          ]);
        },
        onClose: (event) => {
          if (event.wasClean || manualSocketCloseRef.current || terminalRunSeenRef.current) {
            return;
          }
          setEvents((prev) => [
            ...prev,
            {
              type: 'stream_warning',
              data: { message: 'Live result stream closed; polling will continue.' },
              timestamp: new Date().toISOString(),
            },
          ]);
        },
      });

      setWs(websocket);

      return () => {
        manualSocketCloseRef.current = true;
        if (websocket.readyState === WebSocket.OPEN) {
          websocket.close(1000, 'viewer cleanup');
        }
      };
    }
  }, [activeRunId, addStaticRunEvent, isRunning, upsertStaticRun, workflowId]);

  useEffect(() => {
    if (!runViewerOpen || !selectedRunId) {
      return;
    }

    const selectedRun = staticRuns.find((run) => run.run_id === selectedRunId);
    if (selectedRun) {
      setStatus(toRunViewerStatus(selectedRun.status));
      setResults(selectedRun.final_result || null);
      setError(formatEventValue(selectedRun.error));
    }

    const knownEvents = staticRunEvents[selectedRunId];
    if (knownEvents) {
      setEvents(knownEvents.map(normalizeWorkflowEvent).filter(Boolean) as WorkflowEvent[]);
      return;
    }

    api.getStaticWorkflowRunEvents(selectedRunId, workspaceDir || undefined)
      .then((result) => {
        const nextEvents = (result.events || []).map(normalizeWorkflowEvent).filter(Boolean) as WorkflowEvent[];
        setStaticRunEvents(selectedRunId, nextEvents);
        setEvents(nextEvents);
      })
      .catch((error) => {
        console.error('Failed to load workflow run events:', error);
      });
  }, [runViewerOpen, selectedRunId, setStaticRunEvents, staticRunEvents, staticRuns, workspaceDir]);

  const handleClose = () => {
    manualSocketCloseRef.current = true;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.close(1000, 'viewer closed');
    }
    closeRunViewer();
    setStatus('connecting');
    setResults(null);
    setError('');
    setTraceback('');
    setEvents([]);
  };

  const getEventTag = (type: string) => {
    if (type === 'start_dynamic_run') return <Tag color="green">Dynamic Run</Tag>;
    if (type === 'register_task_spec') return <Tag color="cyan">Task Spec</Tag>;
    if (type === 'append_task') return <Tag color="blue">Task Appended</Tag>;
    if (type === 'task_ready') return <Tag color="gold">Task Ready</Tag>;
    if (type === 'start_task') return <Tag color="processing">Task Started</Tag>;
    if (type === 'finish_task') return <Tag color="success">Task Finished</Tag>;
    if (type === 'task_exception') return <Tag color="error">Task Failed</Tag>;
    if (type === 'finish_workflow') return <Tag color="success">Workflow Done</Tag>;
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
    return 'Connected to result stream';
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
    const artifactItems = Object.values(selectedRun?.task_nodes || {}).flatMap((node: any) => (
      (node.artifacts || []).map((artifact: any) => ({
        ...artifact,
        taskId: node.maze_task_id || node.node_id,
        taskLabel: node.label || node.task_name || node.node_id,
      }))
    ));

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
                  onClick={() => {
                    window.open(
                      api.getStaticRunArtifactDownloadUrl(
                        selectedRunId || '',
                        artifact.taskId,
                        artifact.path,
                        workspaceDir || undefined,
                      ),
                      '_blank',
                    );
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

  const selectedRun = selectedRunId ? staticRuns.find((run) => run.run_id === selectedRunId) : null;
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
            <Text type="secondary">Connecting...</Text>
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
          {(results || selectedRun?.final_result) && (
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
                      <ResultDisplay data={results || selectedRun?.final_result} />
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
                        {JSON.stringify(results || selectedRun?.final_result, null, 2)}
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
