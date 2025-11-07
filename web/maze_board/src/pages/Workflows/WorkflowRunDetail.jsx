import React from 'react';
import WorkflowGraph from './components/WorkflowGraph';
import TaskExecutionPanel from './components/TaskExecutionPanel';

const WorkflowRunDetail = ({ mockRun, mockWorkflowDetails, mockTaskExecutions, onBack }) => {
    // 使用 mock 数据（后续可以扩展为支持 API 模式）
    const run = mockRun;
    const workflowDetails = mockWorkflowDetails;
    const taskExecutions = mockTaskExecutions;

    if (!run) {
        return (
            <div className="space-y-6">
                <div className="text-center text-gray-400 py-8">Run not found</div>
            </div>
        );
    }

    return (
        <div className="space-y-6">
            {/* 返回按钮 */}
            <button
                onClick={onBack}
                className="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg transition"
            >
                ← Back to Workflow
            </button>

            {/* Run 信息 */}
            <div className="bg-gray-800 p-6 rounded-lg border border-gray-700">
                <div className="flex justify-between items-start">
                    <div>
                        <h2 className="text-2xl font-bold mb-2">Run #{run.run_id.slice(-4)}</h2>
                        <div className="text-sm text-gray-400 space-y-1">
                            <p>Started: {new Date(run.started_at).toLocaleString()}</p>
                            {run.completed_at && (
                                <p>Completed: {new Date(run.completed_at).toLocaleString()}</p>
                            )}
                            {run.duration && <p>Duration: {run.duration}s</p>}
                        </div>
                    </div>
                    <div className={`px-4 py-2 rounded-lg font-semibold ${
                        run.status === 'completed' ? 'bg-green-600 text-white' :
                            run.status === 'running' ? 'bg-yellow-600 text-gray-900' :
                                run.status === 'failed' ? 'bg-red-600 text-white' :
                                    'bg-gray-600 text-white'
                    }`}>
                        {run.status.toUpperCase()}
                    </div>
                </div>
            </div>

            {/* 主内容区域 */}
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
                {/* 左侧：动态 DAG 图 */}
                <div className="lg:col-span-2 bg-gray-800 p-6 rounded-lg border border-gray-700">
                    <h3 className="text-xl font-semibold mb-4">Execution Graph</h3>
                    <WorkflowGraph
                        nodes={workflowDetails?.nodes || []}
                        edges={workflowDetails?.edges || []}
                        isStatic={false}
                        taskExecutions={taskExecutions}
                    />
                </div>

                {/* 右侧：任务执行详情 */}
                <div className="bg-gray-800 p-6 rounded-lg border border-gray-700">
                    <h3 className="text-xl font-semibold mb-4">Task Executions</h3>
                    <TaskExecutionPanel taskExecutions={taskExecutions} />
                </div>
            </div>
        </div>
    );
};

export default WorkflowRunDetail;