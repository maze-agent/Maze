import { KeyboardEvent, useEffect, useState } from 'react';
import { Handle, Position } from 'reactflow';
import { Card, Input, Spin, Tag, Tooltip } from 'antd';
import { CheckCircleOutlined, CloseCircleOutlined, CodeOutlined, PartitionOutlined, ThunderboltOutlined } from '@ant-design/icons';
import { useWorkflowStore } from '@/stores/workflowStore';

export default function CustomNode({ id, data, selected }: any) {
  const { updateNode } = useWorkflowStore();
  const [editingLabel, setEditingLabel] = useState(false);
  const [labelDraft, setLabelDraft] = useState(data.label || '');
  const isCustom = data.category === 'custom';
  const isWorkspace = data.category === 'workspace';
  const isAgent = data.category === 'agent';
  const isCodeTask = isCustom || isWorkspace;
  const isConfigured = data.configured;
  const runState = data.runState;
  const runStatus = data.runStatus === 'interrupted' && runState?.status === 'running'
    ? 'interrupted'
    : runState?.status;
  const artifactCount = runState?.artifacts?.length || 0;
  const runtimeNodeIp = runState?.node_ip || runState?.nodeIp;
  const runtimeNodeId = runState?.node_id_runtime || runState?.node_id || runState?.nodeId;
  const runtimeGpuId = runState?.gpu_id ?? runState?.gpuId;

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
    if (runStatus === 'running') return '#1677ff';
    if (runStatus === 'completed') return '#52c41a';
    if (runStatus === 'failed') return '#ff4d4f';
    if (runStatus === 'interrupted') return '#fa8c16';
    if (isAgent) {
      return '#722ed1';
    }
    if (isCodeTask) {
      return isConfigured ? '#722ed1' : '#d9d9d9';
    }
    return isConfigured ? '#52c41a' : '#d9d9d9';
  };
  
  const getBackgroundGradient = () => {
    if (runStatus === 'running') {
      return 'linear-gradient(135deg, #e6f4ff 0%, #ffffff 100%)';
    }
    if (runStatus === 'completed') {
      return 'linear-gradient(135deg, #f6ffed 0%, #ffffff 100%)';
    }
    if (runStatus === 'failed') {
      return 'linear-gradient(135deg, #fff2f0 0%, #ffffff 100%)';
    }
    if (runStatus === 'interrupted') {
      return 'linear-gradient(135deg, #fff7e6 0%, #ffffff 100%)';
    }
    if (isAgent) {
      return 'linear-gradient(135deg, #f9f0ff 0%, #ffffff 100%)';
    }
    if (isCodeTask && isConfigured) {
      return 'linear-gradient(135deg, #f5f0ff 0%, #fafafa 100%)';
    }
    return 'white';
  };

  const getRunStatusTagColor = () => {
    if (runStatus === 'completed') return 'success';
    if (runStatus === 'failed') return 'error';
    if (runStatus === 'running') return 'processing';
    if (runStatus === 'interrupted') return 'orange';
    return 'default';
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
          {isAgent ? (
            <PartitionOutlined style={{ color: '#722ed1' }} />
          ) : isCodeTask ? (
            <CodeOutlined style={{ color: '#722ed1' }} />
          ) : (
            <ThunderboltOutlined style={{ color: '#1890ff' }} />
          )}
          {runStatus === 'running' && <Spin size="small" />}
          {runStatus === 'completed' && <CheckCircleOutlined style={{ color: '#52c41a' }} />}
          {runStatus === 'failed' && <CloseCircleOutlined style={{ color: '#ff4d4f' }} />}
          {runStatus === 'interrupted' && <CloseCircleOutlined style={{ color: '#fa8c16' }} />}
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
          {isAgent ? (
            <Tag color="purple" style={{ margin: 0 }}>agent</Tag>
          ) : isCodeTask ? (
            <Tag color="purple" style={{ margin: 0 }}>{isWorkspace ? 'workspace' : 'custom'}</Tag>
          ) : (
            <Tag color="blue" style={{ margin: 0 }}>task</Tag>
          )}
          {!isConfigured && (
            <Tag color="warning" style={{ margin: 0 }}>unconfigured</Tag>
          )}
          {runStatus && (
            <Tag
              color={getRunStatusTagColor()}
              style={{ margin: 0 }}
            >
              {runStatus}
            </Tag>
          )}
        </div>
        
        {isConfigured && (
          <div style={{ marginTop: '8px', fontSize: '12px', color: '#666' }}>
            <div>Inputs: {data.inputs?.length || 0}</div>
            <div>Outputs: {data.outputs?.length || 0}</div>
            {isAgent && (
              <div>{data.reactMode === 'online' ? 'Online LLM' : 'Local Demo'}</div>
            )}
            {isAgent && data.skills && data.skills.length > 0 && (
              <div>Skills: {data.skills.length}</div>
            )}
            {runtimeNodeIp && (
              <Tooltip title={`node_id: ${runtimeNodeId || '-'}${runtimeGpuId !== null && runtimeGpuId !== undefined ? `, gpu: ${runtimeGpuId}` : ''}`}>
                <div style={{ color: '#0958d9', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  Host: {runtimeNodeIp}{runtimeGpuId !== null && runtimeGpuId !== undefined ? ` / GPU ${runtimeGpuId}` : ''}
                </div>
              </Tooltip>
            )}
            {runState?.result_summary !== undefined && runStatus === 'completed' && (
              <Tooltip title={typeof runState.result_summary === 'string' ? runState.result_summary : JSON.stringify(runState.result_summary)}>
                <div style={{ color: '#389e0d', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  Result ready
                </div>
              </Tooltip>
            )}
            {artifactCount > 0 && (
              <div style={{ color: '#0958d9', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                Files: {artifactCount}
              </div>
            )}
            {runState?.error && (
              <Tooltip title={String(runState.error)}>
                <div style={{ color: '#cf1322', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  Error
                </div>
              </Tooltip>
            )}
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
