import axios from 'axios';
import type {
  BuiltinTaskMeta,
  WorkspaceTasksResponse,
  WorkspaceFilesResponse,
  WorkspaceWorkflowsResponse,
  DynamicRunEvent,
  DynamicRunSnapshot,
  ClusterResourcesResponse,
  StaticWorkflowRunEvent,
  StaticWorkflowRunSnapshot,
  Workflow,
  WorkflowEdge,
  WorkflowNode,
} from '@/types/workflow';
import type { LlmSettings } from '@/utils/llmSettings';

const API_BASE = '/api';

export const api = {
  // Get builtin tasks list
  async getBuiltinTasks(): Promise<BuiltinTaskMeta[]> {
    const response = await axios.get(`${API_BASE}/builtin-tasks`);
    return response.data;
  },

  // Get workspace tasks from <workspaceDir>/tasks
  async getWorkspaceTasks(workspaceDir?: string): Promise<WorkspaceTasksResponse> {
    const response = await axios.get(`${API_BASE}/workspace-tasks`, {
      params: workspaceDir ? { workspaceDir } : undefined,
    });
    return response.data;
  },

  // Save a workspace task file
  async saveWorkspaceTask(data: {
    workspaceDir: string;
    relativePath: string;
    code: string;
    parse?: boolean;
  }): Promise<any> {
    const response = await axios.post(`${API_BASE}/workspace-tasks`, data);
    return response.data;
  },

  // Delete a workspace task file
  async deleteWorkspaceTask(data: {
    workspaceDir: string;
    relativePath: string;
  }): Promise<any> {
    const response = await axios.delete(`${API_BASE}/workspace-tasks`, { data });
    return response.data;
  },

  // Rename a workspace task function
  async renameWorkspaceTask(data: {
    workspaceDir: string;
    relativePath: string;
    oldFunctionName: string;
    newName: string;
  }): Promise<any> {
    const response = await axios.patch(`${API_BASE}/workspace-tasks/rename`, data);
    return response.data;
  },

  async getWorkspaceFiles(params?: {
    workspaceDir?: string;
    path?: string;
  }): Promise<WorkspaceFilesResponse> {
    const response = await axios.get(`${API_BASE}/workspace-files`, { params });
    return response.data;
  },

  async uploadWorkspaceFile(data: {
    workspaceDir: string;
    relativePath: string;
    contentBase64: string;
  }): Promise<any> {
    const response = await axios.post(`${API_BASE}/workspace-files/upload`, data);
    return response.data;
  },

  async testLlmConnection(settings: LlmSettings): Promise<{
    success: boolean;
    model: string;
    content?: string;
  }> {
    const response = await axios.post(`${API_BASE}/llm/test`, settings);
    return response.data;
  },

  async generateWorkspaceTask(data: LlmSettings & {
    description: string;
    taskName?: string;
    relativePath?: string;
    taskContext?: Array<{
      nodeId?: string;
      label?: string;
      category?: string;
      functionName?: string;
      taskRef?: string;
      relativePath?: string;
      description?: string;
      inputs?: any[];
      outputs?: any[];
      codePreview?: string;
    }>;
  }): Promise<{
    success: boolean;
    model: string;
    functionName: string;
    relativePath: string;
    code: string;
    notes?: string;
    rawContent?: string;
    warnings?: string[];
  }> {
    const response = await axios.post(`${API_BASE}/llm/generate-task`, data);
    return response.data;
  },

  async createWorkspaceFolder(data: {
    workspaceDir: string;
    relativePath: string;
  }): Promise<any> {
    const response = await axios.post(`${API_BASE}/workspace-files/mkdir`, data);
    return response.data;
  },

  async deleteWorkspaceFile(data: {
    workspaceDir: string;
    relativePath: string;
  }): Promise<any> {
    const response = await axios.delete(`${API_BASE}/workspace-files`, { data });
    return response.data;
  },

  async previewWorkspaceFile(
    workspaceDir: string,
    path: string,
  ): Promise<{ success: boolean; workspaceDir: string; relativePath: string; content: string }> {
    const response = await axios.get(`${API_BASE}/workspace-files/preview`, {
      params: { workspaceDir, path },
    });
    return response.data;
  },

  getWorkspaceFileDownloadUrl(workspaceDir: string, path: string): string {
    const params = new URLSearchParams({ workspaceDir, path });
    return `${API_BASE}/workspace-files/download?${params.toString()}`;
  },

  // Get saved workflows from <workspaceDir>/workflows
  async getWorkspaceWorkflows(workspaceDir?: string): Promise<WorkspaceWorkflowsResponse> {
    const response = await axios.get(`${API_BASE}/workspace-workflows`, {
      params: workspaceDir ? { workspaceDir } : undefined,
    });
    return response.data;
  },

  // Save current workflow into <workspaceDir>/workflows
  async saveWorkspaceWorkflow(data: {
    workspaceDir: string;
    relativePath?: string | null;
    name: string;
    workflowId?: string | null;
    nodes: WorkflowNode[];
    edges: WorkflowEdge[];
  }): Promise<{
    success: boolean;
    workspaceDir: string;
    relativePath: string;
    workflow: {
      name: string;
      nodes: WorkflowNode[];
      edges: WorkflowEdge[];
    };
  }> {
    const response = await axios.post(`${API_BASE}/workspace-workflows/save`, data);
    return response.data;
  },

  // Load a saved workspace workflow file
  async loadWorkspaceWorkflow(data: {
    workspaceDir: string;
    relativePath: string;
  }): Promise<{
    success: boolean;
    workspaceDir: string;
    relativePath: string;
    workflow: {
      name: string;
      nodes: WorkflowNode[];
      edges: WorkflowEdge[];
    };
    importedTaskDefinitions?: {
      imported: Array<{ relativePath: string }>;
      skipped: Array<{ relativePath: string; reason: string }>;
      remapped?: Array<{ from: string; to: string; reason: string }>;
    };
  }> {
    const response = await axios.post(`${API_BASE}/workspace-workflows/load`, data);
    return response.data;
  },

  // Import an external workflow payload and materialize its task definitions into workspace tasks
  async importWorkspaceWorkflow(data: {
    workspaceDir?: string;
    payload: any;
  }): Promise<{
    success: boolean;
    workspaceDir: string;
    workflow: {
      name: string;
      nodes: WorkflowNode[];
      edges: WorkflowEdge[];
    };
    importedTaskDefinitions?: {
      imported: Array<{ relativePath: string }>;
      skipped: Array<{ relativePath: string; reason: string }>;
      remapped?: Array<{ from: string; to: string; reason: string }>;
    };
  }> {
    const response = await axios.post(`${API_BASE}/workspace-workflows/import`, data);
    return response.data;
  },

  // Delete a saved workspace workflow file
  async deleteWorkspaceWorkflow(data: {
    workspaceDir: string;
    relativePath: string;
  }): Promise<any> {
    const response = await axios.delete(`${API_BASE}/workspace-workflows`, { data });
    return response.data;
  },

  // Rename a saved workspace workflow
  async renameWorkspaceWorkflow(data: {
    workspaceDir: string;
    relativePath: string;
    name: string;
  }): Promise<any> {
    const response = await axios.patch(`${API_BASE}/workspace-workflows/rename`, data);
    return response.data;
  },

  // Parse custom function
  async parseCustomFunction(code: string): Promise<{
    name: string;
    inputs: Array<{ name: string; dataType: string }>;
    outputs: Array<{ name: string; dataType: string }>;
    resources?: any;
  }> {
    const response = await axios.post(`${API_BASE}/parse-custom-function`, { code });
    return response.data;
  },

  // Create workflow
  async createWorkflow(name?: string): Promise<{
    workflowId: string;
    name: string;
    mazeWorkflowId: string;
  }> {
    const response = await axios.post(`${API_BASE}/workflows`, { name });
    return response.data;
  },

  // Get workflow details
  async getWorkflow(workflowId: string): Promise<Workflow> {
    const response = await axios.get(`${API_BASE}/workflows/${workflowId}`);
    return response.data;
  },

  // Save workflow (nodes and edges)
  async saveWorkflow(workflowId: string, data: {
    name?: string;
    nodes: WorkflowNode[];
    edges: any[];
  }): Promise<void> {
    await axios.put(`${API_BASE}/workflows/${workflowId}`, data);
  },

  // Run workflow
  async runWorkflow(workflowId: string, workspaceDir?: string): Promise<{
    message: string;
    workflowId: string;
    runId: string;
    run: StaticWorkflowRunSnapshot;
  }> {
    const response = await axios.post(`${API_BASE}/workflows/${workflowId}/run`, { workspaceDir });
    return response.data;
  },

  // Get workflow results
  async getWorkflowResults(workflowId: string): Promise<{
    status: string;
    results: any;
    error?: string;
  }> {
    const response = await axios.get(`${API_BASE}/workflows/${workflowId}/results`);
    return response.data;
  },

  async getDynamicRuns(params?: {
    status?: string;
    limit?: number;
  }): Promise<{ success: boolean; runs: DynamicRunSnapshot[] }> {
    const response = await axios.get(`${API_BASE}/dynamic-runs`, { params });
    return response.data;
  },

  async getDynamicRun(runId: string): Promise<{ success: boolean; run: DynamicRunSnapshot }> {
    const response = await axios.get(`${API_BASE}/dynamic-runs/${encodeURIComponent(runId)}`);
    return response.data;
  },

  async getDynamicRunEvents(
    runId: string,
    after?: number,
  ): Promise<{ success: boolean; runId: string; events: DynamicRunEvent[] }> {
    const response = await axios.get(`${API_BASE}/dynamic-runs/${encodeURIComponent(runId)}/events`, {
      params: after !== undefined ? { after } : undefined,
    });
    return response.data;
  },

  async deleteDynamicRun(runId: string): Promise<{ success: boolean; runId: string; deleted: boolean }> {
    const response = await axios.delete(`${API_BASE}/dynamic-runs/${encodeURIComponent(runId)}`);
    return response.data;
  },

  async cleanupDynamicRuns(data: {
    statuses?: string[];
    older_than_days?: number;
    dry_run?: boolean;
  }): Promise<{ success: boolean; cleanup: any }> {
    const response = await axios.post(`${API_BASE}/dynamic-runs/cleanup`, data);
    return response.data;
  },

  async getClusterResources(): Promise<ClusterResourcesResponse> {
    const response = await axios.get(`${API_BASE}/cluster/resources`);
    return response.data;
  },

  async startReactRun(data: {
    mode: 'local' | 'online';
    prompt: string;
    workspaceDir?: string;
    maxSteps?: number;
    maxTokens?: number;
    timeoutSeconds?: number;
    taskTimeout?: number;
    llm?: LlmSettings;
  }): Promise<{
    success: boolean;
    runId: string;
    answer?: any;
    status: string;
    mode?: string;
    eventTypes?: string[];
  }> {
    const response = await axios.post(`${API_BASE}/react-runs/start`, data);
    return response.data;
  },

  async getStaticWorkflowRuns(params?: {
    workspaceDir?: string;
    status?: string;
    limit?: number;
  }): Promise<{ success: boolean; workspaceDir: string; runs: StaticWorkflowRunSnapshot[] }> {
    const response = await axios.get(`${API_BASE}/workflow-runs/static`, { params });
    return response.data;
  },

  async getStaticWorkflowRun(
    runId: string,
    workspaceDir?: string,
  ): Promise<{ success: boolean; workspaceDir: string; run: StaticWorkflowRunSnapshot }> {
    const response = await axios.get(`${API_BASE}/workflow-runs/static/${encodeURIComponent(runId)}`, {
      params: workspaceDir ? { workspaceDir } : undefined,
    });
    return response.data;
  },

  async getStaticWorkflowRunEvents(
    runId: string,
    workspaceDir?: string,
    after?: number,
  ): Promise<{ success: boolean; workspaceDir: string; runId: string; events: StaticWorkflowRunEvent[] }> {
    const response = await axios.get(`${API_BASE}/workflow-runs/static/${encodeURIComponent(runId)}/events`, {
      params: {
        ...(workspaceDir ? { workspaceDir } : {}),
        ...(after !== undefined ? { after } : {}),
      },
    });
    return response.data;
  },

  async deleteStaticWorkflowRun(
    runId: string,
    workspaceDir?: string,
  ): Promise<{ success: boolean; workspaceDir: string; runId: string; deleted: boolean }> {
    const response = await axios.delete(`${API_BASE}/workflow-runs/static/${encodeURIComponent(runId)}`, {
      data: workspaceDir ? { workspaceDir } : undefined,
    });
    return response.data;
  },

  getStaticRunArtifactDownloadUrl(
    runId: string,
    taskId: string,
    path: string,
    workspaceDir?: string,
  ): string {
    const params = new URLSearchParams({
      taskId,
      path,
      ...(workspaceDir ? { workspaceDir } : {}),
    });
    return `${API_BASE}/workflow-runs/static/${encodeURIComponent(runId)}/artifacts/download?${params.toString()}`;
  },

  // Connect WebSocket for real-time results
  connectWebSocket(
    workflowId: string,
    callbacks: {
      onConnected?: () => void;
      onMessage?: (data: any) => void;
      onWorkflowStarted?: () => void;
      onBuilding?: (message: string) => void;
      onWorkflowCompleted?: (results: any) => void;
      onWorkflowFailed?: (error: string, traceback?: string) => void;
      onTaskUpdate?: (event: any) => void;
      onRunUpdate?: (payload: any) => void;
      onError?: (error: Event) => void;
      onClose?: () => void;
    }
  ): WebSocket {
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const hostname = window.location.hostname || 'localhost';
    const wsUrl = `${protocol}://${hostname}:3001/ws/workflows/${workflowId}/results`;
    const ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      callbacks.onConnected?.();
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);

        callbacks.onMessage?.(data);

        switch (data.type) {
          case 'connected':
            break;

          case 'workflow_started':
            callbacks.onWorkflowStarted?.();
            break;

          case 'building':
            callbacks.onBuilding?.(data.message);
            break;

          case 'workflow_completed':
            callbacks.onWorkflowCompleted?.(data.results);
            break;

          case 'workflow_failed':
            callbacks.onWorkflowFailed?.(data.error, data.traceback);
            break;

          case 'task_update':
            callbacks.onTaskUpdate?.(data.event);
            break;

          case 'run_update':
            callbacks.onRunUpdate?.(data);
            callbacks.onTaskUpdate?.(data.event);
            break;

          case 'workflow_running':
            break;

          default:
            break;
        }
      } catch (error) {
        console.error('Failed to parse WebSocket message:', error);
      }
    };

    ws.onerror = (error) => {
      console.error('WebSocket error:', error);
      callbacks.onError?.(error);
    };

    ws.onclose = () => {
      callbacks.onClose?.();
    };

    return ws;
  },
};
