import React, { useCallback, useRef, useEffect } from 'react';
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  Connection,
  Edge,
  addEdge,
  useNodesState,
  useEdgesState,
  ReactFlowInstance,
  Node as ReactFlowNode,
} from 'reactflow';
import { Button, message, Space, Spin, Tag, Typography } from 'antd';
import { useWorkflowStore } from '@/stores/workflowStore';
import { api } from '@/api/client';
import type {
  BuiltinTaskMeta,
  DynamicRunEvent,
  DynamicRunSnapshot,
  DynamicRunStatus,
  WorkspaceTaskMeta,
  WorkflowEdge,
  WorkflowNode,
} from '@/types/workflow';
import CustomNode from './CustomNode';
import ReActRuntimeCanvas, { buildAgentTrace } from './ReActRuntimeCanvas';

const { Text, Title } = Typography;

const nodeTypes = {
  taskNode: CustomNode,
};

const dynamicTerminalStatuses = new Set<DynamicRunStatus>([
  'finalized',
  'succeeded',
  'failed',
  'canceled',
  'cancelled',
  'timed_out',
  'interrupted',
]);

const dynamicStatusColors: Record<DynamicRunStatus, string> = {
  created: 'default',
  running: 'processing',
  finalized: 'success',
  succeeded: 'success',
  failed: 'error',
  canceled: 'orange',
  cancelled: 'orange',
  timed_out: 'volcano',
  interrupted: 'magenta',
};

interface WorkflowCanvasProps {
  activeDynamicRunId?: string | null;
  onOpenRuns?: () => void;
}

function toNodeRunStatus(status?: DynamicRunStatus | null) {
  if (!status) return null;
  if (status === 'finalized' || status === 'succeeded') return 'completed';
  if (status === 'failed') return 'failed';
  if (status === 'interrupted' || status === 'canceled' || status === 'cancelled' || status === 'timed_out') return 'interrupted';
  return 'running';
}

function formatTime(value?: number | null) {
  if (!value) return '-';
  return new Date(value * 1000).toLocaleString();
}

