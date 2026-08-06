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
  UnifiedRunTaskSnapshot,
  WorkspaceTaskMeta,
  WorkflowEdge,
  WorkflowNode,
} from '@/types/workflow';
import CustomNode from './CustomNode';
import { bindWorkflowConnection, unbindWorkflowEdges } from '@/utils/workflowBindings';
import {
  latestRunForWorkflow,
  runMatchesWorkflow,
  runWorkflowGraph,
} from '@/utils/runSnapshot';

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

function hasSameNodePositions(current: WorkflowNode[], next: WorkflowNode[]) {
  if (current.length !== next.length) return false;
  const currentById = new Map(current.map((node) => [node.id, node]));
  return next.every((node) => {
    const existing = currentById.get(node.id);
    return existing
      && existing.position.x === node.position.x
      && existing.position.y === node.position.y;
  });
}

function hasSameEdges(current: WorkflowEdge[], next: WorkflowEdge[]) {
  if (current.length !== next.length) return false;
  const currentById = new Map(current.map((edge) => [edge.id, edge]));
  return next.every((edge) => {
    const existing = currentById.get(edge.id);
    return existing
      && existing.source === edge.source
      && existing.target === edge.target
      && existing.sourceHandle === edge.sourceHandle
      && existing.targetHandle === edge.targetHandle;
  });
}

