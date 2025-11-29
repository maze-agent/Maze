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
import { useWorkflowStore } from '@/stores/workflowStore';
import type { BuiltinTaskMeta } from '@/types/workflow';
import CustomNode from './CustomNode';

const nodeTypes = {
  taskNode: CustomNode,
};

export default function WorkflowCanvas() {
  const { nodes, edges, setNodes, setEdges, addNode, selectNode, workflowId } = useWorkflowStore();
  
  const [reactFlowNodes, setReactFlowNodes, onNodesChange] = useNodesState(nodes);
  const [reactFlowEdges, setReactFlowEdges, onEdgesChange] = useEdgesState(edges);
  const reactFlowWrapper = useRef<HTMLDivElement>(null);
  const [reactFlowInstance, setReactFlowInstance] = React.useState<ReactFlowInstance | null>(null);

  const lastSyncedNodesRef = useRef<string>('');
  const lastSyncedEdgesRef = useRef<string>('');
  
  const prevNodeCountRef = useRef(nodes.length);
  useEffect(() => {
    if (nodes.length !== prevNodeCountRef.current) {
      setReactFlowNodes(nodes);
      prevNodeCountRef.current = nodes.length;
      lastSyncedNodesRef.current = JSON.stringify(nodes);
    }
  }, [nodes, setReactFlowNodes]);

  const prevEdgeCountRef = useRef(edges.length);
  useEffect(() => {
    if (edges.length !== prevEdgeCountRef.current) {
      setReactFlowEdges(edges);
      prevEdgeCountRef.current = edges.length;
      lastSyncedEdgesRef.current = JSON.stringify(edges);
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
        
        setNodes(updatedNodes);
        setEdges(reactFlowEdges);
        lastSyncedNodesRef.current = positionsStr;
        lastSyncedEdgesRef.current = edgesStr;
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

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();

      if (!workflowId || !reactFlowInstance) {
        return;
      }

      const data = event.dataTransfer.getData('application/reactflow');
      if (!data) {
        return;
      }

      try {
        const { type, task } = JSON.parse(data);

        const position = reactFlowInstance.screenToFlowPosition({
          x: event.clientX,
          y: event.clientY,
        });

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
    [workflowId, reactFlowInstance, addNode, selectNode]
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
