import { Card, List, Tag, Empty, Button, Space, Divider, message } from 'antd';
import { ThunderboltOutlined, ReloadOutlined, PlusOutlined, CodeOutlined } from '@ant-design/icons';
import { useWorkflowStore } from '@/stores/workflowStore';
import type { BuiltinTaskMeta } from '@/types/workflow';
import { api } from '@/api/client';
import { useState } from 'react';

export default function BuiltinTasksSidebar() {
  const { builtinTasks, setBuiltinTasks, workflowId } = useWorkflowStore();
  const [loading, setLoading] = useState(false);

  const onDragStart = (event: React.DragEvent, task: BuiltinTaskMeta) => {
    event.dataTransfer.setData('application/reactflow', JSON.stringify({
      type: 'builtin',
      task: task
    }));
    event.dataTransfer.effectAllowed = 'move';
  };

  const onDragStartCustom = (event: React.DragEvent) => {
    event.dataTransfer.setData('application/reactflow', JSON.stringify({
      type: 'custom'
    }));
    event.dataTransfer.effectAllowed = 'move';
  };

  const handleRefresh = async () => {
    setLoading(true);
    try {
      const tasks = await api.getBuiltinTasks();
      setBuiltinTasks(tasks);
      message.success('Refresh successful');
    } catch (error) {
      console.error('Failed to load builtin tasks:', error);
      message.error('Refresh failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ width: '280px', borderRight: '1px solid #f0f0f0', background: '#fafafa', overflowY: 'auto' }}>
      <div style={{ padding: '16px' }}>
        {/* Custom Task Section */}
        <h3 style={{ marginBottom: '12px' }}>Custom Task</h3>
        <p style={{ fontSize: '12px', color: '#999', marginBottom: '8px' }}>
          Drag to canvas to create custom task
        </p>
        <Card
          size="small"
          style={{ 
            marginBottom: '16px', 
            cursor: 'grab',
            background: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
            border: 'none',
            color: 'white'
          }}
          hoverable
          draggable
          onDragStart={onDragStartCustom}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <CodeOutlined style={{ fontSize: '20px' }} />
            <div>
              <div style={{ fontWeight: 'bold', marginBottom: '4px' }}>Custom Task</div>
              <div style={{ fontSize: '12px', opacity: 0.9 }}>Write your own task code</div>
            </div>
          </div>
        </Card>

        <Divider />

        {/* Builtin Tasks Section */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
          <h3 style={{ margin: 0 }}>Builtin Tasks</h3>
          <Button 
            type="text" 
            size="small"
            icon={<ReloadOutlined />}
            onClick={handleRefresh}
            loading={loading}
          >
            Refresh
          </Button>
        </div>
        <p style={{ fontSize: '12px', color: '#999', marginBottom: '12px' }}>
          Drag tasks to the canvas
        </p>
        {builtinTasks.length === 0 ? (
          <Empty 
            description="No builtin tasks" 
            image={Empty.PRESENTED_IMAGE_SIMPLE}
          >
            <Button 
              type="primary" 
              size="small"
              icon={<ReloadOutlined />}
              onClick={handleRefresh}
            >
              Load Builtin Tasks
            </Button>
          </Empty>
        ) : (
          <List
            dataSource={builtinTasks}
            renderItem={(task) => (
              <Card
                size="small"
                style={{ marginBottom: '8px', cursor: 'grab' }}
                hoverable
                draggable
                onDragStart={(e) => onDragStart(e, task)}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
                  <ThunderboltOutlined style={{ color: '#1890ff' }} />
                  <strong>{task.displayName}</strong>
                </div>
                <Tag color="blue">task</Tag>
              </Card>
            )}
          />
        )}
      </div>
    </div>
  );
}
