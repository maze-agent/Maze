import axios from 'axios';
import type { BuiltinTaskMeta, Workflow, WorkflowNode, RunResult } from '@/types/workflow';

const API_BASE = '/api';

export const api = {
  // Get builtin tasks list
  async getBuiltinTasks(): Promise<BuiltinTaskMeta[]> {
    const response = await axios.get(`${API_BASE}/builtin-tasks`);
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
      onError?: (error: Event) => void;
      onClose?: () => void;
    }
  ): WebSocket {
    const wsUrl = `ws://localhost:3001/ws/workflows/${workflowId}/results`;
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
