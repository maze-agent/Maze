import { create } from 'zustand';
import type {
  WorkflowNode,
  WorkflowEdge,
  WorkspaceTaskMeta,
  WorkspaceWorkflowMeta,
  WorkspaceManifest,
  UnifiedRunEvent,
  UnifiedRunSnapshot,
} from '@/types/workflow';
import { clearWorkflowSource } from '@/utils/workflowBindings';

const ACTIVE_RUN_STATUSES = new Set(['created', 'queued', 'running']);

export function createLocalWorkflowId() {
  return `workflow-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export type WorkflowSaveState =
  | 'empty'
  | 'unsaved_draft'
  | 'saving_draft'
  | 'saved_draft'
  | 'saved_workflow'
  | 'error';

export type WorkflowOperationToken = symbol;

export interface WorkflowOperation {
  token: WorkflowOperationToken;
  label: string;
}

interface WorkflowStore {
  // Workflow state
  workflowId: string | null;
  workflowName: string;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  selectedNode: WorkflowNode | null;
  workflowSaveState: WorkflowSaveState;
  workflowDraftPath: string | null;
  workflowSavedAt: string | null;
  workflowDraftError: string | null;
  workflowOperation: WorkflowOperation | null;
  
  // Workspace tasks
  workspaceId: string;
  workspaceDir: string;
  workspaceManifest: WorkspaceManifest | null;
  workspaceManifestVersion: number | null;
  workspaceTasks: WorkspaceTaskMeta[];
  workspaceWorkflows: WorkspaceWorkflowMeta[];
  currentWorkspaceWorkflowPath: string | null;
  
  // Run state
  isRunning: boolean;
  activeRunId: string | null;
  selectedRunId: string | null;
  staticRuns: UnifiedRunSnapshot[];
  staticRunEvents: Record<string, UnifiedRunEvent[]>;
  
  // Actions
  setWorkflowId: (id: string) => void;
  setWorkflowName: (name: string) => void;
  setNodes: (nodes: WorkflowNode[]) => void;
  setEdges: (edges: WorkflowEdge[]) => void;
  addNode: (node: WorkflowNode) => void;
  updateNode: (nodeId: string, updates: Partial<WorkflowNode['data']>) => void;
  deleteNode: (nodeId: string) => void;
  selectNode: (node: WorkflowNode | null) => void;
  setWorkspaceContext: (workspace: {
    workspaceId?: string;
    workspaceDir?: string;
    workspaceManifestVersion?: number | null;
    manifest?: WorkspaceManifest | null;
  }) => void;
  setWorkspaceDir: (dir: string) => void;
  setWorkspaceTasks: (tasks: WorkspaceTaskMeta[]) => void;
  setWorkspaceWorkflows: (workflows: WorkspaceWorkflowMeta[]) => void;
  setCurrentWorkspaceWorkflowPath: (path: string | null) => void;
  setWorkflowSaveState: (state: {
    status: WorkflowSaveState;
    draftPath?: string | null;
    savedAt?: string | null;
    error?: string | null;
  }) => void;
  acquireWorkflowOperation: (label: string) => WorkflowOperationToken | null;
  releaseWorkflowOperation: (token: WorkflowOperationToken) => void;
  setIsRunning: (isRunning: boolean) => void;
  setSelectedRunId: (runId: string | null) => void;
  setActiveRun: (run: UnifiedRunSnapshot | null) => void;
  upsertStaticRun: (run: UnifiedRunSnapshot) => void;
  setStaticRuns: (runs: UnifiedRunSnapshot[]) => void;
  setStaticRunEvents: (runId: string, events: UnifiedRunEvent[]) => void;
  removeStaticRun: (runId: string) => void;
  reset: () => void;
}

export const useWorkflowStore = create<WorkflowStore>((set) => ({
  // Initial state
  workflowId: createLocalWorkflowId(),
  workflowName: 'Untitled Workflow',
  nodes: [],
  edges: [],
  selectedNode: null,
  workflowSaveState: 'empty',
  workflowDraftPath: null,
  workflowSavedAt: null,
  workflowDraftError: null,
  workflowOperation: null,
  workspaceId: '',
  workspaceDir: '',
  workspaceManifest: null,
  workspaceManifestVersion: null,
  workspaceTasks: [],
  workspaceWorkflows: [],
  currentWorkspaceWorkflowPath: null,
  isRunning: false,
  activeRunId: null,
  selectedRunId: null,
  staticRuns: [],
  staticRunEvents: {},

  // Actions
  setWorkflowId: (id) => set({ workflowId: id }),
  
  setWorkflowName: (name) => set((state) => ({
    workflowName: name,
    workflowSaveState: state.nodes.length > 0 ? 'unsaved_draft' : 'empty',
    workflowDraftError: null,
  })),
  
  setNodes: (nodes) => set({
    nodes,
    workflowSaveState: nodes.length > 0 ? 'unsaved_draft' : 'empty',
    workflowDraftError: null,
  }),
  
  setEdges: (edges) => set((state) => ({
    edges,
    workflowSaveState: state.nodes.length > 0 ? 'unsaved_draft' : 'empty',
    workflowDraftError: null,
  })),
  
  addNode: (node) => set((state) => ({
    nodes: [...state.nodes, node],
    workflowSaveState: 'unsaved_draft',
    workflowDraftError: null,
  })),
  
  updateNode: (nodeId, updates) => set((state) => ({
    nodes: state.nodes.map((node) =>
      node.id === nodeId
        ? { ...node, data: { ...node.data, ...updates } }
        : node
    ),
    workflowSaveState: state.nodes.length > 0 ? 'unsaved_draft' : 'empty',
    workflowDraftError: null,
  })),
  
  deleteNode: (nodeId) => set((state) => {
    const nodes = clearWorkflowSource(
      state.nodes.filter((node) => node.id !== nodeId),
      nodeId,
    );
    return {
      nodes,
      edges: state.edges.filter(
        (edge) => edge.source !== nodeId && edge.target !== nodeId
      ),
      selectedNode: state.selectedNode?.id === nodeId ? null : state.selectedNode,
      workflowSaveState: nodes.length > 0 ? 'unsaved_draft' : 'empty',
      workflowDraftError: null,
    };
  }),
  
  selectNode: (node) => set({ selectedNode: node }),
  
  setWorkspaceContext: (workspace) => set((state) => ({
    workspaceId: workspace.workspaceId ?? workspace.manifest?.workspace_id ?? state.workspaceId,
    workspaceDir: workspace.workspaceDir ?? state.workspaceDir,
    workspaceManifest: workspace.manifest === undefined ? state.workspaceManifest : workspace.manifest,
    workspaceManifestVersion: workspace.workspaceManifestVersion
      ?? workspace.manifest?.manifest_version
      ?? state.workspaceManifestVersion,
  })),

  setWorkspaceDir: (dir) => set({ workspaceDir: dir }),

  setWorkspaceTasks: (tasks) => set({ workspaceTasks: tasks }),

  setWorkspaceWorkflows: (workflows) => set({ workspaceWorkflows: workflows }),

  setCurrentWorkspaceWorkflowPath: (path) => set({ currentWorkspaceWorkflowPath: path }),

  setWorkflowSaveState: (next) => set((state) => ({
    workflowSaveState: next.status,
    workflowDraftPath: next.draftPath === undefined ? state.workflowDraftPath : next.draftPath,
    workflowSavedAt: next.savedAt === undefined ? state.workflowSavedAt : next.savedAt,
    workflowDraftError: next.error === undefined ? null : next.error,
  })),

  acquireWorkflowOperation: (label) => {
    let token: WorkflowOperationToken | null = null;
    set((state) => {
      if (state.workflowOperation) {
        return state;
      }
      token = Symbol(label);
      return { workflowOperation: { token, label } };
    });
    return token;
  },

  releaseWorkflowOperation: (token) => set((state) => (
    state.workflowOperation?.token === token
      ? { workflowOperation: null }
      : state
  )),

  setIsRunning: (isRunning) => set({ isRunning }),

  setSelectedRunId: (runId) => set({ selectedRunId: runId }),

  setActiveRun: (run) => set((state) => {
    if (!run) {
      return { activeRunId: null };
    }
    const existing = state.staticRuns.filter((item) => item.run_id !== run.run_id);
    return {
      activeRunId: run.run_id,
      selectedRunId: run.run_id,
      staticRuns: [run, ...existing],
    };
  }),

  upsertStaticRun: (run) => set((state) => ({
    staticRuns: [
      run,
      ...state.staticRuns.filter((item) => item.run_id !== run.run_id),
    ],
    isRunning: run.run_id === state.activeRunId
      ? ACTIVE_RUN_STATUSES.has(run.status)
      : state.isRunning,
  })),

  setStaticRuns: (runs) => set({ staticRuns: runs }),

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
      isRunning: removingActiveRun ? false : state.isRunning,
    };
  }),

  reset: () => set({
    workflowId: createLocalWorkflowId(),
    workflowName: 'Untitled Workflow',
    nodes: [],
    edges: [],
    selectedNode: null,
    workflowSaveState: 'empty',
    workflowDraftPath: null,
    workflowSavedAt: null,
    workflowDraftError: null,
    currentWorkspaceWorkflowPath: null,
    isRunning: false,
    activeRunId: null,
    selectedRunId: null,
  }),
}));
