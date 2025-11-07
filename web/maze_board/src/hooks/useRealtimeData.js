import { useState, useEffect, useCallback } from 'react';

/**
 * 通用的实时数据获取 Hook
 * 支持轮询刷新
 */
export const useRealtimeData = (fetchFunction, interval = 5000, enabled = true) => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchData = useCallback(async () => {
    try {
      setError(null);
      const result = await fetchFunction();
      setData(result);
    } catch (err) {
      console.error('Fetch data error:', err);
      setError(err.message || 'Failed to fetch data');
    } finally {
      setLoading(false);
    }
  }, [fetchFunction]);

  // 手动刷新方法
  const refresh = useCallback(() => {
    setLoading(true);
    fetchData();
  }, [fetchData]);

  useEffect(() => {
    if (!enabled) return;

    // 初始加载
    fetchData();

    // 设置轮询
    if (interval && interval > 0) {
      const intervalId = setInterval(fetchData, interval);
      return () => clearInterval(intervalId);
    }
  }, [fetchData, interval, enabled]);

  return { data, loading, error, refresh };
};

export default useRealtimeData;

