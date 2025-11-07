import axios from 'axios';
import type { BuiltinTaskMeta, Workflow, WorkflowNode, RunResult } from '@/types/workflow';

const API_BASE = '/api';

export const api = {
  // 获取内置任务列表
  async getBuiltinTasks(): Promise<BuiltinTaskMeta[]> {
    const response = await axios.get(`${API_BASE}/builtin-tasks`);
    return response.data;
  },

  // 解析自定义函数
  async parseCustomFunction(code: string): Promise<{
    name: string;
    nodeType: 'task' | 'tool';
    inputs: Array<{ name: string; dataType: string }>;
    outputs: Array<{ name: string; dataType: string }>;
    resources?: any;
  }> {
    const response = await axios.post(`${API_BASE}/parse-custom-function`, { code });
    return response.data;
  },

  // 创建工作流
  async createWorkflow(name?: string): Promise<{ 
    workflowId: string; 
    name: string;
    mazeWorkflowId: string;
  }> {
    const response = await axios.post(`${API_BASE}/workflows`, { name });
    return response.data;
  },

  // 获取工作流详情
  async getWorkflow(workflowId: string): Promise<Workflow> {
    const response = await axios.get(`${API_BASE}/workflows/${workflowId}`);
    return response.data;
  },

  // 保存工作流（节点和边）
  async saveWorkflow(workflowId: string, data: {
    nodes: WorkflowNode[];
    edges: any[];
  }): Promise<void> {
    await axios.put(`${API_BASE}/workflows/${workflowId}`, data);
  },

  // 运行工作流
  async runWorkflow(workflowId: string): Promise<{ 
    message: string;
    workflowId: string;
  }> {
    const response = await axios.post(`${API_BASE}/workflows/${workflowId}/run`);
    return response.data;
  },

  // 获取工作流结果
  async getWorkflowResults(workflowId: string): Promise<{
    status: string;
    results: any;
    error?: string;
  }> {
    const response = await axios.get(`${API_BASE}/workflows/${workflowId}/results`);
    return response.data;
  },

  // 连接WebSocket获取实时结果
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
      console.log('✅ WebSocket 连接已建立');
      callbacks.onConnected?.();
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        console.log('📨 收到消息:', data);
        
        callbacks.onMessage?.(data);

        switch (data.type) {
          case 'connected':
            console.log('🔌 已连接到结果推送');
            break;
          
          case 'workflow_started':
            console.log('🚀 工作流开始运行');
            callbacks.onWorkflowStarted?.();
            break;
          
          case 'building':
            console.log('🏗️ 正在构建工作流...');
            callbacks.onBuilding?.(data.message);
            break;
          
          case 'workflow_completed':
            console.log('✅ 工作流执行完成');
            callbacks.onWorkflowCompleted?.(data.results);
            break;
          
          case 'workflow_failed':
            console.error('❌ 工作流执行失败:', data.error);
            callbacks.onWorkflowFailed?.(data.error, data.traceback);
            break;
          
          case 'workflow_running':
            console.log('⏳ 工作流运行中...');
            break;
          
          default:
            console.log('📦 未知消息类型:', data.type);
        }
      } catch (error) {
        console.error('解析 WebSocket 消息失败:', error);
      }
    };

    ws.onerror = (error) => {
      console.error('❌ WebSocket 错误:', error);
      callbacks.onError?.(error);
    };

    ws.onclose = () => {
      console.log('🔌 WebSocket 连接已关闭');
      callbacks.onClose?.();
    };

    return ws;
  },
};
