import React from 'react';
import { PieChart, Pie, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer, Cell } from 'recharts';
import SummaryCards from './SummaryCards';
import { useWorkers, useWorkflows } from '../../hooks';
import { useDataMode } from '../../contexts/DataContext';
import { DataStatusWrapper } from '../../components/common';

const Dashboard = ({ mockWorkers, mockWorkflows }) => {
  const { dataMode, refreshInterval } = useDataMode();
  
  const { workers, loading: workersLoading, error: workersError } = useWorkers(
    dataMode,
    mockWorkers,
    refreshInterval
  );

  const { workflows, loading: workflowsLoading, error: workflowsError } = useWorkflows(
    dataMode,
    mockWorkflows,
    refreshInterval
  );

  const loading = workersLoading || workflowsLoading;
  const error = workersError || workflowsError;
  // 计算统计数据
  const activeWorkers = workers.filter(w => w.status === 'active').length;
  const totalCPU = workers.reduce((sum, w) => sum + w.cpu_total, 0);
  const usedCPU = workers.reduce((sum, w) => sum + w.cpu_used, 0);
  const totalMemory = workers.reduce((sum, w) => sum + w.memory_total_gb, 0);
  const usedMemory = workers.reduce((sum, w) => sum + w.memory_used_gb, 0);

  // 状态分布数据
  const statusData = [
    { name: 'Active', value: workers.filter(w => w.status === 'active').length, fill: '#10b981' },
    { name: 'Idle', value: workers.filter(w => w.status === 'idle').length, fill: '#f59e0b' },
    { name: 'Offline', value: workers.filter(w => w.status === 'offline').length, fill: '#ef4444' },
  ];

  // 资源使用数据
  const resourceData = workers.map(w => ({
    name: w.worker_id,
    'CPU Used': w.cpu_used,
    'Memory (GB)': w.memory_used_gb,
  }));

  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-bold">Dashboard Overview</h2>

      <DataStatusWrapper
        loading={loading}
        error={error}
        data={workers.length > 0 || workflows.length > 0}
        emptyMessage="No data available"
      >
        {/* 摘要卡片 */}
        <SummaryCards
          activeWorkers={activeWorkers}
          totalWorkers={workers.length}
          totalWorkflows={workflows.length}
          cpuUsage={{ used: usedCPU, total: totalCPU }}
          memoryUsage={{ used: usedMemory, total: totalMemory }}
        />

        {/* 图表区域 */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mt-6">
          {/* 状态分布饼图 */}
          <div className="bg-gray-800 p-6 rounded-lg border border-gray-700">
            <h3 className="text-xl font-semibold mb-4">Worker Status Distribution</h3>
            <ResponsiveContainer width="100%" height={250}>
              <PieChart>
                <Pie
                  data={statusData}
                  dataKey="value"
                  nameKey="name"
                  cx="50%"
                  cy="50%"
                  outerRadius={80}
                  label
                >
                  {statusData.map((entry, index) => (
                    <Cell key={`cell-${index}`} fill={entry.fill} />
                  ))}
                </Pie>
                <Tooltip />
                <Legend />
              </PieChart>
            </ResponsiveContainer>
          </div>

          {/* 资源使用柱状图 */}
          <div className="bg-gray-800 p-6 rounded-lg border border-gray-700">
            <h3 className="text-xl font-semibold mb-4">Resource Usage by Worker</h3>
            <ResponsiveContainer width="100%" height={250}>
              <BarChart data={resourceData}>
                <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                <XAxis dataKey="name" stroke="#9ca3af" />
                <YAxis stroke="#9ca3af" />
                <Tooltip contentStyle={{ backgroundColor: '#1f2937', border: '1px solid #374151' }} />
                <Legend />
                <Bar dataKey="CPU Used" fill="#3b82f6" />
                <Bar dataKey="Memory (GB)" fill="#10b981" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      </DataStatusWrapper>
    </div>
  );
};

export default Dashboard;