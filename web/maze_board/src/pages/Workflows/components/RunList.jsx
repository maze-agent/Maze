import React from 'react';

const RunList = ({ runs, onRunClick }) => {
    const getStatusColor = (status) => {
        switch (status) {
            case 'completed': return 'bg-green-900 text-green-300 border-green-500';
            case 'running': return 'bg-yellow-900 text-yellow-300 border-yellow-500';
            case 'failed': return 'bg-red-900 text-red-300 border-red-500';
            default: return 'bg-gray-700 text-gray-300 border-gray-500';
        }
    };

    return (
        <div className="space-y-3 max-h-[500px] overflow-y-auto">
            {runs.length === 0 ? (
                <p className="text-gray-400 text-sm">No runs yet</p>
            ) : (
                runs.map(run => (
                    <div
                        key={run.run_id}
                        onClick={() => onRunClick(run.run_id)}
                        className={`p-4 rounded-lg border-2 cursor-pointer transition hover:shadow-lg ${getStatusColor(run.status)}`}
                    >
                        <div className="flex justify-between items-start mb-2">
                            <div className="text-sm font-semibold">
                                Run #{run.run_id.slice(-4)}
                            </div>
                            <div className={`text-xs px-2 py-1 rounded ${getStatusColor(run.status)}`}>
                                {run.status}
                            </div>
                        </div>

                        <div className="text-xs space-y-1">
                            <p>Started: {new Date(run.started_at).toLocaleTimeString()}</p>
                            {run.completed_at && (
                                <p>Duration: {run.duration}s</p>
                            )}
                            <p>Tasks: {run.completed_tasks}/{run.total_tasks}</p>
                            {run.error && (
                                <p className="text-red-400 mt-2">Error: {run.error}</p>
                            )}
                        </div>
                    </div>
                ))
            )}
        </div>
    );
};

export default RunList;