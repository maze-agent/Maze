import axios from 'axios';
import type {
  WorkspaceTasksResponse,
  WorkspaceContextResponse,
  WorkspaceFilesResponse,
  WorkspaceWorkflowsResponse,
  SystemCatalogResponse,
  ClusterQueuesResponse,
  ClusterConsoleRunResponse,
  ClusterResourcesResponse,
  ModelTestResponse,
  ModelsResponse,
  WorkerProfile,
  WorkerProfileActionResponse,
  WorkerProfileDraftTestResponse,
  WorkerProfilesResponse,
  RunLogLine,
  UnifiedRunEvent,
  UnifiedRunSnapshot,
  WorkflowEdge,
  WorkflowNode,
} from '@/types/workflow';
import type { LlmSettings } from '@/utils/llmSettings';

const API_BASE = '/api';

export const api = {
  async createWorkspace(data?: {
    workspaceId?: string;
    name?: string;
    mode?: string;
  }): Promise<WorkspaceContextResponse> {
    const response = await axios.post(`${API_BASE}/workspaces`, data || {});
    return response.data;
  },

  async getWorkspace(workspaceId: string): Promise<WorkspaceContextResponse> {
    const response = await axios.get(`${API_BASE}/workspaces/${encodeURIComponent(workspaceId)}`);
    return response.data;
  },

  async getSystemCatalog(type?: 'workflows' | 'tasks'): Promise<SystemCatalogResponse> {
    const response = await axios.get(`${API_BASE}/system-catalog`, {
      params: type ? { type } : undefined,
    });
    return response.data;
  },

  async importSystemCatalogItem(data: {
    workspaceId?: string;
    workspaceDir?: string;
    type: 'workflows' | 'tasks';
    sourceId: string;
    targetPath?: string;
  }): Promise<any> {
    const response = await axios.post(`${API_BASE}/system-catalog/import`, data);
    return response.data;
  },

  async loadSystemWorkflow(data: {
    workspaceId?: string;
    workspaceDir?: string;
    sourceId: string;
  }): Promise<{
    success: boolean;
    workspaceId?: string;
    workspaceDir: string;
    workspaceManifestVersion?: number;
    sourceId: string;
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
    const response = await axios.post(`${API_BASE}/system-catalog/workflows/load`, data);
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
    workspaceId?: string;
    workspaceDir: string;
    relativePath: string;
    code: string;
    parse?: boolean;
  }): Promise<any> {
    const response = await axios.post(`${API_BASE}/workspace-tasks`, data);
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
    workspaceId?: string;
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

  async promoteArtifactToWorkspaceFile(data: {
    workspaceId?: string;
    workspaceDir?: string;
    artifact: any;
    targetPath?: string;
    runId?: string;
    taskId?: string;
    overwrite?: boolean;
  }): Promise<any> {
    const response = await axios.post(`${API_BASE}/artifacts/promote`, data);
    return response.data;
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
    workspaceId?: string;
    workspaceDir: string;
    relativePath?: string | null;
    name: string;
    workflowId?: string | null;
    nodes: WorkflowNode[];
    edges: WorkflowEdge[];
  }): Promise<{
    success: boolean;
    workspaceId?: string;
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
    workspaceId?: string;
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
    workspaceId?: string;
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

  // Run workflow
  async runWorkflow(workflowId: string, data: {
    workflow: {
      name: string;
      nodes: WorkflowNode[];
      edges: WorkflowEdge[];
    };
    relativePath: string;
    workspaceId?: string;
    workspaceDir: string;
  }): Promise<{
    workflowId: string;
    runId: string;
    coreWorkflowId: string;
    submissionId: string;
    workspaceId?: string;
    workspaceDir?: string;
  }> {
    const response = await axios.post(`${API_BASE}/workflows/${workflowId}/run`, data);
    return {
      workflowId: response.data.workflowId,
      runId: response.data.runId,
      coreWorkflowId: response.data.coreWorkflowId,
      submissionId: response.data.submissionId,
      workspaceId: response.data.workspaceId,
      workspaceDir: response.data.workspaceDir,
    };
  },

  async deleteDynamicRun(runId: string): Promise<{ success: boolean; runId: string; deleted: boolean }> {
    const response = await axios.delete(`${API_BASE}/dynamic-runs/${encodeURIComponent(runId)}`);
    return response.data;
  },

  async getRuns(params?: {
    status?: string;
    kind?: string;
    limit?: number;
    detail?: boolean;
  }): Promise<{ success: boolean; runs: UnifiedRunSnapshot[] }> {
    const response = await axios.get(`${API_BASE}/runs`, { params });
    return response.data;
  },

  async getRun(runId: string): Promise<{ success: boolean; run: UnifiedRunSnapshot }> {
    const response = await axios.get(`${API_BASE}/runs/${encodeURIComponent(runId)}`);
    return response.data;
  },

  async getRunEvents(
    runId: string,
    after?: number,
  ): Promise<{ success: boolean; runId: string; events: UnifiedRunEvent[] }> {
    const response = await axios.get(`${API_BASE}/runs/${encodeURIComponent(runId)}/events`, {
      params: after !== undefined ? { after } : undefined,
    });
    return response.data;
  },

  async getRunLogs(
    runId: string,
    params?: { tail?: number; taskId?: string },
  ): Promise<{ success: boolean; runId: string; taskId?: string | null; lineCount: number; lines: RunLogLine[] }> {
    const response = await axios.get(`${API_BASE}/runs/${encodeURIComponent(runId)}/logs`, { params });
    return response.data;
  },

  async getRunArtifacts(runId: string): Promise<{
    success: boolean;
    runId: string;
    artifacts: any[];
  }> {
    const response = await axios.get(`${API_BASE}/runs/${encodeURIComponent(runId)}/artifacts`);
    return response.data;
  },

  async getRunTaskArtifacts(runId: string, taskId: string): Promise<{
    success: boolean;
    runId: string;
    taskId: string;
    artifacts: any[];
  }> {
    const response = await axios.get(
      `${API_BASE}/runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(taskId)}/artifacts`
    );
    return response.data;
  },

  getArtifactDownloadUrl(sha256: string): string {
    return `${API_BASE}/artifacts/sha256/${encodeURIComponent(sha256)}`;
  },

  async cancelRun(runId: string, reason?: string): Promise<{
    success: boolean;
    runId: string;
    status: string;
  }> {
    const response = await axios.post(`${API_BASE}/runs/${encodeURIComponent(runId)}/cancel`, { reason });
    return response.data;
  },

  async retryRun(runId: string, data?: {
    workspaceDir?: string;
    artifactMode?: boolean;
    timeoutSeconds?: number;
    tags?: string[];
  }): Promise<{
    success: boolean;
    runId: string;
    workflowId: string;
    retriedFromRunId: string;
    spec?: any;
  }> {
    const response = await axios.post(`${API_BASE}/runs/${encodeURIComponent(runId)}/retry`, {
      workspace_dir: data?.workspaceDir,
      artifact_mode: data?.artifactMode,
      timeout_seconds: data?.timeoutSeconds,
      tags: data?.tags,
    });
    return response.data;
  },

  async getClusterResources(): Promise<ClusterResourcesResponse> {
    const response = await axios.get(`${API_BASE}/cluster/resources`);
    return response.data;
  },

  async getClusterQueues(): Promise<ClusterQueuesResponse> {
    const response = await axios.get(`${API_BASE}/cluster/queues`);
    return response.data;
  },

  async getModels(): Promise<ModelsResponse> {
    const response = await axios.get(`${API_BASE}/models`);
    return response.data;
  },

  async updateModelConfig(modelDir: string): Promise<ModelsResponse> {
    const response = await axios.post(`${API_BASE}/models/config`, { model_dir: modelDir });
    return response.data;
  },

  async testModel(modelId: string): Promise<ModelTestResponse> {
    const response = await axios.post(`${API_BASE}/models/test`, { model_id: modelId });
    return response.data;
  },

  async setClusterNodeDisabled(
    nodeId: string,
    disabled: boolean,
  ): Promise<{ status: string; node_id: string; disabled: boolean; cluster?: ClusterResourcesResponse['cluster'] }> {
    const action = disabled ? 'disable' : 'enable';
    const response = await axios.post(`${API_BASE}/cluster/nodes/${encodeURIComponent(nodeId)}/${action}`);
    return response.data;
  },

  async listWorkerProfiles(params?: {
    workspaceId?: string;
    workspaceDir?: string;
  }): Promise<WorkerProfilesResponse> {
    const response = await axios.get(`${API_BASE}/cluster/worker-profiles`, { params });
    return response.data;
  },

  async saveWorkerProfile(data: {
    workspaceId?: string;
    workspaceDir?: string;
    profile: Partial<WorkerProfile> & {
      password?: string;
      auth?: WorkerProfile['auth'] & { password?: string };
    };
    password?: string;
  }): Promise<{ success: boolean; workspaceId?: string; workspaceDir?: string; profile: WorkerProfile }> {
    const response = await axios.post(`${API_BASE}/cluster/worker-profiles`, data);
    return response.data;
  },

  async testWorkerProfileDraft(data: {
    workspaceId?: string;
    workspaceDir?: string;
    profile: Partial<WorkerProfile> & {
      password?: string;
      auth?: WorkerProfile['auth'] & { password?: string };
    };
    password?: string;
    timeoutMs?: number;
  }): Promise<WorkerProfileDraftTestResponse> {
    const response = await axios.post(`${API_BASE}/cluster/worker-profiles/test-draft`, data);
    return response.data;
  },

  async deleteWorkerProfile(
    profileId: string,
    params?: { workspaceId?: string; workspaceDir?: string },
  ): Promise<{ success: boolean; profileId: string }> {
    const response = await axios.delete(`${API_BASE}/cluster/worker-profiles/${encodeURIComponent(profileId)}`, { params });
    return response.data;
  },

  async runWorkerProfileAction(
    profileId: string,
    action: 'test' | 'start' | 'restart' | 'stop' | 'logs',
    data?: {
      workspaceId?: string;
      workspaceDir?: string;
      password?: string;
      timeoutMs?: number;
    },
  ): Promise<WorkerProfileActionResponse> {
    const response = await axios.post(
      `${API_BASE}/cluster/worker-profiles/${encodeURIComponent(profileId)}/${action}`,
      data || {},
    );
    return response.data;
  },

  async runWorkerProfilesBulkAction(data: {
    workspaceId?: string;
    workspaceDir?: string;
    action: 'test' | 'start' | 'restart' | 'stop' | 'logs';
    profileIds: string[];
    passwordByProfileId?: Record<string, string>;
    timeoutMs?: number;
  }): Promise<WorkerProfileActionResponse> {
    const response = await axios.post(`${API_BASE}/cluster/worker-profiles/bulk`, data);
    return response.data;
  },

  async runClusterConsoleCommand(data: {
    workspaceId?: string;
    workspaceDir?: string;
    target: string;
    command: string;
    password?: string;
    timeoutMs?: number;
  }): Promise<ClusterConsoleRunResponse> {
    const response = await axios.post(`${API_BASE}/cluster/console/run`, data);
    return response.data;
  },

};
