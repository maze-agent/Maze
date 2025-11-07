import React from 'react';

const SummaryCard = ({ title, value, subtitle }) => (
  <div className="bg-gray-800 p-6 rounded-lg border border-gray-700">
    <div className="text-gray-400 text-sm font-medium">{title}</div>
    <div className="text-3xl font-bold mt-2">{value}</div>
    {subtitle && <div className="text-sm text-gray-400 mt-1">{subtitle}</div>}
  </div>
);

const SummaryCards = ({ activeWorkers, totalWorkers, totalWorkflows, cpuUsage, memoryUsage }) => {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
      <SummaryCard
        title="Active Workers"
        value={`${activeWorkers} / ${totalWorkers}`}
      />
      <SummaryCard
        title="Total Workflows"
        value={totalWorkflows}
      />
      <SummaryCard
        title="CPU Usage"
        value={`${cpuUsage.used} / ${cpuUsage.total}`}
        subtitle={`${((cpuUsage.used / cpuUsage.total) * 100).toFixed(1)}% utilized`}
      />
      <SummaryCard
        title="Memory Usage"
        value={`${memoryUsage.used.toFixed(0)} / ${memoryUsage.total} GB`}
        subtitle={`${((memoryUsage.used / memoryUsage.total) * 100).toFixed(1)}% utilized`}
      />
    </div>
  );
};

export default SummaryCards;