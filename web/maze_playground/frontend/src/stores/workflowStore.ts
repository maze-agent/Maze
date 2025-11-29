import { create } from 'zustand';
import type { WorkflowNode, WorkflowEdge, BuiltinTaskMeta, RunResult } from '@/types/workflow';

interface WorkflowStore {
  // Workflow state
  workflowId: string | null;
  workflowName: string;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  selectedNode: WorkflowNode | null;
  
  // Builtin tasks
  builtinTasks: BuiltinTaskMeta[];
  
  // Run state
  isRunning: boolean;
  runResults: RunResult[];
  
  // Actions
  setWorkflowId: (id: string) => void;
  setWorkflowName: (name: string) => void;
  setNodes: (nodes: WorkflowNode[]) => void;
  setEdges: (edges: WorkflowEdge[]) => void;
  addNode: (node: WorkflowNode) => void;
  updateNode: (nodeId: string, updates: Partial<WorkflowNode['data']>) => void;
  deleteNode: (nodeId: string) => void;
  selectNode: (node: WorkflowNode | null) => void;
  setBuiltinTasks: (tasks: BuiltinTaskMeta[]) => void;
  setIsRunning: (isRunning: boolean) => void;
  addRunResult: (result: RunResult) => void;
  clearRunResults: () => void;
  reset: () => void;
}

export const useWorkflowStore = create<WorkflowStore>((set, get) => ({
  // Initial state
  workflowId: null,
  workflowName: 'Untitled Workflow',
  nodes: [],
  edges: [],
  selectedNode: null,
  builtinTasks: [],
  isRunning: false,
  runResults: [],

  // Actions
  setWorkflowId: (id) => set({ workflowId: id }),
  
  setWorkflowName: (name) => set({ workflowName: name }),
  
  setNodes: (nodes) => set({ nodes }),
  
  setEdges: (edges) => set({ edges }),
  
  addNode: (node) => set((state) => ({
    nodes: [...state.nodes, node],
  })),
  
  updateNode: (nodeId, updates) => set((state) => ({
    nodes: state.nodes.map((node) =>
      node.id === nodeId
        ? { ...node, data: { ...node.data, ...updates } }
        : node
    ),
  })),
  
  deleteNode: (nodeId) => set((state) => ({
    nodes: state.nodes.filter((node) => node.id !== nodeId),
    edges: state.edges.filter(
      (edge) => edge.source !== nodeId && edge.target !== nodeId
    ),
    selectedNode: state.selectedNode?.id === nodeId ? null : state.selectedNode,
  })),
  
  selectNode: (node) => set({ selectedNode: node }),
  
  setBuiltinTasks: (tasks) => set({ builtinTasks: tasks }),
  
  setIsRunning: (isRunning) => set({ isRunning }),
  
  addRunResult: (result) => set((state) => ({
    runResults: [...state.runResults, result],
  })),
  
  clearRunResults: () => set({ runResults: [] }),
  
  reset: () => set({
    workflowId: null,
    workflowName: 'Untitled Workflow',
    nodes: [],
    edges: [],
    selectedNode: null,
    isRunning: false,
    runResults: [],
  }),
}));
