/**
 * Maze API Service
 * 封装所有后端 API 调用
 */

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';
const WS_BASE_URL = import.meta.env.VITE_WS_URL || 'ws://localhost:8000';

class MazeAPI {
  constructor() {
    this.baseUrl = API_BASE_URL;
    this.wsBaseUrl = WS_BASE_URL;
  }

  /**
   * 通用请求方法
   */
  async request(endpoint, options = {}) {
    try {
      const response = await fetch(`${this.baseUrl}${endpoint}`, {
        headers: {
          'Content-Type': 'application/json',
          ...options.headers,
        },
        ...options,
      });

      if (!response.ok) {
        throw new Error(`API Error: ${response.status} ${response.statusText}`);
      }

      const data = await response.json();
      
      // 检查业务状态
      if (data.status === 'fail') {
        throw new Error(data.message || 'Request failed');
      }

      return data;
    } catch (error) {
      console.error(`API Request Failed [${endpoint}]:`, error);
      throw error;
    }
  }

  // ==================== Worker APIs ====================

  /**
   * 获取所有 worker 信息
   */
  async getWorkers() {
    return this.request('/api/workers');
  }

  /**
   * 获取单个 worker 详细信息
   */
  async getWorkerDetail(workerId) {
    return this.request(`/api/workers/${workerId}`);
  }

  // ==================== Workflow APIs ====================

  /**
   * 获取所有 workflow 列表
   */
  async getWorkflows() {
    return this.request('/api/workflows');
  }

  /**
   * 获取单个 workflow 详情（包括 DAG 结构）
   */
  async getWorkflowDetail(workflowId) {
    return this.request(`/api/workflows/${workflowId}`);
  }

  /**
   * 控制 workflow 服务
   * @param {string} workflowId 
   * @param {string} action - start/pause/resume/stop
   */
  async controlWorkflowService(workflowId, action) {
    return this.request(`/api/workflows/${workflowId}/${action}`, {
      method: 'POST',
    });
  }

  // ==================== Run APIs ====================

  /**
   * 获取 workflow 的运行历史
   */
  async getWorkflowRuns(workflowId, params = {}) {
    const query = new URLSearchParams(params);
    const queryString = query.toString();
    return this.request(
      `/api/workflows/${workflowId}/runs${queryString ? `?${queryString}` : ''}`
    );
  }

  /**
   * 获取单次运行详情
   */
  async getRunDetail(runId) {
    return this.request(`/api/runs/${runId}`);
  }

  // ==================== Dashboard APIs ====================

  /**
   * 获取 Dashboard 概览数据
   */
  async getDashboardSummary() {
    return this.request('/api/dashboard/summary');
  }

  // ==================== WebSocket APIs ====================

  /**
   * 创建 WebSocket 连接用于实时监控
   */
  createMonitoringWebSocket(onMessage, onError) {
    const ws = new WebSocket(`${this.wsBaseUrl}/api/monitoring/stream`);

    ws.onopen = () => {
      console.log('WebSocket connected');
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        onMessage(data);
      } catch (error) {
        console.error('WebSocket message parse error:', error);
      }
    };

    ws.onerror = (error) => {
      console.error('WebSocket error:', error);
      if (onError) onError(error);
    };

    ws.onclose = () => {
      console.log('WebSocket disconnected');
    };

    return ws;
  }

  /**
   * 为特定 workflow 创建执行结果 WebSocket（复用现有接口）
   */
  createWorkflowResultWebSocket(workflowId, onMessage, onError) {
    const ws = new WebSocket(`${this.wsBaseUrl}/get_workflow_res/${workflowId}`);

    ws.onopen = () => {
      console.log(`Workflow ${workflowId} WebSocket connected`);
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        onMessage(data);
      } catch (error) {
        console.error('WebSocket message parse error:', error);
      }
    };

    ws.onerror = (error) => {
      console.error('WebSocket error:', error);
      if (onError) onError(error);
    };

    ws.onclose = () => {
      console.log(`Workflow ${workflowId} WebSocket disconnected`);
    };

    return ws;
  }
}

// 导出单例
export const mazeAPI = new MazeAPI();

// 导出类供测试使用
export default MazeAPI;

