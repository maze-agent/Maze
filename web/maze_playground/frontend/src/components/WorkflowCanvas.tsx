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
import { message } from 'antd';
import { useWorkflowStore } from '@/stores/workflowStore';
import type {
  BuiltinTaskMeta,
  UnifiedRunSnapshot,
  UnifiedRunTaskSnapshot,
  WorkspaceTaskMeta,
  WorkflowEdge,
  WorkflowNode,
} from '@/types/workflow';
import CustomNode from './CustomNode';

const nodeTypes = {
  taskNode: CustomNode,
};

const COPY_PASTE_OFFSET = 48;

type RuntimeNodeData = WorkflowNode['data'] & {
  runState?: UnifiedRunTaskSnapshot | null;
  runStatus?: string | null;
};

function isEditableTarget(target: EventTarget | null) {
  if (!(target instanceof HTMLElement)) return false;
  const tagName = target.tagName.toLowerCase();
  return (
    tagName === 'input'
    || tagName === 'textarea'
    || tagName === 'select'
    || target.isContentEditable
    || Boolean(target.closest('.monaco-editor'))
  );
}

function cloneTaskInputs(inputs: WorkflowNode['data']['inputs']) {
  return inputs.map((input) => {
    if (input.source === 'task') {
      return {
        ...input,
        source: 'user' as const,
        value: '',
        taskSource: undefined,
      };
    }
    return { ...input };
  });
}

function copyableNode(node: WorkflowNode): WorkflowNode {
  const { runState, runStatus, ...data } = node.data as WorkflowNode['data'] & {
    runState?: unknown;
    runStatus?: unknown;
  };
  const hasTaskSourceInput = (data.inputs || []).some((input) => input.source === 'task');
  return {
    ...node,
    position: { ...node.position },
    data: {
      ...data,
      inputs: cloneTaskInputs(data.inputs || []),
      outputs: (data.outputs || []).map((output) => ({ ...output })),
      resources: data.resources ? { ...data.resources } : undefined,
      configured: hasTaskSourceInput ? false : data.configured,
    },
  };
}

function copiedLabel(label: string, existingLabels: Set<string>) {
  const base = `${label} Copy`;
  if (!existingLabels.has(base)) return base;
  for (let index = 2; index < 1000; index += 1) {
    const candidate = `${base} ${index}`;
    if (!existingLabels.has(candidate)) return candidate;
  }
  return `${base} ${Date.now()}`;
}

function duplicateNode(node: WorkflowNode, existingNodes: WorkflowNode[], pasteIndex: number): WorkflowNode {
  const labels = new Set(existingNodes.map((item) => item.data.label));
  const offset = COPY_PASTE_OFFSET * pasteIndex;
  const copied = copyableNode(node);
  return {
    ...copied,
    id: `node-${Date.now()}-${pasteIndex}`,
    position: {
      x: node.position.x + offset,
      y: node.position.y + offset,
    },
    data: {
      ...copied.data,
      label: copiedLabel(node.data.label, labels),
    },
  };
}

function runTaskResources(task: UnifiedRunTaskSnapshot) {
  const resources = task.resources || {};
  return {
    cpu_num: Number((resources as any).cpu_num ?? (resources as any).cpu ?? 1),
    gpu_mem: Number((resources as any).gpu_mem || 0),
    io_num: Number((resources as any).io_num || 0),
  };
}