export default function WorkflowCanvas({ activeDynamicRunId, onOpenRuns }: WorkflowCanvasProps) {
  const {
    nodes,
    edges,
    setNodes,
    setEdges,
    addNode,
    deleteNode,
    selectNode,
    workflowId,
    setWorkflowId,
    activeRunId,
    selectedRunId,
    staticRuns,
  } = useWorkflowStore();
  const [runtimeCanvasOpen, setRuntimeCanvasOpen] = React.useState(false);
  const [dynamicRun, setDynamicRun] = React.useState<DynamicRunSnapshot | null>(null);
  const [dynamicEvents, setDynamicEvents] = React.useState<DynamicRunEvent[]>([]);
  const [dynamicLoading, setDynamicLoading] = React.useState(false);
  
  const visibleRunId = selectedRunId || activeRunId;
  const visibleRun = visibleRunId ? staticRuns.find((run) => run.run_id === visibleRunId) : null;
  const dynamicNodeStatus = toNodeRunStatus(dynamicRun?.status);
  const nodesWithRunState = React.useMemo(() => (
    nodes.map((node) => ({
      ...node,
      data: {
        ...node.data,
        runState: node.data.category === 'agent' && activeDynamicRunId && dynamicNodeStatus
          ? { status: dynamicNodeStatus }
          : visibleRun?.task_nodes?.[node.id] || null,
        runStatus: node.data.category === 'agent' && activeDynamicRunId && dynamicNodeStatus
          ? dynamicNodeStatus
          : visibleRun?.status || null,
      },
    }))
  ), [activeDynamicRunId, dynamicNodeStatus, nodes, visibleRun]);

  const [reactFlowNodes, setReactFlowNodes, onNodesChange] = useNodesState(nodesWithRunState);
  const [reactFlowEdges, setReactFlowEdges, onEdgesChange] = useEdgesState(edges);
  const reactFlowWrapper = useRef<HTMLDivElement>(null);
  const [reactFlowInstance, setReactFlowInstance] = React.useState<ReactFlowInstance | null>(null);
  const creatingWorkflowRef = useRef<Promise<string> | null>(null);

  const lastRenderedNodesRef = useRef<string>('');
  const lastRenderedEdgesRef = useRef<string>('');
  const agentTrace = React.useMemo(() => buildAgentTrace(dynamicEvents), [dynamicEvents]);

  const loadActiveDynamicRun = useCallback(async (silent = false) => {
    if (!activeDynamicRunId) {
      return;
    }

    if (!silent) {
      setDynamicLoading(true);
    }

    try {
      const [runResult, eventResult] = await Promise.all([
        api.getDynamicRun(activeDynamicRunId),
        api.getDynamicRunEvents(activeDynamicRunId),
      ]);
      setDynamicRun(runResult.run);
      setDynamicEvents(eventResult.events || []);
    } catch (error: any) {
      console.error('Failed to load active dynamic run:', error);
      if (!silent) {
        message.error(error.response?.data?.error || 'Failed to load active dynamic run');
      }
    } finally {
      if (!silent) {
        setDynamicLoading(false);
      }
    }
  }, [activeDynamicRunId]);

  useEffect(() => {
    if (!activeDynamicRunId) {
      setRuntimeCanvasOpen(false);
      setDynamicRun(null);
      setDynamicEvents([]);
      return;
    }

    setRuntimeCanvasOpen(true);
    setDynamicRun(null);
    setDynamicEvents([]);
    void loadActiveDynamicRun();
  }, [activeDynamicRunId, loadActiveDynamicRun]);

  useEffect(() => {
    if (!activeDynamicRunId) {
      return undefined;
    }

    if (dynamicRun && dynamicTerminalStatuses.has(dynamicRun.status)) {
      return undefined;
    }

    const timer = window.setInterval(() => {
      void loadActiveDynamicRun(true);
    }, 1000);

    return () => window.clearInterval(timer);
  }, [activeDynamicRunId, dynamicRun?.status, loadActiveDynamicRun]);
  
  useEffect(() => {
    const nodesStr = JSON.stringify(nodesWithRunState);
    if (nodesStr !== lastRenderedNodesRef.current) {
      setReactFlowNodes(nodesWithRunState);
      lastRenderedNodesRef.current = nodesStr;
    }
  }, [nodesWithRunState, setReactFlowNodes]);

  useEffect(() => {
    const edgesStr = JSON.stringify(edges);
    if (edgesStr !== lastRenderedEdgesRef.current) {
      setReactFlowEdges(edges);
      lastRenderedEdgesRef.current = edgesStr;
    }
  }, [edges, setReactFlowEdges]);

  useEffect(() => {
    const normalizedNodes = reactFlowNodes.map(rfNode => {
      const storeNode = nodes.find(n => n.id === rfNode.id);
      if (storeNode) {
        return {
          ...storeNode,
          position: rfNode.position
        };
      }

      const { runState, runStatus, ...data } = rfNode.data || {};
      return {
        ...rfNode,
        data,
      } as WorkflowNode;
    }) as WorkflowNode[];
    const normalizedEdges = reactFlowEdges.map((edge) => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      sourceHandle: edge.sourceHandle || undefined,
      targetHandle: edge.targetHandle || undefined,
    })) as WorkflowEdge[];
    const normalizedNodesStr = JSON.stringify(normalizedNodes);
    const normalizedEdgesStr = JSON.stringify(normalizedEdges);

    if (normalizedNodesStr === JSON.stringify(nodes) && normalizedEdgesStr === JSON.stringify(edges)) {
      return;
    }

    const timer = setTimeout(() => {
      if (normalizedNodesStr !== JSON.stringify(nodes)) {
        setNodes(normalizedNodes);
        lastRenderedNodesRef.current = normalizedNodesStr;
      }
      if (normalizedEdgesStr !== JSON.stringify(edges)) {
        setEdges(normalizedEdges);
        lastRenderedEdgesRef.current = normalizedEdgesStr;
      }
    }, 500);

    return () => clearTimeout(timer);
  }, [reactFlowNodes, reactFlowEdges, setNodes, setEdges, nodes, edges]);

  const onConnect = useCallback(
    (params: Connection | Edge) => {
      const newEdges = addEdge(params, reactFlowEdges);
      setReactFlowEdges(newEdges);
    },
    [reactFlowEdges, setReactFlowEdges]
  );

  const onNodeClick = useCallback(
    (_: React.MouseEvent, clickedNode: any) => {
      const latestNode = nodes.find(n => n.id === clickedNode.id) || clickedNode;
      selectNode(latestNode);
    },
    [selectNode, nodes]
  );

  const onNodesDelete = useCallback(
    (deletedNodes: ReactFlowNode[]) => {
      deletedNodes.forEach((node) => deleteNode(node.id));
    },
    [deleteNode]
  );

  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
  }, []);

  const ensureWorkflow = useCallback(async () => {
    if (workflowId) {
      return workflowId;
    }

    if (!creatingWorkflowRef.current) {
      const hideLoading = message.loading('Creating workflow...', 0);
      creatingWorkflowRef.current = api.createWorkflow()
        .then(({ workflowId: newWorkflowId }) => {
          setWorkflowId(newWorkflowId);
          message.success('Workflow created automatically');
          return newWorkflowId;
        })
        .catch((error) => {
          console.error('Failed to auto-create workflow:', error);
          message.error('Failed to create workflow');
          throw error;
        })
        .finally(() => {
          hideLoading();
          creatingWorkflowRef.current = null;
        });
    }

    return creatingWorkflowRef.current;
  }, [workflowId, setWorkflowId]);

  const onDrop = useCallback(
    async (event: React.DragEvent) => {
      event.preventDefault();

      if (!reactFlowInstance) {
        return;
      }

      const data = event.dataTransfer.getData('application/reactflow');
      if (!data) {
        return;
      }

      try {
        const { type, task } = JSON.parse(data);
        const dropPoint = {
          x: event.clientX,
          y: event.clientY,
        };

        const position = reactFlowInstance.screenToFlowPosition(dropPoint);
        await ensureWorkflow();

        if (type === 'builtin') {
          const builtinTask = task as BuiltinTaskMeta;

          const newNode = {
            id: `node-${Date.now()}`,
            type: 'taskNode' as const,
            position,
            data: {
              category: 'builtin' as const,
              nodeType: 'task' as const,
              label: builtinTask.displayName,
              taskRef: `${builtinTask.module}.${builtinTask.functionRef}`,
              inputs: builtinTask.inputs.map(inp => ({
                name: inp.name,
                dataType: inp.dataType,
                source: 'user' as const,
                value: ''
              })),
              outputs: builtinTask.outputs,
              resources: builtinTask.resources,
              configured: true,
            },
          };

          addNode(newNode);
        } else if (type === 'workspace') {
          const workspaceTask = task as WorkspaceTaskMeta;

          const newNode = {
            id: `node-${Date.now()}`,
            type: 'taskNode' as const,
            position,
            data: {
              category: 'workspace' as const,
              nodeType: 'task' as const,
              label: workspaceTask.displayName || workspaceTask.name,
              customCode: workspaceTask.code,
              workspaceDir: workspaceTask.workspaceDir,
              taskPath: workspaceTask.relativePath,
              functionName: workspaceTask.functionName,
              inputs: workspaceTask.inputs.map(inp => ({
                name: inp.name,
                dataType: inp.dataType,
                source: 'user' as const,
                value: ''
              })),
              outputs: workspaceTask.outputs,
              resources: workspaceTask.resources,
              configured: true,
            },
          };

          addNode(newNode);
          selectNode(newNode);
        } else if (type === 'custom') {
          const newNode = {
            id: `node-${Date.now()}`,
            type: 'taskNode' as const,
            position,
            data: {
              category: 'custom' as const,
              nodeType: 'task' as const,
              label: 'Custom Task',
              customCode: '',
              inputs: [],
              outputs: [],
              configured: false,
            },
          };

          addNode(newNode);
          selectNode(newNode);
        } else if (type === 'agent-react') {
          const newNode = {
            id: `node-${Date.now()}`,
            type: 'taskNode' as const,
            position,
            data: {
              category: 'agent' as const,
              nodeType: 'task' as const,
              label: 'ReAct Workflow',
              agentKind: 'react' as const,
              reactMode: 'local' as const,
              prompt: 'Use the calculator to compute 18 * 7, then give the final answer.',
              maxSteps: 4,
              maxTokens: 2048,
              inputs: [],
              outputs: [{ name: 'answer', dataType: 'any' }],
              configured: true,
            },
          };

          addNode(newNode);
          selectNode(newNode);
        } else if (type === 'workflow-distributed-smoke') {
          const baseId = Date.now();
          const smokeNodes = Array.from({ length: 2 }, (_, index) => ({
            id: `node-${baseId}-${index + 1}`,
            type: 'taskNode' as const,
            position: {
              x: position.x + (index % 2) * 260,
              y: position.y + Math.floor(index / 2) * 180,
            },
            data: {
              category: 'builtin' as const,
              nodeType: 'task' as const,
              label: `GPU Probe ${index + 1}`,
              taskRef: 'distributedSmoke.distributed_gpu_probe',
              inputs: [
                {
                  name: 'probe_id',
                  dataType: 'int',
                  source: 'user' as const,
                  value: String(index + 1),
                },
                {
                  name: 'sleep_seconds',
                  dataType: 'int',
                  source: 'user' as const,
                  value: '1',
                },
              ],
              outputs: [{ name: 'placement', dataType: 'dict' }],
              resources: { cpu: 1, cpu_mem: 128, gpu: 1, gpu_mem: 0 },
              configured: true,
            },
          }));

          smokeNodes.forEach((node) => addNode(node));
          selectNode(smokeNodes[0]);
        }
      } catch (error) {
        console.error('Failed to drop node:', error);
      }
    },
    [reactFlowInstance, ensureWorkflow, addNode, selectNode]
  );

  return (
    <div ref={reactFlowWrapper} style={{ width: '100%', height: '100%' }}>
      <ReactFlow
        nodes={reactFlowNodes}
        edges={reactFlowEdges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onNodeClick={onNodeClick}
        onNodesDelete={onNodesDelete}
        onInit={setReactFlowInstance}
        onDrop={onDrop}
        onDragOver={onDragOver}
        deleteKeyCode={['Backspace', 'Delete']}
        nodeTypes={nodeTypes}
        fitView
      >
        <Background />
        <Controls />
        <MiniMap />
      </ReactFlow>

      {activeDynamicRunId && runtimeCanvasOpen && (
        <div
          style={{
            position: 'absolute',
            inset: 16,
            zIndex: 10,
            display: 'flex',
            flexDirection: 'column',
            minHeight: 0,
            background: '#fff',
            border: '1px solid #d9d9d9',
            borderRadius: 8,
            boxShadow: '0 12px 32px rgba(15, 23, 42, 0.16)',
            overflow: 'hidden',
          }}
        >
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: 12,
              padding: '12px 14px',
              borderBottom: '1px solid #f0f0f0',
              background: '#fff',
            }}
          >
            <div style={{ minWidth: 0 }}>
              <Space size={8} wrap>
                <Title level={5} style={{ margin: 0 }}>
                  Live ReAct Runtime
                </Title>
                {dynamicRun ? (
                  <Tag color={dynamicStatusColors[dynamicRun.status] || 'default'}>{dynamicRun.status}</Tag>
                ) : (
                  <Tag>loading</Tag>
                )}
              </Space>
              <Space size={8} wrap style={{ marginTop: 4 }}>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {activeDynamicRunId}
                </Text>
                {dynamicRun?.created_time && (
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    Started {formatTime(dynamicRun.created_time)}
                  </Text>
                )}
              </Space>
            </div>

            <Space>
              <Button onClick={onOpenRuns}>
                Open Runs
              </Button>
              <Button onClick={() => setRuntimeCanvasOpen(false)}>
                Back to Editor
              </Button>
            </Space>
          </div>

          <div style={{ flex: 1, minHeight: 0, padding: 12, background: '#fafafa' }}>
            {dynamicLoading && !dynamicRun ? (
              <div style={{ height: '100%', display: 'grid', placeItems: 'center' }}>
                <Space direction="vertical" align="center">
                  <Spin />
                  <Text type="secondary">Loading runtime graph...</Text>
                </Space>
              </div>
            ) : (
              <ReActRuntimeCanvas
                trace={agentTrace}
                run={dynamicRun}
                height="100%"
                emptyDescription="Waiting for ReAct runtime events..."
              />
            )}
          </div>
        </div>
      )}
    </div>
  );
}
