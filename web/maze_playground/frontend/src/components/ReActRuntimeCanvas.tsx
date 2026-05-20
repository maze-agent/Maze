import { useMemo, useState } from 'react';
import { Button, Divider, Empty, Space, Tag, Typography } from 'antd';
import ReactFlow, {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  type Edge,
  type Node,
  type NodeProps,
} from 'reactflow';
import type { DynamicRunEvent, DynamicRunSnapshot } from '@/types/workflow';

const { Text, Title } = Typography;

export interface AgentTraceStep {
  step: number;
  decision?: any;
  action?: any;
  tool?: string;
  args?: any;
  decisionTaskId?: string;
  toolTaskId?: string;
  observation?: any;
  repair?: boolean;
}

export interface AgentTrace {
  started: Record<string, any> | null;
  final: Record<string, any> | null;
  error: Record<string, any> | null;
  steps: AgentTraceStep[];
}

interface TraceNodeData {
  title: string;
  subtitle?: string;
  tags?: string[];
  tone: 'start' | 'llm' | 'tool' | 'repair' | 'observation' | 'final' | 'error';
  kind: 'start' | 'llm' | 'tool' | 'repair' | 'observation' | 'error';
  step?: number;
  taskId?: string;
  taskSpecId?: string;
  toolName?: string;
  prompt?: string;
  decision?: any;
  action?: any;
  args?: any;
  observation?: any;
}

const traceNodeStyles: Record<TraceNodeData['tone'], { border: string; background: string; accent: string }> = {
  start: { border: '#d3adf7', background: '#f9f0ff', accent: '#722ed1' },
  llm: { border: '#91caff', background: '#f0f7ff', accent: '#1677ff' },
  tool: { border: '#95de64', background: '#f6ffed', accent: '#52c41a' },
  repair: { border: '#ffd591', background: '#fff7e6', accent: '#fa8c16' },
  observation: { border: '#87e8de', background: '#e6fffb', accent: '#13c2c2' },
  final: { border: '#b7eb8f', background: '#f6ffed', accent: '#389e0d' },
  error: { border: '#ffa39e', background: '#fff1f0', accent: '#ff4d4f' },
};

function shortId(value?: string) {
  if (!value) return '';
  return value.length > 12 ? `${value.slice(0, 8)}...` : value;
}