function nodeFromRunTask(
  taskId: string,
  task: UnifiedRunTaskSnapshot,
  index: number,
  currentNodes: WorkflowNode[],
): WorkflowNode {
  const existing = currentNodes.find((node) => node.id === taskId);
  return {
    id: taskId,
    type: 'taskNode',
    position: existing?.position || {
      x: 160 + (index % 3) * 280,
      y: 120 + Math.floor(index / 3) * 180,
    },
    data: {
      category: (existing?.data.category || 'builtin') as WorkflowNode['data']['category'],
      nodeType: 'task',
      label: task.task_name || existing?.data.label || taskId,
      taskRef: existing?.data.taskRef,
      customCode: existing?.data.customCode,
      workspaceDir: existing?.data.workspaceDir,
      taskPath: existing?.data.taskPath,
      functionName: existing?.data.functionName,
      inputs: existing?.data.inputs || [],
      outputs: existing?.data.outputs || [],
      task_kind: (task.task_kind || existing?.data.task_kind || 'cpu') as any,
      resources: existing?.data.resources || runTaskResources(task),
      configured: true,
      runState: task,
      runStatus: task.status,
    } as RuntimeNodeData,
  };
}

function runViewNodes(run: UnifiedRunSnapshot, currentNodes: WorkflowNode[]) {
  const taskNodes = run.task_nodes || {};
  const graphNodeIds = run.graph?.nodes || [];
  const ids = graphNodeIds.length > 0 ? graphNodeIds : Object.keys(taskNodes);
  return ids.map((taskId, index) => (
    nodeFromRunTask(taskId, taskNodes[taskId] || {
      task_id: taskId,
      status: 'pending',
    }, index, currentNodes)
  ));
}

function runViewEdges(run: UnifiedRunSnapshot): WorkflowEdge[] {
  return (run.graph?.edges || []).map((edge, index) => ({
    id: `run-edge-${edge.source}-${edge.target}-${index}`,
    source: edge.source,
    target: edge.target,
  }));
}

