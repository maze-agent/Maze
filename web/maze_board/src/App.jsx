import React, { useState } from 'react';
import MainLayout from './layouts/MainLayout';
import Dashboard from './pages/Dashboard/Dashboard';
import Workers from './pages/Workers/Workers';
import Workflows from './pages/Workflows/Workflows';
import WorkflowDetail from './pages/Workflows/WorkflowDetail';
import WorkflowRunDetail from './pages/Workflows/WorkflowRunDetail';
import { mockAPI, workflowRuns, runTaskExecutions } from './utils/mockData';

const App = () => {
  const [currentTab, setCurrentTab] = useState('dashboard');
  const [selectedWorkflow, setSelectedWorkflow] = useState(null);
  const [selectedRun, setSelectedRun] = useState(null);
  
  // 本地状态管理 workflows（用于模拟服务状态变化）
  const [workflows, setWorkflows] = useState(mockAPI.workflows);

  const handleWorkflowClick = (workflowId) => {
    setSelectedWorkflow(workflowId);
    setSelectedRun(null);
    setCurrentTab('workflow-detail');
  };

  const handleRunClick = (runId) => {
    setSelectedRun(runId);
    setCurrentTab('run-detail');
  };

  const handleBackToWorkflows = () => {
    setSelectedWorkflow(null);
    setSelectedRun(null);
    setCurrentTab('workflows');
  };

  const handleBackToWorkflowDetail = () => {
    setSelectedRun(null);
    setCurrentTab('workflow-detail');
  };

  // 处理服务操作（启动、暂停、恢复、停止）
  const handleServiceAction = (workflowId, action) => {
    console.log(`Service action: ${action} for workflow ${workflowId}`);
    
    // 更新工作流状态
    setWorkflows(prevWorkflows => 
      prevWorkflows.map(workflow => {
        if (workflow.workflow_id === workflowId) {
          let newStatus = workflow.service_status;
          
          switch (action) {
            case 'start':
            case 'resume':
              newStatus = 'running';
              break;
            case 'pause':
              newStatus = 'paused';
              break;
            case 'stop':
              newStatus = 'stopped';
              break;
            default:
              break;
          }
          
          return {
            ...workflow,
            service_status: newStatus
          };
        }
        return workflow;
      })
    );

    // 显示操作提示
    const actionMessages = {
      start: 'Service started successfully',
      pause: 'Service paused',
      resume: 'Service resumed',
      stop: 'Service stopped'
    };
    
    // 可以使用 toast 通知，这里暂时用 alert
    alert(`${actionMessages[action]} for workflow: ${workflowId}`);
    
    // 在实际应用中，这里应该调用后端 API
    // try {
    //   await fetch(`/api/workflows/${workflowId}/${action}`, { method: 'POST' });
    // } catch (error) {
    //   console.error('Service action failed:', error);
    // }
  };

  const renderPage = () => {
    switch (currentTab) {
      case 'dashboard':
        return <Dashboard workers={mockAPI.workers} workflows={workflows} />;

      case 'workers':
        return <Workers workers={mockAPI.workers} />;

      case 'workflows':
        return (
          <Workflows 
            workflows={workflows} 
            onWorkflowClick={handleWorkflowClick}
            onServiceAction={handleServiceAction}
          />
        );

      case 'workflow-detail':
        if (!selectedWorkflow) {
          return (
            <Workflows 
              workflows={workflows} 
              onWorkflowClick={handleWorkflowClick}
              onServiceAction={handleServiceAction}
            />
          );
        }
        const workflow = workflows.find(w => w.workflow_id === selectedWorkflow);
        const runs = workflowRuns[selectedWorkflow] || [];
        return (
          <WorkflowDetail
            workflow={workflow}
            workflowDetails={mockAPI.workflowDetails[selectedWorkflow]}
            runs={runs}
            onRunClick={handleRunClick}
            onBack={handleBackToWorkflows}
          />
        );

      case 'run-detail':
        if (!selectedWorkflow || !selectedRun) return null;
        const run = workflowRuns[selectedWorkflow]?.find(r => r.run_id === selectedRun);
        const taskExecutions = runTaskExecutions[selectedRun] || [];
        return (
          <WorkflowRunDetail
            run={run}
            workflowDetails={mockAPI.workflowDetails[selectedWorkflow]}
            taskExecutions={taskExecutions}
            onBack={handleBackToWorkflowDetail}
          />
        );

      default:
        return <Dashboard workers={mockAPI.workers} workflows={workflows} />;
    }
  };

  return (
    <MainLayout currentTab={currentTab} onTabChange={setCurrentTab}>
      {renderPage()}
    </MainLayout>
  );
};

export default App;