import { memo, useEffect, useMemo, useRef, useState } from 'react';
import ReactFlow, { Background, Controls, Handle, MiniMap, NodeProps, Position } from 'reactflow';
import 'reactflow/dist/style.css';
import { Alert, Button, Descriptions, Drawer, Empty, List, Space, Tabs, Tag, Typography } from 'antd';
import { api } from '@/api/client';
import { acceptedTaskResult, awaitingMoreSteps, projectRunGraph, RuntimeGraphSnapshot, taskReport } from '@/utils/runGraph';
import { resolveResourceUsage, resourceUsageRows } from '@/utils/resourceUsage';

const { Text } = Typography;
const colors: Record<string, string> = {
  running: '#1677ff', retrying: '#d48806', completed: '#389e0d', succeeded: '#389e0d',
  failed: '#cf1322', cancelled: '#d46b08', canceled: '#d46b08', pending: '#8c8c8c', submitted: '#8c8c8c',
};

const RuntimeTask = memo(({ data, selected }: NodeProps) => {
  const status = data.task.fault_tolerance?.status === 'retrying' ? 'retrying' : data.task.status;
  const kind = data.task.task_kind || (data.task.resources?.gpu_mem ? 'gpu' : data.task.resources?.io_num ? 'io' : 'cpu');
  return (
    <div data-runtime-task={data.task.task_id} style={{
      width: 232, padding: '12px 14px', background: '#fff', borderRadius: 8,
      border: `${selected ? 2 : 1}px solid ${selected ? '#1677ff' : colors[status] || '#d9d9d9'}`,
      boxShadow: selected ? '0 0 0 3px #e6f4ff' : '0 2px 5px #0000000a',
    }}>
      <Handle type="target" position={Position.Left} />
      <div title={data.label} style={{ fontWeight: 600, marginBottom: 8, overflowWrap: 'anywhere',
        display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>{data.label}</div>
      <Space size={4} wrap>
        <Tag color={kind === 'gpu' ? 'purple' : kind === 'io' ? 'cyan' : 'blue'} style={{ margin: 0 }}>{String(kind).toUpperCase()}</Tag>
        <Tag color={colors[status]} style={{ margin: 0 }}>{status}</Tag>
        {data.report?.verdict && <Tag color={data.report.verdict === 'passed' ? 'green' : 'orange'} style={{ margin: 0 }}>
          Outcome: {String(data.report.verdict).replace(/_/g, ' ')}
        </Tag>}
      </Space>
      {data.task.selected_node?.node_ip && <div style={{ color: '#8c8c8c', fontSize: 11, marginTop: 6 }}>{data.task.selected_node.node_ip}</div>}
      <Handle type="source" position={Position.Right} />
    </div>
  );
});
const nodeTypes = { runtimeTask: RuntimeTask };

function pretty(value: any) {
  return JSON.stringify(value ?? null, null, 2)?.slice(0, 60000);
}

export function RunTaskDetails({ run, events = [], taskId, onBack, refreshVersion = 0 }: {
  run: RuntimeGraphSnapshot; events?: any[]; taskId: string; onBack?: () => void; refreshVersion?: number;
}) {
  const [logs, setLogs] = useState<any[]>([]);
  const [artifacts, setArtifacts] = useState<any[]>([]);
  const [detailError, setDetailError] = useState('');
  const selected = taskId ? run.task_nodes?.[taskId] : null;
  const result = acceptedTaskResult(selected, events);
  const report = taskReport(result);

  useEffect(() => {
    if (!taskId) return;
    let disposed = false;
    setDetailError('');
    setLogs([]);
    setArtifacts([]);
    void Promise.allSettled([
      api.getRunLogs(run.run_id, { taskId: taskId, tail: 200 }),
      api.getRunTaskArtifacts(run.run_id, taskId),
    ]).then(([logResult, artifactResult]) => {
      if (disposed) return;
      if (logResult.status === 'fulfilled') setLogs(logResult.value.lines || []);
      if (artifactResult.status === 'fulfilled') setArtifacts(artifactResult.value.artifacts || []);
      if (logResult.status === 'rejected' || artifactResult.status === 'rejected') setDetailError('Some task details could not be refreshed.');
    });
    return () => { disposed = true; };
  }, [run.run_id, taskId, selected?.status, events.length, refreshVersion]);

  const measured = report?.resource_usage;
  const usage = measured?.cpu && measured?.memory && measured?.gpu && measured?.disk
    ? measured : resolveResourceUsage(selected);
  const preStyle = { fontSize: 12, whiteSpace: 'pre-wrap' as const, overflowWrap: 'anywhere' as const, maxHeight: 440, overflow: 'auto' };

  return <div className={onBack ? 'run-task-details' : undefined} data-testid="run-task-details">
    {onBack && <>
      <Button type="link" onClick={onBack} style={{ paddingLeft: 0 }}>Run overview</Button>
      <Typography.Title level={5} style={{ marginTop: 8 }}>{selected?.task_name || 'Task execution'}</Typography.Title>
      <Space wrap size={4} style={{ marginBottom: 16 }}><Tag>{String(selected?.task_kind || 'cpu').toUpperCase()}</Tag><Tag>{selected?.status || 'pending'}</Tag></Space>
    </>}
        {selected && <>
          {detailError && <Alert type="warning" message={detailError} style={{ marginBottom: 12 }} />}
          <Descriptions size="small" bordered column={1} style={{ marginBottom: 16 }}>
            <Descriptions.Item label="Task"><Text copyable>{selected.task_id}</Text></Descriptions.Item>
            <Descriptions.Item label="Execution">{selected.status}</Descriptions.Item>
            {report?.verdict && <Descriptions.Item label="Outcome"><Tag color={report.verdict === 'passed' ? 'green' : 'orange'}>{String(report.verdict).replace(/_/g, ' ')}</Tag></Descriptions.Item>}
            <Descriptions.Item label="Worker">{selected.selected_node?.node_ip || selected.selected_node?.node_id || 'Not placed'}</Descriptions.Item>
            <Descriptions.Item label="Attempts">{selected.attempt || selected.fault_tolerance?.attempts?.length || '-'}</Descriptions.Item>
          </Descriptions>
          {selected.error && <Alert type="error" message="Task execution failed" description={<pre style={preStyle}>{pretty(selected.error)}</pre>} />}
          <Tabs items={[
            { key: 'result', label: 'Result', children: <pre style={preStyle}>{pretty(result)}</pre> },
            { key: 'resources', label: 'Resources', children: <>
              {measured && <Text type="secondary">Execution-reported usage{report.resource_scope === 'command_process_tree' ? ': command and child processes' : ''}</Text>}
              <Descriptions size="small" column={1} items={resourceUsageRows(usage).map(([label, value]) => ({ key: label, label, children: value }))} />
            </> },
            { key: 'inputs', label: 'Inputs', children: <pre style={preStyle}>{pretty(selected.inputs)}</pre> },
            { key: 'logs', label: 'Logs', children: <pre style={preStyle}>{[
              report?.stdout_tail, report?.stderr_tail,
              ...logs.map((line) => typeof line === 'string' ? line : line.message || line.line || pretty(line)),
            ].filter(Boolean).join('\n') || 'No task logs available yet.'}</pre> },
            { key: 'artifacts', label: `Artifacts (${artifacts.length})`, children: <List dataSource={artifacts} renderItem={(artifact: any) => <List.Item>
              {artifact.sha256 ? <a href={api.getArtifactDownloadUrl(artifact.sha256)} target="_blank" rel="noreferrer">{artifact.path || artifact.name || artifact.sha256}</a> : <Text>{artifact.path || artifact.name}</Text>}
            </List.Item>} /> },
          ]} />
        </>}
  </div>;
}

export default function RunGraph({ run, events = [], fullHeight = false, selectedTaskId, onSelectTask }: {
  run: RuntimeGraphSnapshot; events?: any[]; fullHeight?: boolean;
  selectedTaskId?: string | null; onSelectTask?: (taskId: string | null) => void;
}) {
  const positions = useRef<{ id: string; values: Map<string, { x: number; y: number }> }>({ id: run.run_id, values: new Map() });
  if (positions.current.id !== run.run_id) positions.current = { id: run.run_id, values: new Map() };
  const graph = useMemo(() => {
    const value = projectRunGraph(run, positions.current.values, events);
    positions.current.values = new Map(value.nodes.map((node) => [node.id, node.position]));
    return value;
  }, [run, events]);
  const [internalSelectedId, setInternalSelectedId] = useState<string | null>(null);
  const selectedId = selectedTaskId === undefined ? internalSelectedId : selectedTaskId;
  const select = (taskId: string | null) => { setInternalSelectedId(taskId); onSelectTask?.(taskId); };
  useEffect(() => { setInternalSelectedId(null); }, [run.run_id]);
  const selected = selectedId ? run.task_nodes?.[selectedId] : null;

  return (
    <div data-testid="run-graph" className={fullHeight ? 'run-graph-full' : undefined}
      style={fullHeight ? undefined : { border: '1px solid #e8e8e8', borderRadius: 8, overflow: 'hidden' }}>
      {awaitingMoreSteps(run) && <Alert type="info" showIcon message="Waiting for further steps" description="Current tasks have finished. The workflow remains open until explicitly finalized." style={{ borderRadius: 0 }} />}
      <div style={fullHeight ? { flex: 1, minHeight: 0, background: '#fafcff' } : { height: 370, background: '#fafcff' }}>
        {graph.nodes.length ? <ReactFlow
          key={run.run_id} nodes={graph.nodes.map((node) => ({ ...node, selected: node.id === selectedId }))} edges={graph.edges} nodeTypes={nodeTypes}
          onNodeClick={(_, node) => select(node.id)} onPaneClick={() => select(null)}
          nodesDraggable={false} nodesConnectable={false} edgesUpdatable={false} deleteKeyCode={null}
          fitView fitViewOptions={{ padding: 0.18, maxZoom: fullHeight ? 1 : 0.85 }} minZoom={0.15} maxZoom={2}
          attributionPosition="bottom-left"
        >
          <Background /><Controls showInteractive={false} /><MiniMap style={{ width: 140, height: 90 }} nodeColor={(node) => colors[node.data.task.status] || '#bfbfbf'} />
        </ReactFlow> : <Empty description="No tasks appended yet" style={{ paddingTop: 100 }} />}
      </div>
      {!fullHeight && <Drawer title={selected?.task_name || 'Task execution'} width={640} open={Boolean(selectedId)} onClose={() => select(null)}>
        {selectedId && <RunTaskDetails key={`${run.run_id}:${selectedId}`} run={run} events={events} taskId={selectedId} />}
      </Drawer>}
    </div>
  );
}
