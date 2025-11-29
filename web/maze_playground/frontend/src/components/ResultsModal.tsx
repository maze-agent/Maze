import { useEffect, useState } from 'react';
import { Modal, Typography, Spin, Alert, Button, Tabs } from 'antd';
import { CheckCircleOutlined, CloseCircleOutlined, LoadingOutlined, FileTextOutlined, CodeOutlined } from '@ant-design/icons';
import { useWorkflowStore } from '@/stores/workflowStore';
import { api } from '@/api/client';
import ResultDisplay from './ResultDisplay';

const { Text, Title } = Typography;

export default function ResultsModal() {
  const { workflowId, isRunning, setIsRunning, clearRunResults } = useWorkflowStore();
  const [status, setStatus] = useState<'connecting' | 'running' | 'completed' | 'failed'>('connecting');
  const [results, setResults] = useState<any>(null);
  const [error, setError] = useState<string>('');
  const [traceback, setTraceback] = useState<string>('');
  const [ws, setWs] = useState<WebSocket | null>(null);

  useEffect(() => {
    if (isRunning && workflowId) {
      const websocket = api.connectWebSocket(workflowId, {
        onConnected: () => {
          setStatus('running');
        },
        onWorkflowStarted: () => {
          setStatus('running');
        },
        onBuilding: () => {
          setStatus('running');
        },
        onWorkflowCompleted: (res) => {
          setStatus('completed');
          setResults(res);
        },
        onWorkflowFailed: (err, trace) => {
          setStatus('failed');
          setError(err);
          setTraceback(trace || '');
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
  };

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
        <div style={{ textAlign: 'center', padding: '40px' }}>
          <Spin size="large" />
          <div style={{ marginTop: '16px' }}>
            <Text type="secondary">Workflow is running, please wait...</Text>
          </div>
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
          <Tabs
            defaultActiveKey="formatted"
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
