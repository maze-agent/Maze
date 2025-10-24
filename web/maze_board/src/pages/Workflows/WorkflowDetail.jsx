import React, { useState } from 'react';
import WorkflowGraph from './components/WorkflowGraph';
import RunList from './components/RunList';
import ApiDocumentation from './components/ApiDocumentation';

const WorkflowDetail = ({ workflow, workflowDetails, runs, onRunClick, onBack }) => {
  const [activeTab, setActiveTab] = useState('structure'); // structure, runs, api

  return (
    <div className="space-y-6">
      {/* 返回按钮 */}
      <button
        onClick={onBack}
        className="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg transition"
      >
        ← Back to Workflows
      </button>

      {/* 工作流信息 */}
      <div className="bg-gray-800 p-6 rounded-lg border border-gray-700">
        <div className="flex items-start justify-between">
          <div>
            <h2 className="text-2xl font-bold mb-2">{workflow.workflow_name}</h2>
            <div className="text-sm text-gray-400 space-y-1">
              <p>Workflow ID: {workflow.workflow_id}</p>
              <p>Created: {new Date(workflow.created_at).toLocaleString()}</p>
              <p>Total Requests: {workflow.total_requests}</p>
            </div>
          </div>
          <div>
            {workflow.service_status === 'running' && (
              <span className="px-3 py-1 rounded-full text-sm border bg-green-500/20 text-green-400 border-green-500/50">
                 Service Running
              </span>
            )}
            {workflow.service_status === 'paused' && (
              <span className="px-3 py-1 rounded-full text-sm border bg-yellow-500/20 text-yellow-400 border-yellow-500/50">
                ⏸️ Service Paused
              </span>
            )}
            {workflow.service_status === 'stopped' && (
              <span className="px-3 py-1 rounded-full text-sm border bg-gray-500/20 text-gray-400 border-gray-500/50">
                ⏹️ Service Stopped
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Tab 导航 */}
      <div className="border-b border-gray-700">
        <div className="flex gap-4">
          <button
            onClick={() => setActiveTab('structure')}
            className={`px-4 py-2 font-medium transition ${
              activeTab === 'structure'
                ? 'text-blue-400 border-b-2 border-blue-400'
                : 'text-gray-400 hover:text-gray-300'
            }`}
          >
            Workflow Structure
          </button>
          <button
            onClick={() => setActiveTab('runs')}
            className={`px-4 py-2 font-medium transition ${
              activeTab === 'runs'
                ? 'text-blue-400 border-b-2 border-blue-400'
                : 'text-gray-400 hover:text-gray-300'
            }`}
          >
            Run History
          </button>
          <button
            onClick={() => setActiveTab('api')}
            className={`px-4 py-2 font-medium transition ${
              activeTab === 'api'
                ? 'text-blue-400 border-b-2 border-blue-400'
                : 'text-gray-400 hover:text-gray-300'
            }`}
          >
            API Documentation
          </button>
        </div>
      </div>

      {/* Tab 内容 - 添加滚动支持 */}
      <div className="min-h-[500px] max-h-[calc(100vh-400px)] overflow-y-auto">
        {activeTab === 'structure' && (
          <div className="bg-gray-800 p-6 rounded-lg border border-gray-700">
            <h3 className="text-xl font-semibold mb-4">Workflow Structure</h3>
            <p className="text-sm text-gray-400 mb-4">
              This is the static workflow definition showing the DAG structure.
            </p>
            <WorkflowGraph
              nodes={workflowDetails?.nodes || []}
              edges={workflowDetails?.edges || []}
              isStatic={true}
            />
          </div>
        )}

        {activeTab === 'runs' && (
          <div className="bg-gray-800 p-6 rounded-lg border border-gray-700">
            <h3 className="text-xl font-semibold mb-4">Run History</h3>
            <p className="text-sm text-gray-400 mb-4">
              History of all execution requests processed by this service.
            </p>
            <RunList runs={runs} onRunClick={onRunClick} />
          </div>
        )}

        {activeTab === 'api' && (
          <ApiDocumentation
            workflowId={workflow.workflow_id}
            apiConfig={workflowDetails?.api_config}
          />
        )}
      </div>
    </div>
  );
};

export default WorkflowDetail;