export default function WorkflowCanvas() {
  const workflowId = useWorkflowStore((state) => state.workflowId);
  const workspaceId = useWorkflowStore((state) => state.workspaceId);
  const workspaceDir = useWorkflowStore((state) => state.workspaceDir);
  const workflowPath = useWorkflowStore((state) => state.currentWorkspaceWorkflowPath);
  const nodes = useWorkflowStore((state) => state.nodes);
  const edges = useWorkflowStore((state) => state.edges);
  const setNodes = useWorkflowStore((state) => state.setNodes);
  const setEdges = useWorkflowStore((state) => state.setEdges);
  const addNode = useWorkflowStore((state) => state.addNode);
  const deleteNode = useWorkflowStore((state) => state.deleteNode);
  const selectNode = useWorkflowStore((state) => state.selectNode);
  const selectedRunId = useWorkflowStore((state) => state.selectedRunId);
  const setSelectedRunTaskId = useWorkflowStore((state) => state.setSelectedRunTaskId);
  const staticRuns = useWorkflowStore((state) => state.staticRuns);

  const identity = React.useMemo(() => ({
    workflowId,
    workflowPath,
    workspaceId,
    workspaceDir,
  }), [workflowId, workflowPath, workspaceDir, workspaceId]);
  const selectedRun = selectedRunId
    ? staticRuns.find((run) => run.run_id === selectedRunId) || null
    : null;
  const designRun = React.useMemo(
    () => latestRunForWorkflow(staticRuns, identity),
    [identity, staticRuns],
  );
  const isRunView = Boolean(selectedRunId);
  const visibleRun = isRunView ? selectedRun : designRun;
  const historicalGraph = React.useMemo(() => {
    if (!selectedRun) return { nodes: [], edges: [] };
    const positionNodes = runMatchesWorkflow(selectedRun, identity) ? nodes : [];
    return runWorkflowGraph(selectedRun, positionNodes);
  }, [identity, nodes, selectedRun]);
  const nodesWithRunState = React.useMemo(() => (
    isRunView
      ? historicalGraph.nodes
      : nodes.map((node) => ({
        ...node,
        data: {
          ...node.data,
          runState: visibleRun?.task_nodes?.[node.id] || null,
          runStatus: visibleRun?.task_nodes?.[node.id]?.status || null,
        },
      }))
  ), [historicalGraph.nodes, isRunView, nodes, visibleRun]);
  const visibleEdges = React.useMemo(() => (
    isRunView ? historicalGraph.edges : edges
  ), [edges, historicalGraph.edges, isRunView]);

  const [reactFlowNodes, setReactFlowNodes, applyNodesChange] = useNodesState(nodesWithRunState);
  const [reactFlowEdges, setReactFlowEdges, applyEdgesChange] = useEdgesState(visibleEdges);
  const reactFlowWrapper = useRef<HTMLDivElement>(null);
  const [reactFlowInstance, setReactFlowInstance] = React.useState<ReactFlowInstance | null>(null);
  const copiedNodeRef = useRef<WorkflowNode | null>(null);
  const pasteIndexRef = useRef(1);
  
  useEffect(() => {
    setReactFlowNodes(nodesWithRunState);
  }, [nodesWithRunState, setReactFlowNodes]);

  useEffect(() => {
    setReactFlowEdges(visibleEdges);
  }, [setReactFlowEdges, visibleEdges]);

  const onNodesChange = useCallback((changes: Parameters<typeof applyNodesChange>[0]) => {
    const allowedChanges = isRunView
      ? changes.filter((change) => change.type === 'select' || change.type === 'dimensions')
      : changes;
    if (allowedChanges.length > 0) {
      applyNodesChange(allowedChanges);
    }
  }, [applyNodesChange, isRunView]);

  const onEdgesChange = useCallback((changes: Parameters<typeof applyEdgesChange>[0]) => {
    const allowedChanges = isRunView
      ? changes.filter((change) => change.type === 'select')
      : changes;
    if (allowedChanges.length > 0) {
      applyEdgesChange(allowedChanges);
    }
  }, [applyEdgesChange, isRunView]);

  useEffect(() => {
    if (!reactFlowInstance || nodesWithRunState.length === 0) return;
    window.setTimeout(() => {
      reactFlowInstance.fitView({ padding: 0.25, duration: 200 });
    }, 0);
  }, [nodesWithRunState.length, reactFlowInstance, selectedRunId]);

  useEffect(() => {
    if (isRunView) return;
    const timer = window.setTimeout(() => {
      const current = useWorkflowStore.getState();
      const currentNodesById = new Map(current.nodes.map((node) => [node.id, node]));
      const normalizedNodes = reactFlowNodes.map((rfNode) => {
        const storeNode = currentNodesById.get(rfNode.id);
        if (storeNode) {
          return {
            ...storeNode,
            position: rfNode.position,
          };
        }

        const { runState, runStatus, ...data } = (rfNode.data || {}) as RuntimeNodeData;
        return {
          id: rfNode.id,
          type: 'taskNode',
          position: rfNode.position,
          data,
        } as WorkflowNode;
      });
      const normalizedEdges = reactFlowEdges.map((edge) => ({
        id: edge.id,
        source: edge.source,
        target: edge.target,
        sourceHandle: edge.sourceHandle || undefined,
        targetHandle: edge.targetHandle || undefined,
      })) as WorkflowEdge[];

      if (!hasSameNodePositions(current.nodes, normalizedNodes)) {
        setNodes(normalizedNodes);
      }
      if (!hasSameEdges(current.edges, normalizedEdges)) {
        setEdges(normalizedEdges);
      }
    }, 500);

    return () => window.clearTimeout(timer);
  }, [isRunView, reactFlowNodes, reactFlowEdges, setNodes, setEdges]);

  const onConnect = useCallback(
    (params: Connection | Edge) => {
      if (isRunView) return;
      const bound = bindWorkflowConnection(nodes, params);
      if (bound.error) {
        message.warning(bound.error);
        return;
      }
      const newEdges = addEdge(params, reactFlowEdges);
      setNodes(bound.nodes);
      setEdges(newEdges.map((edge) => ({
        id: edge.id,
        source: edge.source,
        target: edge.target,
        sourceHandle: edge.sourceHandle || undefined,
        targetHandle: edge.targetHandle || undefined,
      })));
      setReactFlowEdges(newEdges);
    },
    [isRunView, nodes, reactFlowEdges, setEdges, setNodes, setReactFlowEdges]
  );

  const onEdgesDelete = useCallback((deletedEdges: Edge[]) => {
    if (isRunView) return;
    setNodes(unbindWorkflowEdges(nodes, deletedEdges));
  }, [isRunView, nodes, setNodes]);

  const onNodeClick = useCallback(
    (_: React.MouseEvent, clickedNode: any) => {
      if (isRunView) {
        setSelectedRunTaskId(clickedNode.id);
        return;
      }
      const latestNode = nodes.find((node) => node.id === clickedNode.id) || clickedNode;
      selectNode(latestNode);
    },
    [isRunView, nodes, selectNode, setSelectedRunTaskId]
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

        if (type === 'workspace') {
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
        onEdgesDelete={onEdgesDelete}
        onConnect={onConnect}
        onNodeClick={onNodeClick}
        onNodesDelete={onNodesDelete}
        onInit={setReactFlowInstance}
        onDrop={onDrop}
        onDragOver={onDragOver}
        deleteKeyCode={isRunView ? null : ['Backspace', 'Delete']}
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
