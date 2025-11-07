import React from 'react';
import { useWorkflows } from '../../hooks';
import { useDataMode } from '../../contexts/DataContext';
import { DataStatusWrapper } from '../../components/common';

const Workflows = ({ mockWorkflows, onWorkflowClick, onMockServiceAction }) => {
  const { dataMode, refreshInterval } = useDataMode();
  const { workflows, loading, error, refresh, handleServiceAction } = useWorkflows(
    dataMode,
    mockWorkflows,
    refreshInterval
  );
  const getStatusBadge = (status) => {
    const statusStyles = {
      running: 'bg-green-500/20 text-green-400 border-green-500/50',
      paused: 'bg-yellow-500/20 text-yellow-400 border-yellow-500/50',
      stopped: 'bg-gray-500/20 text-gray-400 border-gray-500/50'
    };
    
    const statusLabels = {
      running: 'Running',
      paused: 'Paused',
      stopped: 'Stopped'
    };
    
    return (
      <span className={`px-2 py-1 rounded-full text-xs border ${statusStyles[status]}`}>
        {statusLabels[status]}
      </span>
    );
  };

  const handleAction = async (e, workflowId, action) => {
    e.stopPropagation(); // 防止触发 view details
    
    try {
      // 根据模式使用不同的处理函数
      if (dataMode === 'mock' && onMockServiceAction) {
        onMockServiceAction(workflowId, action);
      } else {
        await handleServiceAction(workflowId, action);
        const actionMessages = {
          start: 'Service started successfully',
          pause: 'Service paused',
          resume: 'Service resumed',
          stop: 'Service stopped'
        };
        alert(`${actionMessages[action]} for workflow: ${workflowId}`);
      }
    } catch (err) {
      alert(`Failed to ${action} workflow: ${err.message}`);
    }
  };

  const getActionButtons = (workflow) => {
    const baseButtonClass = "px-3 py-1 rounded-md transition text-xs";
    
    return (
      <div className="flex gap-2">
        {workflow.service_status === 'running' && (
          <button
            onClick={(e) => handleAction(e, workflow.workflow_id, 'pause')}
            className={`${baseButtonClass} bg-yellow-600 hover:bg-yellow-700`}
          >
            Pause
          </button>
        )}
        
        {workflow.service_status === 'paused' && (
          <>
            <button
              onClick={(e) => handleAction(e, workflow.workflow_id, 'resume')}
              className={`${baseButtonClass} bg-green-600 hover:bg-green-700`}
            >
              Resume
            </button>
            <button
              onClick={(e) => handleAction(e, workflow.workflow_id, 'stop')}
              className={`${baseButtonClass} bg-red-600 hover:bg-red-700`}
            >
              Stop
            </button>
          </>
        )}
        
        {workflow.service_status === 'stopped' && (
          <button
            onClick={(e) => handleAction(e, workflow.workflow_id, 'start')}
            className={`${baseButtonClass} bg-green-600 hover:bg-green-700`}
          >
            Start
          </button>
        )}
        
        <button
          onClick={() => onWorkflowClick && onWorkflowClick(workflow.workflow_id)}
          className={`${baseButtonClass} bg-blue-600 hover:bg-blue-700`}
        >
          View Details
        </button>
      </div>
    );
  };

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h2 className="text-2xl font-bold">Workflows - Agent as a Service</h2>
        <div className="flex items-center gap-4">
          <div className="text-sm text-gray-400">
            Each workflow is an agent service
          </div>
          <button
            onClick={refresh}
            className="px-4 py-2 bg-blue-600 hover:bg-blue-700 rounded-lg transition text-sm"
            disabled={loading}
          >
            {loading ? '⟳ Refreshing...' : '🔄 Refresh'}
          </button>
        </div>
      </div>
      
      <DataStatusWrapper
        loading={loading}
        error={error}
        data={workflows}
        emptyMessage="No workflows available"
        onRetry={refresh}
      >
        <div className="bg-gray-800 rounded-lg border border-gray-700 overflow-hidden">
          <table className="min-w-full divide-y divide-gray-700">
            <thead className="bg-gray-900">
              <tr>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider">
                  Service Name
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider">
                  Status
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider">
                  Created
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider">
                  Total Requests
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-400 uppercase tracking-wider">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-700">
              {workflows.map(workflow => (
                <tr key={workflow.workflow_id} className="hover:bg-gray-700">
                  <td className="px-6 py-4 whitespace-nowrap">
                    <div className="text-sm font-medium">{workflow.workflow_name}</div>
                    <div className="text-xs text-gray-400">ID: {workflow.workflow_id}</div>
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap">
                    {getStatusBadge(workflow.service_status)}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-400">
                    {new Date(workflow.created_at).toLocaleString()}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-sm">
                    <div className="flex items-center gap-2">
                      <span className="text-lg font-semibold text-blue-400">
                        {workflow.total_requests}
                      </span>
                      <span className="text-xs text-gray-400">requests</span>
                    </div>
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-sm">
                    {getActionButtons(workflow)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </DataStatusWrapper>
    </div>
  );
};

export default Workflows;