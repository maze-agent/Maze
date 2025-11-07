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

  // 用于记录最后同步的状态，避免循环
  const lastSyncedNodesRef = useRef<string>('');
  const lastSyncedEdgesRef = useRef<string>('');
  
  // 只在节点数量变化时（新增/删除）才从 store 同步到 ReactFlow
  const prevNodeCountRef = useRef(nodes.length);
  useEffect(() => {
    if (nodes.length !== prevNodeCountRef.current) {
      setReactFlowNodes(nodes);
      prevNodeCountRef.current = nodes.length;
      // 更新同步记录，避免反向同步
      lastSyncedNodesRef.current = JSON.stringify(nodes);
    }
  }, [nodes, setReactFlowNodes]);

  // 只在边数量变化时才从 store 同步到 ReactFlow
  const prevEdgeCountRef = useRef(edges.length);
  useEffect(() => {
    if (edges.length !== prevEdgeCountRef.current) {
      setReactFlowEdges(edges);
      prevEdgeCountRef.current = edges.length;
      // 更新同步记录，避免反向同步
      lastSyncedEdgesRef.current = JSON.stringify(edges);
    }
  }, [edges, setReactFlowEdges]);

  // 使用防抖将 ReactFlow 状态同步回 store（仅同步位置信息）
  useEffect(() => {
    // 只同步位置变化，不同步节点数据内容
    // 这样可以避免覆盖用户在其他地方更新的节点数据（如 customCode）
    const positionsStr = JSON.stringify(reactFlowNodes.map(n => ({ id: n.id, position: n.position })));
    const edgesStr = JSON.stringify(reactFlowEdges);
    
    // 只有当位置或边真正变化时才同步
    if (positionsStr !== lastSyncedNodesRef.current || edgesStr !== lastSyncedEdgesRef.current) {
      const timer = setTimeout(() => {
        // 从 store 获取最新的节点数据，只更新位置
        const updatedNodes = reactFlowNodes.map(rfNode => {
          const storeNode = nodes.find(n => n.id === rfNode.id);
          if (storeNode) {
            // 保留 store 中的数据，只更新位置
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
      }, 500); // 500ms 防抖

      return () => clearTimeout(timer);
    }
  }, [reactFlowNodes, reactFlowEdges, setNodes, setEdges, nodes]);

  // 处理连接
  const onConnect = useCallback(
    (params: Connection | Edge) => {
      const newEdges = addEdge(params, reactFlowEdges);
      setReactFlowEdges(newEdges);
    },
    [reactFlowEdges, setReactFlowEdges]
  );

  // 处理节点点击
  const onNodeClick = useCallback(
    (_: React.MouseEvent, clickedNode: any) => {
      // 从 store 中获取最新的节点数据，而不是使用 ReactFlow 的节点对象
      const latestNode = nodes.find(n => n.id === clickedNode.id) || clickedNode;
      console.log('🖱️ 节点点击');
      console.log('   节点ID:', clickedNode.id);
      console.log('   使用最新数据:', latestNode.data.customCode?.length || 0, '字符');
      selectNode(latestNode);
    },
    [selectNode, nodes]
  );

  // 处理拖放
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

        // 获取画布上的位置
        const position = reactFlowInstance.screenToFlowPosition({
          x: event.clientX,
          y: event.clientY,
        });

        if (type === 'builtin') {
          const builtinTask = task as BuiltinTaskMeta;

          // 创建内置任务节点
          const newNode = {
            id: `node-${Date.now()}`,
            type: 'taskNode' as const,
            position,
            data: {
              category: 'builtin' as const,
              nodeType: builtinTask.nodeType,
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
          // 创建自定义任务节点
          const newNode = {
            id: `node-${Date.now()}`,
            type: 'taskNode' as const,
            position,
            data: {
              category: 'custom' as const,
              nodeType: 'task' as const,
              label: '自定义任务',
              customCode: '',
              inputs: [],
              outputs: [],
              configured: false,
            },
          };

          addNode(newNode);
          // 自动选中节点，提示用户配置
          selectNode(newNode);
        }
      } catch (error) {
        console.error('拖放节点失败:', error);
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

