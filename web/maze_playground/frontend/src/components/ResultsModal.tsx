import { useEffect, useState } from 'react';
import { Modal, Typography, Spin, Alert, Button, Tabs, List, Tag, Space } from 'antd';
import { CheckCircleOutlined, CloseCircleOutlined, LoadingOutlined, FileTextOutlined, CodeOutlined } from '@ant-design/icons';
import { useWorkflowStore } from '@/stores/workflowStore';
import { api } from '@/api/client';
import ResultDisplay from './ResultDisplay';

const { Text, Title } = Typography;

interface WorkflowEvent {
  type: string;
  data?: Record<string, any>;
  timestamp?: string;
}

export default function ResultsModal() {
  const { workflowId, isRunning, setIsRunning, clearRunResults } = useWorkflowStore();
  const [status, setStatus] = useState<'connecting' | 'running' | 'completed' | 'failed'>('connecting');
  const [results, setResults] = useState<any>(null);
  const [error, setError] = useState<string>('');
  const [traceback, setTraceback] = useState<string>('');
  const [ws, setWs] = useState<WebSocket | null>(null);
  const [events, setEvents] = useState<WorkflowEvent[]>([]);

  useEffect(() => {
    if (isRunning && workflowId) {
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
          setStatus('running');
          setEvents((prev) => [...prev, { ...event, timestamp: new Date().toISOString() }]);
        },
        onWorkflowCompleted: (res) => {
          setStatus('completed');
          setResults(res);
          setEvents((prev) => [...prev, { type: 'workflow_completed', timestamp: new Date().toISOString() }]);
        },
        onWorkflowFailed: (err, trace) => {
          setStatus('failed');
          setError(err);
          setTraceback(trace || '');
          setEvents((prev) => [...prev, { type: 'workflow_failed', data: { error: err }, timestamp: new Date().toISOString() }]);
        },
        onError: (error) => {
          console.error('WebSocket error:', error);
          setStatus('failed');
          setError('WebSocket connection failed');
        },
      });

      setWs(websocket);

      return () => {
        if (websocket.readyState === WebSocket.OPEN) {
          websocket.close();
        }
      };
    }
  }, [isRunning, workflowId]);

  const handleClose = () => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.close();
    }
    setIsRunning(false);
    clearRunResults();
    setStatus('connecting');
    setResults(null);
    setError('');
    setTraceback('');
    setEvents([]);
  };

  const getEventTag = (type: string) => {
    if (type === 'start_task') return <Tag color="processing">Task Started</Tag>;
    if (type === 'finish_task') return <Tag color="success">Task Finished</Tag>;
    if (type === 'task_exception') return <Tag color="error">Task Failed</Tag>;
    if (type === 'finish_workflow') return <Tag color="success">Workflow Done</Tag>;
    if (type === 'workflow_completed') return <Tag color="success">Workflow Done</Tag>;
    if (type === 'workflow_failed') return <Tag color="error">Workflow Failed</Tag>;
    if (type === 'building') return <Tag color="blue">Building</Tag>;
    if (type === 'workflow_started') return <Tag color="green">Started</Tag>;
    return <Tag>{type}</Tag>;
  };

  const renderEventSummary = (event: WorkflowEvent) => {
    const data = event.data || {};
    const shortTaskId = data.task_id ? `${String(data.task_id).slice(0, 8)}...` : '';
    if (event.type === 'start_task') {
      const node = data.node_id ? ` on ${String(data.node_id).slice(0, 8)}...` : '';
      return `Task ${shortTaskId} started${node}`;
    }
    if (event.type === 'finish_task') {
      return `Task ${shortTaskId} finished`;
    }
    if (event.type === 'task_exception') {
      return `Task ${shortTaskId} failed: ${data.result || 'Unknown error'}`;
    }
    if (event.type === 'finish_workflow') {
      return 'All workflow tasks finished';
    }
    if (event.type === 'building') {
      return data.message || 'Building workflow...';
    }
    if (event.type === 'workflow_started') {
      return 'Workflow started';
    }
    if (event.type === 'workflow_completed') {
      return 'Workflow completed';
    }
    if (event.type === 'workflow_failed') {
      return data.error || 'Workflow failed';
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
                #{index + 1}
              </Text>
              {getEventTag(event.type)}
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

  const shouldShow = isRunning || status === 'completed' || status === 'failed';

  return (
    <Modal
      title={
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          {status === 'running' && <LoadingOutlined style={{ color: '#1890ff' }} />}
          {status === 'completed' && <CheckCircleOutlined style={{ color: '#52c41a' }} />}
          {status === 'failed' && <CloseCircleOutlined style={{ color: '#ff4d4f' }} />}
          Workflow Results
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
              <Text type="secondary">Workflow is running. Task events will appear below.</Text>
            </div>
          </div>
          {renderEvents()}
        </div>
      )}

      {status === 'completed' && results && (
        <div>
          <Alert
            message="Workflow completed successfully"
            type="success"
            showIcon
            style={{ marginBottom: '16px' }}
          />
          {renderEvents()}
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
                    <ResultDisplay data={results} />
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
                      {JSON.stringify(results, null, 2)}
                    </pre>
                  </div>
                ),
              },
            ]}
          />
        </div>
      )}

      {status === 'failed' && (
        <div>
          <Alert
            message="Workflow execution failed"
            description={error}
            type="error"
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
