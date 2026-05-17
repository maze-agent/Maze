import { create } from 'zustand';
import type {
  WorkflowNode,
  WorkflowEdge,
  BuiltinTaskMeta,
  WorkspaceTaskMeta,
  WorkspaceWorkflowMeta,
  RunResult,
  StaticWorkflowRunEvent,
  StaticWorkflowRunSnapshot,
} from '@/types/workflow';

interface WorkflowStore {
  // Workflow state
  workflowId: string | null;
  workflowName: string;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  selectedNode: WorkflowNode | null;
  
  // Builtin tasks
  builtinTasks: BuiltinTaskMeta[];

  // Workspace tasks
  workspaceDir: string;
  workspaceTasks: WorkspaceTaskMeta[];
  workspaceWorkflows: WorkspaceWorkflowMeta[];
  currentWorkspaceWorkflowPath: string | null;
  
  // Run state
  isRunning: boolean;
  runResults: RunResult[];
  activeRunId: string | null;
  selectedRunId: string | null;
  runViewerOpen: boolean;
  staticRuns: StaticWorkflowRunSnapshot[];
  staticRunEvents: Record<string, StaticWorkflowRunEvent[]>;
  
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
  setWorkspaceDir: (dir: string) => void;
  setWorkspaceTasks: (tasks: WorkspaceTaskMeta[]) => void;
  setWorkspaceWorkflows: (workflows: WorkspaceWorkflowMeta[]) => void;
  setCurrentWorkspaceWorkflowPath: (path: string | null) => void;
  setIsRunning: (isRunning: boolean) => void;
  addRunResult: (result: RunResult) => void;
  clearRunResults: () => void;
  setActiveRun: (run: StaticWorkflowRunSnapshot | null) => void;
  upsertStaticRun: (run: StaticWorkflowRunSnapshot) => void;
  setStaticRuns: (runs: StaticWorkflowRunSnapshot[]) => void;
  addStaticRunEvent: (runId: string, event: StaticWorkflowRunEvent) => void;
  setStaticRunEvents: (runId: string, events: StaticWorkflowRunEvent[]) => void;
  removeStaticRun: (runId: string) => void;
  openRunViewer: (runId: string) => void;
  closeRunViewer: () => void;
  reset: () => void;
}

export const useWorkflowStore = create<WorkflowStore>((set) => ({
  // Initial state
  workflowId: null,
  workflowName: 'Untitled Workflow',
  nodes: [],
  edges: [],
  selectedNode: null,
  builtinTasks: [],
  workspaceDir: '',
  workspaceTasks: [],
  workspaceWorkflows: [],
  currentWorkspaceWorkflowPath: null,
  isRunning: false,
  runResults: [],
  activeRunId: null,
  selectedRunId: null,
  runViewerOpen: false,
  staticRuns: [],
  staticRunEvents: {},

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

  setWorkspaceDir: (dir) => set({ workspaceDir: dir }),

  setWorkspaceTasks: (tasks) => set({ workspaceTasks: tasks }),

  setWorkspaceWorkflows: (workflows) => set({ workspaceWorkflows: workflows }),

  setCurrentWorkspaceWorkflowPath: (path) => set({ currentWorkspaceWorkflowPath: path }),
  
  setIsRunning: (isRunning) => set({ isRunning }),
  
  addRunResult: (result) => set((state) => ({
    runResults: [...state.runResults, result],
  })),
  
  clearRunResults: () => set({ runResults: [] }),

  setActiveRun: (run) => set((state) => {
    if (!run) {
      return { activeRunId: null };
    }
    const existing = state.staticRuns.filter((item) => item.run_id !== run.run_id);
    return {
      activeRunId: run.run_id,
      selectedRunId: run.run_id,
      runViewerOpen: true,
      staticRuns: [run, ...existing],
    };
  }),

  upsertStaticRun: (run) => set((state) => ({
    staticRuns: [
      run,
      ...state.staticRuns.filter((item) => item.run_id !== run.run_id),
    ],
    isRunning: run.run_id === state.activeRunId
      ? run.status === 'running'
      : state.isRunning,
  })),

  setStaticRuns: (runs) => set({ staticRuns: runs }),

  addStaticRunEvent: (runId, event) => set((state) => ({
    staticRunEvents: {
      ...state.staticRunEvents,
      [runId]: [...(state.staticRunEvents[runId] || []), event],
    },
  })),

  setStaticRunEvents: (runId, events) => set((state) => ({
    staticRunEvents: {
      ...state.staticRunEvents,
      [runId]: events,
    },
  })),

  removeStaticRun: (runId) => set((state) => {
    const remainingEvents = { ...state.staticRunEvents };
    delete remainingEvents[runId];
    const removingActiveRun = state.activeRunId === runId;
    const removingSelectedRun = state.selectedRunId === runId;

    return {
      staticRuns: state.staticRuns.filter((run) => run.run_id !== runId),
      staticRunEvents: remainingEvents,
      activeRunId: removingActiveRun ? null : state.activeRunId,
      selectedRunId: removingSelectedRun ? null : state.selectedRunId,
      runViewerOpen: removingSelectedRun ? false : state.runViewerOpen,
      isRunning: removingActiveRun ? false : state.isRunning,
    };
  }),

  openRunViewer: (runId) => set({ selectedRunId: runId, runViewerOpen: true }),

  closeRunViewer: () => set({ runViewerOpen: false }),
  
  reset: () => set({
    workflowId: null,
    workflowName: 'Untitled Workflow',
    nodes: [],
    edges: [],
    selectedNode: null,
    currentWorkspaceWorkflowPath: null,
    isRunning: false,
    runResults: [],
    activeRunId: null,
    selectedRunId: null,
    runViewerOpen: false,
  }),
}));
