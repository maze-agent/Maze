import React, { createContext, useContext, useState, useEffect } from 'react';

/**
 * 数据模式上下文
 * 管理 mock/api 模式切换
 */
const DataContext = createContext();

export const useDataMode = () => {
  const context = useContext(DataContext);
  if (!context) {
    throw new Error('useDataMode must be used within DataProvider');
  }
  return context;
};

export const DataProvider = ({ children }) => {
  // 从 localStorage 读取保存的模式，默认为 mock
  const [dataMode, setDataMode] = useState(() => {
    const saved = localStorage.getItem('maze_data_mode');
    return saved || import.meta.env.VITE_DATA_MODE || 'mock';
  });

  // 刷新间隔（毫秒）
  const refreshInterval = parseInt(
    import.meta.env.VITE_REFRESH_INTERVAL || '5000',
    10
  );

  // 模式切换
  const toggleDataMode = () => {
    setDataMode((prev) => {
      const newMode = prev === 'mock' ? 'api' : 'mock';
      localStorage.setItem('maze_data_mode', newMode);
      return newMode;
    });
  };

  // 设置特定模式
  const setMode = (mode) => {
    if (mode === 'mock' || mode === 'api') {
      setDataMode(mode);
      localStorage.setItem('maze_data_mode', mode);
    }
  };

  useEffect(() => {
    console.log(`Data mode: ${dataMode}`);
  }, [dataMode]);

  const value = {
    dataMode,
    toggleDataMode,
    setMode,
    refreshInterval,
    isMockMode: dataMode === 'mock',
    isApiMode: dataMode === 'api',
  };

  return <DataContext.Provider value={value}>{children}</DataContext.Provider>;
};

export default DataContext;

