import { Handle, Position } from 'reactflow';
import { Card, Tag } from 'antd';
import { ThunderboltOutlined, ToolOutlined, CodeOutlined } from '@ant-design/icons';

export default function CustomNode({ data, selected }: any) {
  const isCustom = data.category === 'custom';
  const isConfigured = data.configured;
  
  // 自定义任务使用紫色主题，内置任务使用绿色主题
  const getBorderColor = () => {
    if (selected) return '#faad14';
    if (isCustom) {
      return isConfigured ? '#722ed1' : '#d9d9d9';
    }
    return isConfigured ? '#52c41a' : '#d9d9d9';
  };
  
  const getBackgroundGradient = () => {
    if (isCustom && isConfigured) {
      return 'linear-gradient(135deg, #f5f0ff 0%, #fafafa 100%)';
    }
    return 'white';
  };
  
  return (
    <Card
      size="small"
      style={{
        minWidth: '200px',
        borderWidth: '2px',
        borderColor: getBorderColor(),
        boxShadow: selected ? '0 0 0 2px rgba(250, 173, 20, 0.2)' : undefined,
        background: getBackgroundGradient(),
      }}
    >
      {/* 输入端点 */}
      <Handle 
        type="target" 
        position={Position.Left}
        style={{
          width: '12px',
          height: '12px',
          background: '#1890ff',
          border: '2px solid white',
        }}
      />
      
      <div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
          {isCustom ? (
            <CodeOutlined style={{ color: '#722ed1' }} />
          ) : data.nodeType === 'task' ? (
            <ThunderboltOutlined style={{ color: '#1890ff' }} />
          ) : (
            <ToolOutlined style={{ color: '#52c41a' }} />
          )}
          <strong style={{ fontSize: '14px' }}>{data.label}</strong>
        </div>
        
        <div style={{ display: 'flex', gap: '4px', flexWrap: 'wrap' }}>
          {isCustom ? (
            <Tag color="purple" style={{ margin: 0 }}>自定义</Tag>
          ) : (
            <Tag color={data.nodeType === 'task' ? 'blue' : 'green'} style={{ margin: 0 }}>
              {data.nodeType}
            </Tag>
          )}
          {!isConfigured && (
            <Tag color="warning" style={{ margin: 0 }}>未配置</Tag>
          )}
        </div>
        
        {/* 显示输入输出数量 */}
        {isConfigured && (
          <div style={{ marginTop: '8px', fontSize: '12px', color: '#666' }}>
            <div>📥 输入: {data.inputs?.length || 0}</div>
            <div>📤 输出: {data.outputs?.length || 0}</div>
          </div>
        )}
      </div>
      
      {/* 输出端点 */}
      <Handle 
        type="source" 
        position={Position.Right}
        style={{
          width: '12px',
          height: '12px',
          background: '#52c41a',
          border: '2px solid white',
        }}
      />
    </Card>
  );
}

