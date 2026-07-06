import { KeyboardEvent, useEffect, useState } from 'react';
import { Handle, Position } from 'reactflow';
import { Card, Input, Tag, Tooltip } from 'antd';
import {
  CheckCircleFilled,
  ClockCircleOutlined,
  CloseCircleFilled,
  CodeOutlined,
  CloudServerOutlined,
  DatabaseOutlined,
  FileDoneOutlined,
  LoadingOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import { useWorkflowStore } from '@/stores/workflowStore';

type TaskKind = 'cpu' | 'gpu' | 'io';

const taskKindStyles: Record<TaskKind, {
  label: string;
  color: string;
  border: string;
  background: string;
  iconColor: string;
}> = {
  cpu: {
    label: 'CPU',
    color: '#0958d9',
    border: '#1677ff',
    background: '#f0f7ff',
    iconColor: '#1677ff',
  },
  gpu: {
    label: 'GPU',
    color: '#6d28d9',
    border: '#7c3aed',
    background: '#f5f3ff',
    iconColor: '#7c3aed',
  },
  io: {
    label: 'I/O',
    color: '#047857',
    border: '#10b981',
    background: '#f0fdf4',
    iconColor: '#059669',
  },
};

function taskKind(data: any): TaskKind {
  const explicitKind = data.task_kind || data.taskKind;
  if (['cpu', 'gpu', 'io'].includes(explicitKind)) return explicitKind;
  const resources = data.resources || {};
  const label = [
    data.label,
    data.taskRef,
    data.taskPath,
    data.functionName,
  ].filter(Boolean).join(' ').toLowerCase();

  if (
    Number(resources.gpu_mem || 0) > 0
    || data.modelAnchor
    || data.model_anchor
    || data.localModel
    || label.includes('gpu')
    || label.includes('cuda')
    || label.includes('llm')
    || label.includes('model')
    || label.includes('inference')
    || label.includes('embed')
  ) {
    return 'gpu';
  }
  if (label.includes('file') || label.includes('io') || label.includes('input') || label.includes('artifact') || label.includes('sandbox')) {
    return 'io';
  }
  return 'cpu';
}

function taskKindIcon(kind: TaskKind) {
  if (kind === 'gpu') return <ThunderboltOutlined />;
  if (kind === 'io') return <DatabaseOutlined />;
  return <CodeOutlined />;
}

function normalizedStatus(runStatus?: string, isConfigured = true) {
  const status = String(runStatus || '').toLowerCase();
  if (!isConfigured) return 'unconfigured';
  if (status === 'completed' || status === 'succeeded' || status === 'success') return 'succeeded';
  if (status === 'running') return 'running';
  if (status === 'queued' || status === 'pending' || status === 'created') return 'queued';
  if (status === 'failed' || status === 'error') return 'failed';
  if (status === 'interrupted' || status === 'timed_out' || status === 'cancelled' || status === 'canceled') return 'failed';
  return '';
}

function statusLabel(status: string) {
  if (status === 'running') return 'Running';
  if (status === 'queued') return 'Queued';
  if (status === 'failed') return 'Failed';
  if (status === 'unconfigured') return 'Unconfigured';
  return '';
}

function statusColor(status: string) {
  if (status === 'running') return 'processing';
  if (status === 'queued') return 'warning';
  if (status === 'failed') return 'error';
  if (status === 'unconfigured') return 'default';
  return 'default';
}

function placementText(runState: any, runtimeNodeIp?: string, runtimeNodeId?: string, runtimeGpuId?: string | number | null) {
  const resources = runState?.resources || runState?.schedule_decision?.requested_resources;
  const resourceText = resources
    ? Object.entries(resources)
      .filter(([, value]) => value !== undefined && value !== null && value !== 0)
      .map(([key, value]) => `${key}: ${value}`)
      .join(', ')
    : '';
  return [
    runtimeNodeIp ? `IP: ${runtimeNodeIp}` : null,
    runtimeNodeId ? `Node: ${runtimeNodeId}` : null,
    runtimeGpuId !== null && runtimeGpuId !== undefined ? `GPU: ${runtimeGpuId}` : null,
    resourceText ? `Resources: ${resourceText}` : null,
  ].filter(Boolean).join('\n') || 'No placement yet';
}

function hasVisibleResult(result: any) {
  if (result === undefined || result === null) return false;
  if (typeof result === 'string' && result.trim().toLowerCase() === 'null') return false;
  return true;
}

function outputText(runState: any, artifactCount: number) {
  const result = runState?.result_summary;
  const artifacts = runState?.artifacts || [];
  const parts = [];
  if (hasVisibleResult(result)) {
    parts.push(typeof result === 'string' ? result : JSON.stringify(result, null, 2));
  }
  if (artifactCount > 0) {
    parts.push(`Artifacts: ${artifactCount}`);
    artifacts.slice(0, 3).forEach((artifact: any) => {
      parts.push(`- ${artifact.name || artifact.path || artifact.uri || artifact.id || 'artifact'}`);
    });
  }
  return parts.join('\n') || 'No outputs yet';
}

export default function CustomNode({ id, data, selected }: any) {
  const { updateNode } = useWorkflowStore();
  const [editingLabel, setEditingLabel] = useState(false);
  const [labelDraft, setLabelDraft] = useState(data.label || '');
  const isConfigured = data.configured;
  const kind = taskKind(data);
  const kindStyle = taskKindStyles[kind];
  const runState = data.runState;
  const runStatus = data.runStatus === 'interrupted' && runState?.status === 'running'
    ? 'interrupted'
    : runState?.status;
  const status = normalizedStatus(runStatus, isConfigured);
  const artifactCount = runState?.artifacts?.length || 0;
  const runtimeNodeIp = runState?.node_ip || runState?.nodeIp;
  const runtimeNodeId = runState?.node_id_runtime || runState?.node_id || runState?.nodeId;
  const runtimeGpuId = runState?.gpu_id ?? runState?.gpuId;
  const hasPlacement = Boolean(runtimeNodeIp || runtimeNodeId);
  const hasOutput = hasVisibleResult(runState?.result_summary) || artifactCount > 0;

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
  
  const statusIcon = () => {
    if (status === 'succeeded') {
      return <CheckCircleFilled style={{ color: '#22c55e', fontSize: 16 }} />;
    }
    if (status === 'running') {
      return <LoadingOutlined spin style={{ color: '#1677ff', fontSize: 16 }} />;
    }
    if (status === 'queued') {
      return <ClockCircleOutlined style={{ color: '#d97706', fontSize: 16 }} />;
    }
    if (status === 'failed') {
      return <CloseCircleFilled style={{ color: '#ef4444', fontSize: 16 }} />;
    }
    return null;
  };

  const statusTooltip = status === 'succeeded' ? 'Succeeded' : statusLabel(status);
  
  return (
    <Card
      size="small"
      style={{
        width: '244px',
        borderWidth: '2px',
        borderColor: kindStyle.border,
        boxShadow: selected ? '0 0 0 2px rgba(245, 158, 11, 0.22)' : undefined,
        background: `linear-gradient(135deg, ${kindStyle.background} 0%, #ffffff 72%)`,
        borderRadius: 8,
      }}
      bodyStyle={{ padding: 14 }}
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
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, marginBottom: 10 }}>
          <span style={{ color: kindStyle.iconColor, lineHeight: '20px', paddingTop: 1 }}>
            {taskKindIcon(kind)}
          </span>
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
              style={{ width: 150 }}
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
                flex: 1,
                fontSize: 15,
                lineHeight: '20px',
                cursor: 'text',
                maxWidth: 165,
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
              }}
            >
              {data.label}
            </strong>
          )}
          <Tooltip title={statusTooltip}>
            <span style={{ width: 18, height: 20, display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }}>
              {statusIcon()}
            </span>
          </Tooltip>
        </div>
        
        <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap', marginBottom: 10 }}>
          <Tag
            style={{
              margin: 0,
              color: kindStyle.color,
              borderColor: kindStyle.border,
              background: '#fff',
              fontWeight: 600,
            }}
          >
            {kindStyle.label}
          </Tag>
          {!isConfigured && (
            <Tag color="warning" style={{ margin: 0 }}>Unconfigured</Tag>
          )}
          {status && status !== 'succeeded' && status !== 'unconfigured' && (
            <Tag
              color={statusColor(status)}
              style={{ margin: 0 }}
            >
              {statusLabel(status)}
            </Tag>
          )}
        </div>
        
        {isConfigured && (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, fontSize: 12, color: '#64748b' }}>
            <span style={{ whiteSpace: 'nowrap' }}>
              Inputs {data.inputs?.length || 0} · Outputs {data.outputs?.length || 0}
            </span>
            <span style={{ display: 'inline-flex', gap: 8, alignItems: 'center' }}>
              {hasPlacement && (
                <Tooltip
                  placement="top"
                  title={<pre style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{placementText(runState, runtimeNodeIp, runtimeNodeId, runtimeGpuId)}</pre>}
                >
                  <CloudServerOutlined style={{ color: '#2563eb', fontSize: 15 }} />
                </Tooltip>
              )}
              {hasOutput && (
                <Tooltip
                  placement="top"
                  title={<pre style={{ margin: 0, whiteSpace: 'pre-wrap', maxWidth: 260 }}>{outputText(runState, artifactCount)}</pre>}
                >
                  <FileDoneOutlined style={{ color: '#16a34a', fontSize: 15 }} />
                </Tooltip>
              )}
              {runState?.error && (
                <Tooltip title={String(runState.error)}>
                  <CloseCircleFilled style={{ color: '#ef4444', fontSize: 15 }} />
                </Tooltip>
              )}
            </span>
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
