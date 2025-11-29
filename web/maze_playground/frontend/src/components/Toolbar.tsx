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

  const handleCreateWorkflow = async () => {
    try {
      const { workflowId: newId } = await api.createWorkflow();
      setWorkflowId(newId);
      message.success('Workflow created successfully');
    } catch (error) {
      console.error('Failed to create workflow:', error);
      message.error('Failed to create workflow');
    }
  };

  const handleSaveWorkflow = async () => {
    if (!workflowId) {
      message.warning('Please create a workflow first');
      return;
    }

    try {
      await api.saveWorkflow(workflowId, { nodes, edges });
      message.success('Workflow saved successfully');
    } catch (error) {
      console.error('Failed to save workflow:', error);
      message.error('Failed to save workflow');
    }
  };

  const handleRunWorkflow = async () => {
    if (!workflowId) {
      message.warning('Please create a workflow first');
      return;
    }

    if (nodes.length === 0) {
      message.warning('Please add at least one task node');
      return;
    }

    const unconfiguredNodes = nodes.filter(n => !n.data.configured);
    if (unconfiguredNodes.length > 0) {
      message.warning('Some nodes are not configured, please configure all nodes first');
      return;
    }

    try {
      await api.saveWorkflow(workflowId, { nodes, edges });
      setIsRunning(true);
      clearRunResults();
      await api.runWorkflow(workflowId);
      message.info('Workflow started running');
      
    } catch (error) {
      console.error('Failed to run workflow:', error);
      message.error('Failed to run workflow');
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

      <Space>
        <Button 
          type="primary" 
          icon={<PlusOutlined />}
          onClick={handleCreateWorkflow}
        >
          New Workflow
        </Button>
        
        <Button 
          icon={<SaveOutlined />}
          onClick={handleSaveWorkflow}
          disabled={!workflowId}
        >
          Save
        </Button>
        
        <Button 
          type="primary"
          icon={<PlayCircleOutlined />}
          onClick={handleRunWorkflow}
          disabled={!workflowId || isRunning}
          loading={isRunning}
          style={{ background: '#52c41a', borderColor: '#52c41a' }}
        >
          {isRunning ? 'Running...' : 'Run'}
        </Button>
      </Space>
    </div>
  );
}
