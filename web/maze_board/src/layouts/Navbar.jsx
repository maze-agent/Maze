import React from 'react';
import { useDataMode } from '../contexts/DataContext';

const Navbar = ({ currentTab, onTabChange }) => {
  const { dataMode, toggleDataMode, isMockMode, isApiMode } = useDataMode();

  const tabs = [
    { id: 'dashboard', label: '📊 Dashboard' },
    { id: 'workers', label: '🖥️ Workers' },
    { id: 'workflows', label: '⚙️ Workflows' }
  ];

  return (
    <div className="bg-gray-800 border-b border-gray-700 px-6 py-4">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-3xl font-bold">Maze Board - Workflow Monitor</h1>
        
        {/* 数据模式切换开关 */}
        <div className="flex items-center gap-3">
          <span className="text-sm text-gray-400">Data Mode:</span>
          <button
            onClick={toggleDataMode}
            className={`relative inline-flex items-center h-8 rounded-full w-16 transition-colors focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500 ${
              isApiMode ? 'bg-green-600' : 'bg-yellow-600'
            }`}
            title={`Click to switch to ${isMockMode ? 'API' : 'Mock'} mode`}
          >
            <span
              className={`inline-block w-6 h-6 transform transition-transform bg-white rounded-full ${
                isApiMode ? 'translate-x-9' : 'translate-x-1'
              }`}
            />
          </button>
          <span className={`text-sm font-medium ${
            isApiMode ? 'text-green-400' : 'text-yellow-400'
          }`}>
            {isMockMode ? '🎭 Mock' : '🔌 API'}
          </span>
        </div>
      </div>
      
      <div className="flex gap-4">
        {tabs.map(tab => (
          <button
            key={tab.id}
            onClick={() => onTabChange(tab.id)}
            className={`px-6 py-2 rounded-lg font-medium transition ${
              currentTab === tab.id || 
              (tab.id === 'workflows' && currentTab === 'workflow-detail')
                ? 'bg-blue-600' 
                : 'bg-gray-700 hover:bg-gray-600'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>
    </div>
  );
};

export default Navbar;