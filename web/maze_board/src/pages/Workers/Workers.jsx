import React from 'react';
import WorkerCard from './WorkerCard';
import { useWorkers } from '../../hooks';
import { useDataMode } from '../../contexts/DataContext';
import { DataStatusWrapper } from '../../components/common';

const Workers = ({ mockWorkers }) => {
  const { dataMode, refreshInterval } = useDataMode();
  const { workers, loading, error, refresh } = useWorkers(
    dataMode,
    mockWorkers,
    refreshInterval
  );

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h2 className="text-2xl font-bold">Workers</h2>
        <button
          onClick={refresh}
          className="px-4 py-2 bg-blue-600 hover:bg-blue-700 rounded-lg transition text-sm"
          disabled={loading}
        >
          {loading ? '⟳ Refreshing...' : '🔄 Refresh'}
        </button>
      </div>
      
      <DataStatusWrapper
        loading={loading}
        error={error}
        data={workers}
        emptyMessage="No workers available"
        onRetry={refresh}
      >
        <div className="grid grid-cols-1 gap-4">
          {workers.map(worker => (
            <WorkerCard key={worker.worker_id} worker={worker} />
          ))}
        </div>
      </DataStatusWrapper>
    </div>
  );
};

export default Workers;