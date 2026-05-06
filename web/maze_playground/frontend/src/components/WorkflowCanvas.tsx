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
} from 'reactflow';
import { message } from 'antd';
import { useWorkflowStore } from '@/stores/workflowStore';
import { api } from '@/api/client';
import type { BuiltinTaskMeta, WorkflowEdge, WorkflowNode } from '@/types/workflow';
import CustomNode from './CustomNode';

const nodeTypes = {
  taskNode: CustomNode,
};

export default function WorkflowCanvas() {
  const { nodes, edges, setNodes, setEdges, addNode, selectNode, workflowId, setWorkflowId } = useWorkflowStore();
  
  const [reactFlowNodes, setReactFlowNodes, onNodesChange] = useNodesState(nodes);
  const [reactFlowEdges, setReactFlowEdges, onEdgesChange] = useEdgesState(edges);
  const reactFlowWrapper = useRef<HTMLDivElement>(null);
  const [reactFlowInstance, setReactFlowInstance] = React.useState<ReactFlowInstance | null>(null);
  const creatingWorkflowRef = useRef<Promise<string> | null>(null);

  const lastSyncedNodesRef = useRef<string>('');
  const lastSyncedEdgesRef = useRef<string>('');
  
  useEffect(() => {
    const nodesStr = JSON.stringify(nodes);
    if (nodesStr !== lastSyncedNodesRef.current) {
      setReactFlowNodes(nodes);
      lastSyncedNodesRef.current = nodesStr;
    }
  }, [nodes, setReactFlowNodes]);

  useEffect(() => {
    const edgesStr = JSON.stringify(edges);
    if (edgesStr !== lastSyncedEdgesRef.current) {
      setReactFlowEdges(edges);
      lastSyncedEdgesRef.current = edgesStr;
    }
  }, [edges, setReactFlowEdges]);

  useEffect(() => {
    const positionsStr = JSON.stringify(reactFlowNodes.map(n => ({ id: n.id, position: n.position })));
    const edgesStr = JSON.stringify(reactFlowEdges);
    
    if (positionsStr !== lastSyncedNodesRef.current || edgesStr !== lastSyncedEdgesRef.current) {
      const timer = setTimeout(() => {
        const updatedNodes = reactFlowNodes.map(rfNode => {
          const storeNode = nodes.find(n => n.id === rfNode.id);
          if (storeNode) {
            return {
              ...storeNode,
              position: rfNode.position
            };
          }
          return rfNode;
        });
        
        const normalizedNodes = updatedNodes as WorkflowNode[];
        const normalizedEdges = reactFlowEdges.map((edge) => ({
          id: edge.id,
          source: edge.source,
          target: edge.target,
          sourceHandle: edge.sourceHandle || undefined,
          targetHandle: edge.targetHandle || undefined,
        })) as WorkflowEdge[];
        const normalizedNodesStr = JSON.stringify(normalizedNodes);
        const normalizedEdgesStr = JSON.stringify(normalizedEdges);

        if (normalizedNodesStr !== JSON.stringify(nodes)) {
          setNodes(normalizedNodes);
        }
        if (normalizedEdgesStr !== JSON.stringify(edges)) {
          setEdges(normalizedEdges);
        }

        lastSyncedNodesRef.current = normalizedNodesStr;
        lastSyncedEdgesRef.current = normalizedEdgesStr;
      }, 500);

      return () => clearTimeout(timer);
    }
  }, [reactFlowNodes, reactFlowEdges, setNodes, setEdges, nodes]);

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
        onInit={setReactFlowInstance}
        onDrop={onDrop}
        onDragOver={onDragOver}
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
