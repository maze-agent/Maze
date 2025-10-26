import { useState, useCallback } from 'react';
import { useRealtimeData } from './useRealtimeData';
import { mazeAPI } from '../services/api';

/**
 * Workflows 数据获取和操作 Hook
 */
export const useWorkflows = (mode = 'api', mockData = null, interval = 5000) => {
  const [localWorkflows, setLocalWorkflows] = useState(mockData || []);

  // 服务控制操作 - Mock 模式专用
  const handleMockServiceAction = useCallback(async (workflowId, action) => {
    setLocalWorkflows(prevWorkflows =>
      prevWorkflows.map(workflow => {
        if (workflow.workflow_id === workflowId) {
          let newStatus = workflow.service_status;

          switch (action) {
            case 'start':
            case 'resume':
              newStatus = 'running';
              break;
            case 'pause':
              newStatus = 'paused';
              break;
            case 'stop':
              newStatus = 'stopped';
              break;
            default:
              break;
          }

          return {
            ...workflow,
            service_status: newStatus
          };
        }
        return workflow;
      })
    );
    return { status: 'success', message: `Service ${action}ed successfully` };
  }, []);

  // Mock 模式：直接返回本地状态
  if (mode === 'mock') {
    return {
      workflows: localWorkflows,
      loading: false,
      error: null,
      refresh: () => {},
      handleServiceAction: handleMockServiceAction,
    };
  }

  // API 模式：使用 useCallback 确保函数引用稳定
  const fetchWorkflows = useCallback(async () => {
    const result = await mazeAPI.getWorkflows();
    return result;
  }, []);

  const { data, loading, error, refresh } = useRealtimeData(
    fetchWorkflows,
    interval,
    true
  );

  // 服务控制操作 - API 模式
  const handleApiServiceAction = useCallback(async (workflowId, action) => {
    const result = await mazeAPI.controlWorkflowService(workflowId, action);
    refresh();
    return result;
  }, [refresh]);

  return {
    workflows: data?.workflows || [],
    loading,
    error,
    refresh,
    handleServiceAction: handleApiServiceAction,
  };
};

export default useWorkflows;

