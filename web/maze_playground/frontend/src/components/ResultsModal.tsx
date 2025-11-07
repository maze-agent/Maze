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
      console.log('[ResultsModal] 建立 WebSocket 连接:', workflowId);
      // 建立 WebSocket 连接
      const websocket = api.connectWebSocket(workflowId, {
        onConnected: () => {
          console.log('[ResultsModal] WebSocket 已连接');
          setStatus('running');
        },
        onWorkflowStarted: () => {
          console.log('[ResultsModal] 工作流开始运行');
          setStatus('running');
        },
        onBuilding: () => {
          console.log('[ResultsModal] 工作流构建中');
          setStatus('running');
        },
        onWorkflowCompleted: (res) => {
          console.log('[ResultsModal] 工作流完成，结果:', res);
          setStatus('completed');
          setResults(res);
          // 不要立即关闭 isRunning，保持 Modal 打开
          // setIsRunning(false); // 移除这行
        },
        onWorkflowFailed: (err, trace) => {
          console.log('[ResultsModal] 工作流失败:', err);
          setStatus('failed');
          setError(err);
          setTraceback(trace || '');
          // setIsRunning(false); // 移除这行
        },
        onError: (error) => {
          console.error('[ResultsModal] WebSocket 错误:', error);
          setStatus('failed');
          setError('WebSocket 连接失败');
          // setIsRunning(false); // 移除这行
        },
      });

      setWs(websocket);

      return () => {
        console.log('[ResultsModal] 清理 WebSocket 连接');
        if (websocket.readyState === WebSocket.OPEN) {
          websocket.close();
        }
      };
    }
  }, [isRunning, workflowId]);

  const handleClose = () => {
    console.log('[ResultsModal] 用户关闭窗口');
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.close();
    }
    // 重置所有状态
    setIsRunning(false);
    clearRunResults();
    setStatus('connecting');
    setResults(null);
    setError('');
    setTraceback('');
  };

  // Modal 应该在以下情况显示：
  // 1. isRunning=true (工作流正在运行)
  // 2. status='completed' (工作流完成但未关闭)
  // 3. status='failed' (工作流失败但未关闭)
  const shouldShow = isRunning || status === 'completed' || status === 'failed';
  
  console.log('[ResultsModal] 渲染状态:', { isRunning, status, shouldShow, hasResults: !!results });

  return (
    <Modal
      title={
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          {status === 'running' && <LoadingOutlined style={{ color: '#1890ff' }} />}
          {status === 'completed' && <CheckCircleOutlined style={{ color: '#52c41a' }} />}
          {status === 'failed' && <CloseCircleOutlined style={{ color: '#ff4d4f' }} />}
          工作流运行结果
        </div>
      }
      open={shouldShow}
      onCancel={handleClose}
      footer={[
        <Button key="close" onClick={handleClose}>
          关闭
        </Button>,
      ]}
      width={800}
    >
      {status === 'connecting' && (
        <div style={{ textAlign: 'center', padding: '40px' }}>
          <Spin size="large" />
          <div style={{ marginTop: '16px' }}>
            <Text type="secondary">正在连接...</Text>
          </div>
        </div>
      )}

      {status === 'running' && (
        <div style={{ textAlign: 'center', padding: '40px' }}>
          <Spin size="large" />
          <div style={{ marginTop: '16px' }}>
            <Text type="secondary">工作流正在运行中，请稍候...</Text>
          </div>
        </div>
      )}

      {status === 'completed' && results && (
        <div>
          <Alert
            message="工作流执行成功"
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
                    友好展示
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
                    原始 JSON
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
            message="工作流执行失败"
            description={error}
            type="error"
            showIcon
            style={{ marginBottom: '16px' }}
          />
          {traceback && (
            <>
              <Title level={5}>错误详情</Title>
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

