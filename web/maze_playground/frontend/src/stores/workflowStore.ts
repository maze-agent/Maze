import { create } from 'zustand';
import type {
  WorkflowNode,
  WorkflowEdge,
  BuiltinTaskMeta,
  WorkspaceTaskMeta,
  WorkspaceWorkflowMeta,
  WorkspaceManifest,
  LocalWorkspaceFileMeta,
  RunResult,
  StaticWorkflowRunEvent,
  StaticWorkflowRunSnapshot,
} from '@/types/workflow';

export type WorkflowSaveState =
  | 'empty'
  | 'unsaved_draft'
  | 'saving_draft'
  | 'saved_draft'
  | 'saved_workflow'
  | 'error';

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
  
  // Builtin tasks
  builtinTasks: BuiltinTaskMeta[];

  // Workspace tasks
  workspaceId: string;
  workspaceDir: string;
  workspaceManifest: WorkspaceManifest | null;
  workspaceManifestVersion: number | null;
  workspaceTasks: WorkspaceTaskMeta[];
  workspaceWorkflows: WorkspaceWorkflowMeta[];
  currentWorkspaceWorkflowPath: string | null;
  localWorkspaceId: string;
  localWorkspaceName: string;
  localWorkspaceHandle: any | null;
  localWorkspaceFiles: LocalWorkspaceFileMeta[];
  localWorkspaceVersion: string;
  localWorkspaceLastSyncedAt: string | null;
  
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
  setLocalWorkspace: (workspace: {
    id: string;
    name: string;
    handle: any | null;
    files: LocalWorkspaceFileMeta[];
    version: string;
    syncedAt?: string | null;
  }) => void;
  setLocalWorkspaceFiles: (files: LocalWorkspaceFileMeta[], version: string, syncedAt?: string | null) => void;
  clearLocalWorkspace: () => void;
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
  workflowSaveState: 'empty',
  workflowDraftPath: null,
  workflowSavedAt: null,
  workflowDraftError: null,
  builtinTasks: [],
  workspaceId: '',
  workspaceDir: '',
  workspaceManifest: null,
  workspaceManifestVersion: null,
  workspaceTasks: [],
  workspaceWorkflows: [],
  currentWorkspaceWorkflowPath: null,
  localWorkspaceId: '',
  localWorkspaceName: '',
  localWorkspaceHandle: null,
  localWorkspaceFiles: [],
  localWorkspaceVersion: '',
  localWorkspaceLastSyncedAt: null,
  isRunning: false,
  runResults: [],
  activeRunId: null,
  selectedRunId: null,
  runViewerOpen: false,
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
    const nodes = state.nodes.filter((node) => node.id !== nodeId);
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
  
  setBuiltinTasks: (tasks) => set({ builtinTasks: tasks }),

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

  setLocalWorkspace: (workspace) => set({
    localWorkspaceId: workspace.id,
    localWorkspaceName: workspace.name,
    localWorkspaceHandle: workspace.handle,
    localWorkspaceFiles: workspace.files,
    localWorkspaceVersion: workspace.version,
    localWorkspaceLastSyncedAt: workspace.syncedAt || new Date().toISOString(),
  }),

  setLocalWorkspaceFiles: (files, version, syncedAt) => set({
    localWorkspaceFiles: files,
    localWorkspaceVersion: version,
    localWorkspaceLastSyncedAt: syncedAt || new Date().toISOString(),
  }),

  clearLocalWorkspace: () => set({
    localWorkspaceId: '',
    localWorkspaceName: '',
    localWorkspaceHandle: null,
    localWorkspaceFiles: [],
    localWorkspaceVersion: '',
    localWorkspaceLastSyncedAt: null,
  }),
  
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
    workflowSaveState: 'empty',
    workflowDraftPath: null,
    workflowSavedAt: null,
    workflowDraftError: null,
    currentWorkspaceWorkflowPath: null,
    isRunning: false,
    runResults: [],
    activeRunId: null,
    selectedRunId: null,
    runViewerOpen: false,
  }),
}));
