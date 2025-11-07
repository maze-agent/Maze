import { useCallback } from 'react';
import { useRealtimeData } from './useRealtimeData';
import { mazeAPI } from '../services/api';

/**
 * Workers 数据获取 Hook
 */
export const useWorkers = (mode = 'api', mockData = null, interval = 5000) => {
  // Mock 模式：直接返回静态数据，不使用 useRealtimeData
  if (mode === 'mock') {
    return {
      workers: mockData || [],
      loading: false,
      error: null,
      refresh: () => {},
    };
  }

  // API 模式：使用 useCallback 确保函数引用稳定
  const fetchWorkers = useCallback(async () => {
    const result = await mazeAPI.getWorkers();
    return result;
  }, []);

  const { data, loading, error, refresh } = useRealtimeData(
    fetchWorkers,
    interval,
    true
  );

  return {
    workers: data?.workers || [],
    loading,
    error,
    refresh,
  };
};

export default useWorkers;