export function formatJson(value: any) {
  if (value === undefined || value === null || value === '') return '-';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function ReActTraceNode({ data }: NodeProps<TraceNodeData>) {
  const style = traceNodeStyles[data.tone];

  return (
    <div
      style={{
        minWidth: 180,
        maxWidth: 220,
        border: `1px solid ${style.border}`,
        borderRadius: 8,
        background: style.background,
        boxShadow: '0 4px 12px rgba(15, 23, 42, 0.08)',
        overflow: 'hidden',
      }}
    >
      <Handle type="target" position={Position.Left} style={{ background: style.accent }} />
      <div style={{ height: 5, background: style.accent }} />
      <div style={{ padding: '10px 12px' }}>
        <Text strong style={{ display: 'block', fontSize: 13 }}>
          {data.title}
        </Text>
        {data.subtitle && (
          <Text
            type="secondary"
            style={{
              display: '-webkit-box',
              WebkitLineClamp: 2,
              WebkitBoxOrient: 'vertical',
              overflow: 'hidden',
              fontSize: 12,
              marginTop: 2,
              wordBreak: 'break-word',
            }}
          >
            {data.subtitle}
          </Text>
        )}
        {data.tags && data.tags.length > 0 && (
          <Space size={4} wrap style={{ marginTop: 8 }}>
            {data.tags.map((tag) => (
              <Tag key={tag} style={{ marginInlineEnd: 0 }}>
                {tag}
              </Tag>
            ))}
          </Space>
        )}
      </div>
      <Handle type="source" position={Position.Right} style={{ background: style.accent }} />
    </div>
  );
}

const traceNodeTypes = {
  trace: ReActTraceNode,
};

function actionSummary(step: AgentTraceStep) {
  if (step.action?.final !== undefined) return 'Final answer';
  if (step.action?.tool) return String(step.action.tool);
  if (step.tool) return step.tool;
  return 'Decision';
}

function observationSummary(step: AgentTraceStep) {
  const result = step.observation?.result ?? step.observation;
  if (result?.result !== undefined) return `result: ${String(result.result)}`;
  if (step.observation?.error_type) return String(step.observation.error_type);
  if (result === undefined || result === null) return 'observation';
  if (typeof result === 'object') return 'structured result';
  return String(result);
}

function edge(source: string, target: string, label?: string): Edge {
  return {
    id: `${source}-${target}`,
    source,
    target,
    label,
    type: 'smoothstep',
    markerEnd: { type: MarkerType.ArrowClosed },
    style: { stroke: '#8c8c8c', strokeWidth: 2 },
    labelStyle: { fill: '#595959', fontSize: 11 },
    labelBgStyle: { fill: '#fff', fillOpacity: 0.9 },
  };
}

export function buildReActCanvas(trace: AgentTrace): { nodes: Node<TraceNodeData>[]; edges: Edge[] } {
  const nodes: Node<TraceNodeData>[] = [];
  const edges: Edge[] = [];
  let previousExitNodeId: string | null = null;

  if (trace.started) {
    const tools = Array.isArray(trace.started.tools) ? trace.started.tools : [];
    nodes.push({
      id: 'agent-start',
      type: 'trace',
      position: { x: -280, y: 40 },
      data: {
        title: 'Agent Start',
        subtitle: trace.started.prompt ? String(trace.started.prompt) : undefined,
        tags: [
          trace.started.mode ? String(trace.started.mode) : 'agent',
          `${tools.length} tool(s)`,
        ],
        tone: 'start',
        kind: 'start',
        prompt: trace.started.prompt,
      },
    });
    previousExitNodeId = 'agent-start';
  }

  trace.steps.forEach((step, index) => {
    const x = index * 280;
    const llmNodeId = `step-${step.step}-llm`;
    nodes.push({
      id: llmNodeId,
      type: 'trace',
      position: { x, y: 40 },
      data: {
        title: `Step ${step.step} LLM Decision`,
        subtitle: step.decisionTaskId ? shortId(step.decisionTaskId) : actionSummary(step),
        tags: ['LLM', actionSummary(step)],
        tone: 'llm',
        kind: 'llm',
        step: step.step,
        taskId: step.decisionTaskId,
        taskSpecId: 'react_llm_decision',
        decision: step.decision,
        action: step.action,
      },
    });

    if (previousExitNodeId) {
      edges.push(edge(previousExitNodeId, llmNodeId, 'history'));
    }

    if (step.repair) {
      const repairNodeId = `step-${step.step}-repair`;
      nodes.push({
        id: repairNodeId,
        type: 'trace',
        position: { x, y: 200 },
        data: {
          title: 'Repair Observation',
          subtitle: observationSummary(step),
          tags: step.tool ? ['repair', step.tool] : ['repair'],
          tone: 'repair',
          kind: 'repair',
          step: step.step,
          taskId: step.decisionTaskId,
          taskSpecId: 'react_llm_decision',
          toolName: step.tool,
          decision: step.decision,
          action: step.action,
          observation: step.observation,
        },
      });
      edges.push(edge(llmNodeId, repairNodeId, 'invalid action'));
      previousExitNodeId = repairNodeId;
      return;
    }

    if (step.action?.final !== undefined) {
      previousExitNodeId = llmNodeId;
      return;
    }

    if (step.tool || step.action?.tool) {
      const toolName = step.tool || step.action?.tool || 'tool';
      const toolNodeId = `step-${step.step}-tool`;
      nodes.push({
        id: toolNodeId,
        type: 'trace',
        position: { x, y: 200 },
        data: {
          title: `Tool: ${toolName}`,
          subtitle: step.toolTaskId ? shortId(step.toolTaskId) : 'pending task id',
          tags: ['tool'],
          tone: 'tool',
          kind: 'tool',
          step: step.step,
          taskId: step.toolTaskId,
          taskSpecId: toolName,
          toolName,
          args: step.args || step.action?.args,
          observation: step.observation,
        },
      });
      edges.push(edge(llmNodeId, toolNodeId, 'call'));

      if (step.observation !== undefined) {
        const observationNodeId = `step-${step.step}-observation`;
        nodes.push({
          id: observationNodeId,
          type: 'trace',
          position: { x, y: 360 },
          data: {
            title: 'Observation',
            subtitle: observationSummary(step),
            tags: ['result'],
            tone: 'observation',
            kind: 'observation',
            step: step.step,
            taskId: step.toolTaskId,
            taskSpecId: toolName,
            toolName,
            observation: step.observation,
          },
        });
        edges.push(edge(toolNodeId, observationNodeId, 'result'));
        previousExitNodeId = observationNodeId;
      } else {
        previousExitNodeId = toolNodeId;
      }
    } else {
      previousExitNodeId = llmNodeId;
    }
  });

  if (trace.error && previousExitNodeId) {
    const errorNodeId = 'agent-error';
    nodes.push({
      id: errorNodeId,
      type: 'trace',
      position: { x: trace.steps.length * 280, y: 200 },
      data: {
        title: 'Agent Error',
        subtitle: String(trace.error.error || 'Unknown error'),
        tags: ['error'],
        tone: 'error',
        kind: 'error',
        observation: trace.error,
      },
    });
    edges.push(edge(previousExitNodeId, errorNodeId, 'error'));
  }

  return { nodes, edges };
}

export function buildAgentTrace(events: DynamicRunEvent[]): AgentTrace {
  const stepMap = new Map<number, AgentTraceStep>();
  let started: Record<string, any> | null = null;
  let final: Record<string, any> | null = null;
  let error: Record<string, any> | null = null;

  const getStep = (stepValue: any) => {
    const step = Number(stepValue || 0);
    if (!Number.isFinite(step) || step <= 0) return null;
    if (!stepMap.has(step)) {
      stepMap.set(step, { step });
    }
    return stepMap.get(step)!;
  };

  events.forEach((event) => {
    const data = event.data || {};
    if (event.type === 'agent_run_started') {
      started = data;
      return;
    }
    if (event.type === 'agent_final') {
      final = data;
      return;
    }
    if (event.type === 'agent_error') {
      error = data;
      return;
    }

    const step = getStep(data.step);
    if (!step) return;

    if (event.type === 'react_llm_decision') {
      step.decision = data.decision;
      step.action = data.action;
      step.decisionTaskId = data.task_id;
    } else if (event.type === 'agent_action') {
      step.tool = data.tool;
      step.args = data.args;
      step.action = step.action || { tool: data.tool, args: data.args };
      step.decisionTaskId = step.decisionTaskId || data.decision_task_id;
    } else if (event.type === 'agent_observation') {
      step.tool = step.tool || data.tool;
      step.toolTaskId = data.task_id;
      step.observation = data.result;
    } else if (event.type === 'agent_repair_observation') {
      step.tool = step.tool || data.tool;
      step.decisionTaskId = step.decisionTaskId || data.decision_task_id;
      step.observation = data.result;
      step.action = step.action || data.result?.action;
      step.repair = true;
    }
  });

  return {
    started,
    final,
    error,
    steps: Array.from(stepMap.values()).sort((a, b) => a.step - b.step),
  };
}

export function hasAgentTrace(trace: AgentTrace) {
  return Boolean(trace.started || trace.final || trace.error || trace.steps.length);
}

interface ReActRuntimeCanvasProps {
  trace: AgentTrace;
  run?: DynamicRunSnapshot | null;
  height?: number | string;
  emptyDescription?: string;
}

function findTaskNode(run: DynamicRunSnapshot | null | undefined, taskId?: string) {
  if (!run?.task_nodes || !taskId) return null;
  return Object.values(run.task_nodes).find((node: any) => node?.task_id === taskId) || null;
}

function findTaskSpec(run: DynamicRunSnapshot | null | undefined, taskSpecId?: string, taskNode?: any) {
  if (!run?.task_specs) return null;
  const resolvedSpecId = taskSpecId || taskNode?.task_spec_id;
  if (!resolvedSpecId) return null;
  return run.task_specs[resolvedSpecId] || null;
}

function DetailBlock({ title, value }: { title: string; value: any }) {
  if (value === undefined || value === null || value === '') {
    return null;
  }

  return (
    <div>
      <Text type="secondary" style={{ display: 'block', fontSize: 12, marginBottom: 4 }}>
        {title}
      </Text>
      <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 12 }}>
        {typeof value === 'string' ? value : formatJson(value)}
      </pre>
    </div>
  );
}