export default function WorkflowCanvas() {
  const {
    nodes,
    edges,
    setNodes,
    setEdges,
    addNode,
    deleteNode,
    selectNode,
    activeRunId,
    selectedRunId,
    staticRuns,
  } = useWorkflowStore();
  
  const visibleRunId = selectedRunId || activeRunId;
  const visibleRun = visibleRunId ? staticRuns.find((run) => run.run_id === visibleRunId) : null;
  const isRunView = Boolean(selectedRunId && visibleRun);
  const nodesWithRunState = React.useMemo(() => (
    isRunView && visibleRun
      ? runViewNodes(visibleRun, nodes)
      : nodes.map((node) => ({
        ...node,
        data: {
          ...node.data,
          runState: visibleRun?.task_nodes?.[node.id] || null,
          runStatus: visibleRun?.task_nodes?.[node.id]?.status || null,
        },
      }))
  ), [isRunView, nodes, visibleRun]);
  const visibleEdges = React.useMemo(() => (
    isRunView && visibleRun ? runViewEdges(visibleRun) : edges
  ), [edges, isRunView, visibleRun]);

  const [reactFlowNodes, setReactFlowNodes, onNodesChange] = useNodesState(nodesWithRunState);
  const [reactFlowEdges, setReactFlowEdges, onEdgesChange] = useEdgesState(visibleEdges);
  const reactFlowWrapper = useRef<HTMLDivElement>(null);
  const [reactFlowInstance, setReactFlowInstance] = React.useState<ReactFlowInstance | null>(null);
  const copiedNodeRef = useRef<WorkflowNode | null>(null);
  const pasteIndexRef = useRef(1);

  const lastRenderedNodesRef = useRef<string>('');
  const lastRenderedEdgesRef = useRef<string>('');
  
  useEffect(() => {
    const nodesStr = JSON.stringify(nodesWithRunState);
    if (nodesStr !== lastRenderedNodesRef.current) {
      setReactFlowNodes(nodesWithRunState);
      lastRenderedNodesRef.current = nodesStr;
    }
  }, [nodesWithRunState, setReactFlowNodes]);

  useEffect(() => {
    const edgesStr = JSON.stringify(visibleEdges);
    if (edgesStr !== lastRenderedEdgesRef.current) {
      setReactFlowEdges(visibleEdges);
      lastRenderedEdgesRef.current = edgesStr;
    }
  }, [setReactFlowEdges, visibleEdges]);

  useEffect(() => {
    if (!reactFlowInstance || nodesWithRunState.length === 0) return;
    window.setTimeout(() => {
      reactFlowInstance.fitView({ padding: 0.25, duration: 200 });
    }, 0);
  }, [nodesWithRunState.length, reactFlowInstance, visibleRunId]);

  useEffect(() => {
    if (isRunView) return;
    const normalizedNodes = reactFlowNodes.map(rfNode => {
      const storeNode = nodes.find(n => n.id === rfNode.id);
      if (storeNode) {
        return {
          ...storeNode,
          position: rfNode.position
        };
      }

      const { runState, runStatus, ...data } = (rfNode.data || {}) as RuntimeNodeData;
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
  }, [isRunView, reactFlowNodes, reactFlowEdges, setNodes, setEdges, nodes, edges]);

  const onConnect = useCallback(
    (params: Connection | Edge) => {
      if (isRunView) return;
      const newEdges = addEdge(params, reactFlowEdges);
      setReactFlowEdges(newEdges);
    },
    [isRunView, reactFlowEdges, setReactFlowEdges]
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
      if (isRunView) return;
      deletedNodes.forEach((node) => deleteNode(node.id));
    },
    [deleteNode, isRunView]
  );

  const copySelectedNode = useCallback(() => {
    const selectedNode = useWorkflowStore.getState().selectedNode;
    if (!selectedNode) return;
    copiedNodeRef.current = copyableNode(selectedNode);
    pasteIndexRef.current = 1;
    message.success('Task copied');
  }, []);

  const pasteCopiedNode = useCallback(() => {
    const copiedNode = copiedNodeRef.current;
    if (!copiedNode) return;
    const currentNodes = useWorkflowStore.getState().nodes;
    const newNode = duplicateNode(copiedNode, currentNodes, pasteIndexRef.current);
    pasteIndexRef.current += 1;
    addNode(newNode);
    selectNode(newNode);
    message.success('Task pasted');
  }, [addNode, selectNode]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (isRunView) return;
      if (!(event.ctrlKey || event.metaKey) || isEditableTarget(event.target)) return;
      const key = event.key.toLowerCase();
      if (key === 'c') {
        const selectedNode = useWorkflowStore.getState().selectedNode;
        if (!selectedNode) return;
        event.preventDefault();
        copySelectedNode();
      } else if (key === 'v') {
        if (!copiedNodeRef.current) return;
        event.preventDefault();
        pasteCopiedNode();
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => {
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [copySelectedNode, isRunView, pasteCopiedNode]);

  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
  }, []);

  const onDrop = useCallback(
    async (event: React.DragEvent) => {
      event.preventDefault();

      if (isRunView) {
        message.info('Open the workflow draft to edit tasks. Run views are read-only.');
        return;
      }

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
        } else if (type === 'workflow-distributed-smoke') {
          const baseId = Date.now();
          const smokeNodes: WorkflowNode[] = Array.from({ length: 2 }, (_, index) => ({
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
              task_kind: 'gpu' as const,
              resources: { cpu_num: 1, gpu_mem: 0, io_num: 0 },
              configured: true,
            },
          }));

          smokeNodes.forEach((node) => addNode(node));
          selectNode(smokeNodes[0]);
        } else if (type === 'workspace-example-task' || type === 'workflow-resource-soak') {
          message.info('Use Add in the Library to import this example into the workspace.');
        }
      } catch (error) {
        console.error('Failed to drop node:', error);
      }
    },
    [isRunView, reactFlowInstance, addNode, selectNode]
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
        nodesDraggable={!isRunView}
        nodesConnectable={!isRunView}
        elementsSelectable
        nodeTypes={nodeTypes}
        fitView
      >
        <Background />
        <Controls />
        <MiniMap />
      </ReactFlow>
    </div>
  );
}
