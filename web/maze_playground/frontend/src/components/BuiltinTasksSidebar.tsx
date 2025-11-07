import { Card, List, Tag, Empty, Button, Space, Divider, message } from 'antd';
import { ThunderboltOutlined, ToolOutlined, ReloadOutlined, PlusOutlined, CodeOutlined } from '@ant-design/icons';
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
      message.success('刷新成功');
    } catch (error) {
      console.error('加载内置任务失败:', error);
      message.error('刷新失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ width: '280px', borderRight: '1px solid #f0f0f0', background: '#fafafa', overflowY: 'auto' }}>
      <div style={{ padding: '16px' }}>
        {/* 自定义任务区域 */}
        <h3 style={{ marginBottom: '12px' }}>自定义任务</h3>
        <p style={{ fontSize: '12px', color: '#999', marginBottom: '8px' }}>
          拖拽到画布创建自定义任务
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
              <div style={{ fontWeight: 'bold', marginBottom: '4px' }}>自定义任务</div>
              <div style={{ fontSize: '12px', opacity: 0.9 }}>编写自己的任务代码</div>
            </div>
          </div>
        </Card>

        <Divider />

        {/* 内置任务区域 */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
          <h3 style={{ margin: 0 }}>内置任务</h3>
          <Button 
            type="text" 
            size="small"
            icon={<ReloadOutlined />}
            onClick={handleRefresh}
            loading={loading}
          >
            刷新
          </Button>
        </div>
        <p style={{ fontSize: '12px', color: '#999', marginBottom: '12px' }}>
          拖拽任务到画布上
        </p>
        {builtinTasks.length === 0 ? (
          <Empty 
            description="暂无内置任务" 
            image={Empty.PRESENTED_IMAGE_SIMPLE}
          >
            <Button 
              type="primary" 
              size="small"
              icon={<ReloadOutlined />}
              onClick={handleRefresh}
            >
              加载内置任务
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
                  {task.nodeType === 'task' ? (
                    <ThunderboltOutlined style={{ color: '#1890ff' }} />
                  ) : (
                    <ToolOutlined style={{ color: '#52c41a' }} />
                  )}
                  <strong>{task.displayName}</strong>
                </div>
                <Tag color={task.nodeType === 'task' ? 'blue' : 'green'}>{task.nodeType}</Tag>
              </Card>
            )}
          />
        )}
      </div>
    </div>
  );
}