function RuntimeNodeDetails({
  node,
  run,
  onClose,
}: {
  node: Node<TraceNodeData>;
  run?: DynamicRunSnapshot | null;
  onClose: () => void;
}) {
  const data = node.data;
  const taskNode = findTaskNode(run, data.taskId);
  const taskSpec = findTaskSpec(run, data.taskSpecId, taskNode);
  const taskInputs = Array.isArray(taskNode?.inputs)
    ? taskNode.inputs.map((input: any) => ({
        name: input.name,
        dataType: input.data_type,
        value: input.value,
      }))
    : [];

  return (
    <div
      style={{
        position: 'absolute',
        top: 12,
        right: 12,
        bottom: 12,
        width: 380,
        maxWidth: '42%',
        zIndex: 6,
        background: '#fff',
        border: '1px solid #d9d9d9',
        borderRadius: 8,
        boxShadow: '0 10px 28px rgba(15, 23, 42, 0.14)',
        overflow: 'auto',
        padding: 14,
      }}
    >
      <Space style={{ justifyContent: 'space-between', width: '100%' }} align="start">
        <div style={{ minWidth: 0 }}>
          <Title level={5} style={{ margin: 0 }}>{data.title}</Title>
          <Space size={4} wrap style={{ marginTop: 6 }}>
            {data.tags?.map((tag) => (
              <Tag key={tag} style={{ marginInlineEnd: 0 }}>{tag}</Tag>
            ))}
            {taskNode?.status && <Tag color="blue">{taskNode.status}</Tag>}
          </Space>
        </div>
        <Button size="small" onClick={onClose}>Close</Button>
      </Space>

      <Divider style={{ margin: '12px 0' }} />

      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        {data.taskId && (
          <div>
            <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>Task ID</Text>
            <Text copyable style={{ fontSize: 12 }}>{data.taskId}</Text>
          </div>
        )}

        {taskSpec && (
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <div>
              <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>Task Spec</Text>
              <Space size={4} wrap>
                <Tag>{taskSpec.task_spec_id}</Tag>
                <Tag>{taskSpec.task_name}</Tag>
              </Space>
            </div>
            <DetailBlock title="Implementation" value={taskSpec.code_preview || taskSpec.code_str} />
          </Space>
        )}

        {taskInputs.length > 0 && <DetailBlock title="Task Inputs" value={taskInputs} />}
        <DetailBlock title="Prompt" value={data.prompt} />
        <DetailBlock title="Decision" value={data.decision} />
        <DetailBlock title="Action" value={data.action || (data.toolName ? { tool: data.toolName, args: data.args || {} } : undefined)} />
        <DetailBlock title="Observation" value={data.observation} />
      </Space>
    </div>
  );
}

