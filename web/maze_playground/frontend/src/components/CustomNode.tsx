import { KeyboardEvent, useEffect, useState } from 'react';
import { Handle, Position } from 'reactflow';
import { Card, Input, Tag } from 'antd';
import { ThunderboltOutlined, CodeOutlined } from '@ant-design/icons';
import { useWorkflowStore } from '@/stores/workflowStore';

export default function CustomNode({ id, data, selected }: any) {
  const { updateNode } = useWorkflowStore();
  const [editingLabel, setEditingLabel] = useState(false);
  const [labelDraft, setLabelDraft] = useState(data.label || '');
  const isCustom = data.category === 'custom';
  const isWorkspace = data.category === 'workspace';
  const isCodeTask = isCustom || isWorkspace;
  const isConfigured = data.configured;

  useEffect(() => {
    if (!editingLabel) {
      setLabelDraft(data.label || '');
    }
  }, [data.label, editingLabel]);

  const commitLabel = () => {
    const nextLabel = labelDraft.trim() || data.label;
    setEditingLabel(false);
    setLabelDraft(nextLabel);

    if (nextLabel !== data.label) {
      updateNode(id, { label: nextLabel });
    }
  };

  const cancelLabelEdit = () => {
    setLabelDraft(data.label || '');
    setEditingLabel(false);
  };

  const handleLabelKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter') {
      event.currentTarget.blur();
    }
    if (event.key === 'Escape') {
      cancelLabelEdit();
    }
  };
  
  const getBorderColor = () => {
    if (selected) return '#faad14';
    if (isCodeTask) {
      return isConfigured ? '#722ed1' : '#d9d9d9';
    }
    return isConfigured ? '#52c41a' : '#d9d9d9';
  };
  
  const getBackgroundGradient = () => {
    if (isCodeTask && isConfigured) {
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
          {isCodeTask ? (
            <CodeOutlined style={{ color: '#722ed1' }} />
          ) : (
            <ThunderboltOutlined style={{ color: '#1890ff' }} />
          )}
          {editingLabel ? (
            <Input
              autoFocus
              size="small"
              value={labelDraft}
              onChange={(event) => setLabelDraft(event.target.value)}
              onBlur={commitLabel}
              onKeyDown={handleLabelKeyDown}
              onClick={(event) => event.stopPropagation()}
              onMouseDown={(event) => event.stopPropagation()}
              style={{ width: '150px' }}
            />
          ) : (
            <strong
              title="Click to rename task"
              onClick={(event) => {
                event.stopPropagation();
                setEditingLabel(true);
              }}
              onMouseDown={(event) => event.stopPropagation()}
              style={{
                fontSize: '14px',
                cursor: 'text',
                maxWidth: '150px',
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
              }}
            >
              {data.label}
            </strong>
          )}
        </div>
        
        <div style={{ display: 'flex', gap: '4px', flexWrap: 'wrap' }}>
          {isCodeTask ? (
            <Tag color="purple" style={{ margin: 0 }}>{isWorkspace ? 'workspace' : 'custom'}</Tag>
          ) : (
            <Tag color="blue" style={{ margin: 0 }}>task</Tag>
          )}
          {!isConfigured && (
            <Tag color="warning" style={{ margin: 0 }}>unconfigured</Tag>
          )}
        </div>
        
        {isConfigured && (
          <div style={{ marginTop: '8px', fontSize: '12px', color: '#666' }}>
            <div>Inputs: {data.inputs?.length || 0}</div>
            <div>Outputs: {data.outputs?.length || 0}</div>
          </div>
        )}
      </div>
      
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
