import { Button, Space, Typography, message } from 'antd';
import { PlayCircleOutlined, PlusOutlined, SaveOutlined, ProjectOutlined } from '@ant-design/icons';
import { useWorkflowStore } from '@/stores/workflowStore';
import { api } from '@/api/client';

const { Text } = Typography;

export default function Toolbar() {
  const { 
    workflowId, 
    workflowName, 
    nodes, 
    edges, 
    isRunning,
    setWorkflowId,
    setIsRunning,
    clearRunResults,
  } = useWorkflowStore();

  // 创建新工作流
  const handleCreateWorkflow = async () => {
    try {
      const { workflowId: newId } = await api.createWorkflow();
      setWorkflowId(newId);
      message.success('工作流创建成功');
    } catch (error) {
      console.error('创建工作流失败:', error);
      message.error('创建工作流失败');
    }
  };

  // 保存工作流
  const handleSaveWorkflow = async () => {
    if (!workflowId) {
      message.warning('请先创建工作流');
      return;
    }

    try {
      await api.saveWorkflow(workflowId, { nodes, edges });
      message.success('工作流保存成功');
    } catch (error) {
      console.error('保存工作流失败:', error);
      message.error('保存工作流失败');
    }
  };

  // 运行工作流
  const handleRunWorkflow = async () => {
    if (!workflowId) {
      message.warning('请先创建工作流');
      return;
    }

    if (nodes.length === 0) {
      message.warning('请至少添加一个任务节点');
      return;
    }

    // 检查是否所有节点都已配置
    const unconfiguredNodes = nodes.filter(n => !n.data.configured);
    if (unconfiguredNodes.length > 0) {
      message.warning('有节点未配置，请先配置所有节点');
      return;
    }

    try {
      // 先保存工作流
      await api.saveWorkflow(workflowId, { nodes, edges });
      
      // 设置运行状态，触发 ResultsModal 显示
      setIsRunning(true);
      clearRunResults();
      
      // 发起运行请求（ResultsModal 会自动建立 WebSocket 连接）
      await api.runWorkflow(workflowId);
      message.info('工作流已开始运行');
      
    } catch (error) {
      console.error('运行工作流失败:', error);
      message.error('运行工作流失败');
      setIsRunning(false);
    }
  };

  return (
    <div
      style={{
        height: '60px',
        borderBottom: '1px solid #f0f0f0',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '0 24px',
        background: '#fff',
      }}
    >
      {/* 左侧 */}
      <Space size="large">
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <ProjectOutlined style={{ fontSize: '24px', color: '#1890ff' }} />
          <Text strong style={{ fontSize: '16px' }}>
            Maze Workflow Playground
          </Text>
        </div>
        
        {workflowId && (
          <Text type="secondary" style={{ fontSize: '14px' }}>
            {workflowName} ({workflowId.substring(0, 8)}...)
          </Text>
        )}
      </Space>

      {/* 右侧操作按钮 */}
      <Space>
        <Button 
          type="primary" 
          icon={<PlusOutlined />}
          onClick={handleCreateWorkflow}
        >
          新建工作流
        </Button>
        
        <Button 
          icon={<SaveOutlined />}
          onClick={handleSaveWorkflow}
          disabled={!workflowId}
        >
          保存
        </Button>
        
        <Button 
          type="primary"
          icon={<PlayCircleOutlined />}
          onClick={handleRunWorkflow}
          disabled={!workflowId || isRunning}
          loading={isRunning}
          style={{ background: '#52c41a', borderColor: '#52c41a' }}
        >
          {isRunning ? '运行中...' : '运行'}
        </Button>
      </Space>
    </div>
  );
}

