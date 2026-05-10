import axios from 'axios';
import type {
  BuiltinTaskMeta,
  WorkspaceTasksResponse,
  WorkspaceWorkflowsResponse,
  Workflow,
  WorkflowEdge,
  WorkflowNode,
} from '@/types/workflow';

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
  async runWorkflow(workflowId: string): Promise<{ 
    message: string;
    workflowId: string;
  }> {
    const response = await axios.post(`${API_BASE}/workflows/${workflowId}/run`);
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
