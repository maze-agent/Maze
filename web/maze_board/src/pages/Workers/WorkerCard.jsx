import React from 'react';
import { STATUS_COLORS } from '../../utils/constants';

const ResourceBar = ({ label, used, total, color }) => (
  <div>
    <div className="text-sm text-gray-400">{label}</div>
    <div className="text-lg font-semibold">{used} / {total}</div>
    <div className="w-full bg-gray-700 rounded-full h-2 mt-1">
      <div 
        className={`${color} h-2 rounded-full`} 
        style={{ width: `${total > 0 ? (used / total) * 100 : 0}%` }}
      />
    </div>
  </div>
);

const WorkerCard = ({ worker }) => {
  return (
    <div className="bg-gray-800 p-6 rounded-lg border border-gray-700">
      <div className="flex justify-between items-start mb-4">
        <div>
          <h3 className="text-xl font-semibold">{worker.worker_id}</h3>
          <p className="text-sm text-gray-400">{worker.hostname}</p>
        </div>
        <span className={`px-3 py-1 rounded-full text-sm font-medium ${STATUS_COLORS[worker.status]}`}>
          {worker.status}
        </span>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
        <ResourceBar
          label="CPU"
          used={worker.cpu_used}
          total={worker.cpu_total}
          color="bg-blue-500"
        />
        <ResourceBar
          label="Memory"
          used={worker.memory_used_gb}
          total={worker.memory_total_gb}
          color="bg-green-500"
        />
        <ResourceBar
          label="GPU"
          used={worker.gpu_used}
          total={worker.gpu_total}
          color="bg-purple-500"
        />
      </div>
    </div>
  );
};

export default WorkerCard;