export default function ReActRuntimeCanvas({
  trace,
  run,
  height = 420,
  emptyDescription = 'No ReAct graph events recorded',
}: ReActRuntimeCanvasProps) {
  const graph = useMemo(() => buildReActCanvas(trace), [trace]);
  const [selectedNode, setSelectedNode] = useState<Node<TraceNodeData> | null>(null);

  if (graph.nodes.length === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={emptyDescription} />;
  }

  return (
    <div
      style={{
        height,
        minHeight: typeof height === 'number' ? undefined : 260,
        border: '1px solid #f0f0f0',
        borderRadius: 8,
        overflow: 'hidden',
        background: '#fbfbfb',
        position: 'relative',
      }}
    >
      <ReactFlow
        key={`${graph.nodes.length}-${graph.edges.length}`}
        nodes={graph.nodes}
        edges={graph.edges}
        nodeTypes={traceNodeTypes}
        fitView
        fitViewOptions={{ padding: 0.18 }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        panOnScroll
        zoomOnScroll={false}
        onNodeClick={(_, node) => setSelectedNode(node)}
      >
        <Background color="#e8e8e8" gap={18} />
        <MiniMap pannable zoomable />
        <Controls showInteractive={false} />
      </ReactFlow>
      {selectedNode && (
        <RuntimeNodeDetails
          node={selectedNode}
          run={run}
          onClose={() => setSelectedNode(null)}
        />
      )}
    </div>
  );
}
