import React from 'react';

const TaskExecutionPanel = ({ taskExecutions }) => {
    const getStatusIcon = (status) => {
        switch (status) {
            case 'completed': return '✅';
            case 'running': return '🔄';
            case 'failed': return '❌';
            case 'cancelled': return '🚫';
            default: return '⏸️';
        }
    };

    return (
        <div className="space-y-3 max-h-[500px] overflow-y-auto">
            {taskExecutions.map(task => (
                <div
                    key={task.task_id}
                    className={`p-4 rounded-lg border-2 ${
                        task.status === 'completed' ? 'border-green-500 bg-green-900/20' :
                            task.status === 'running' ? 'border-yellow-500 bg-yellow-900/20' :
                                task.status === 'failed' ? 'border-red-500 bg-red-900/20' :
                                    'border-gray-500 bg-gray-800'
                    }`}
                >
                    <div className="flex items-center justify-between mb-2">
                        <div className="text-sm font-semibold text-white">
                            {getStatusIcon(task.status)} {task.node_name}
                        </div>
                    </div>

                    <div className="text-xs text-gray-300 space-y-1">
                        {task.worker_id && (
                            <p>🖥️ Worker: {task.worker_id}</p>
                        )}
                        {task.started_at && (
                            <p>⏰ Started: {new Date(task.started_at).toLocaleTimeString()}</p>
                        )}
                        {task.duration && (
                            <p>⏱️ Duration: {task.duration}s</p>
                        )}
                        {task.output && (
                            <details className="mt-2">
                                <summary className="cursor-pointer text-blue-400 hover:text-blue-300">
                                    View Output
                                </summary>
                                <pre className="mt-2 p-2 bg-gray-900 rounded text-[10px] overflow-x-auto">
                  {JSON.stringify(task.output, null, 2)}
                </pre>
                            </details>
                        )}
                        {task.error && (
                            <p className="text-red-400 mt-2">❌ {task.error}</p>
                        )}
                    </div>
                </div>
            ))}
        </div>
    );
};

export default TaskExecutionPanel;