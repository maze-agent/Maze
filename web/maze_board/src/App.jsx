import React, { useState } from 'react';
import MainLayout from './layouts/MainLayout';
import Dashboard from './pages/Dashboard/Dashboard';
import Workers from './pages/Workers/Workers';
import Workflows from './pages/Workflows/Workflows';
import WorkflowDetail from './pages/Workflows/WorkflowDetail';
import WorkflowRunDetail from './pages/Workflows/WorkflowRunDetail';
import { mockAPI, workflowRuns, runTaskExecutions } from './utils/mockData';
import { DataProvider } from './contexts/DataContext';

const AppContent = () => {
  const [currentTab, setCurrentTab] = useState('dashboard');
  const [selectedWorkflow, setSelectedWorkflow] = useState(null);
  const [selectedRun, setSelectedRun] = useState(null);
  
  // 本地状态管理 workflows（用于 mock 模式的服务状态变化）
  const [mockWorkflows, setMockWorkflows] = useState(mockAPI.workflows);

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

  // 处理服务操作（启动、暂停、恢复、停止）- Mock 模式使用
  const handleMockServiceAction = (workflowId, action) => {
    console.log(`[Mock Mode] Service action: ${action} for workflow ${workflowId}`);
    
    // 更新工作流状态
    setMockWorkflows(prevWorkflows => 
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
    
    alert(`[Mock] ${actionMessages[action]} for workflow: ${workflowId}`);
  };

  const renderPage = () => {
    switch (currentTab) {
      case 'dashboard':
        return (
          <Dashboard 
            mockWorkers={mockAPI.workers}
            mockWorkflows={mockWorkflows}
          />
        );

      case 'workers':
        return <Workers mockWorkers={mockAPI.workers} />;

      case 'workflows':
        return (
          <Workflows 
            mockWorkflows={mockWorkflows}
            onWorkflowClick={handleWorkflowClick}
            onMockServiceAction={handleMockServiceAction}
          />
        );

      case 'workflow-detail':
        if (!selectedWorkflow) {
          return (
            <Workflows 
              mockWorkflows={mockWorkflows}
              onWorkflowClick={handleWorkflowClick}
              onMockServiceAction={handleMockServiceAction}
            />
          );
        }
        const workflow = mockWorkflows.find(w => w.workflow_id === selectedWorkflow);
        const runs = workflowRuns[selectedWorkflow] || [];
        return (
          <WorkflowDetail
            mockWorkflow={workflow}
            mockWorkflowDetails={mockAPI.workflowDetails[selectedWorkflow]}
            mockRuns={runs}
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
            mockRun={run}
            mockWorkflowDetails={mockAPI.workflowDetails[selectedWorkflow]}
            mockTaskExecutions={taskExecutions}
            onBack={handleBackToWorkflowDetail}
          />
        );

      default:
        return (
          <Dashboard 
            mockWorkers={mockAPI.workers}
            mockWorkflows={mockWorkflows}
          />
        );
    }
  };

  return (
    <MainLayout currentTab={currentTab} onTabChange={setCurrentTab}>
      {renderPage()}
    </MainLayout>
  );
};

const App = () => {
  return (
    <DataProvider>
      <AppContent />
    </DataProvider>
  );
};

export default App;