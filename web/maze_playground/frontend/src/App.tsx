import { useCallback, useEffect, useRef } from 'react';
import { ConfigProvider } from 'antd';
import enUS from 'antd/locale/en_US';
import { message } from 'antd';
import Toolbar from './components/Toolbar';
import BuiltinTasksSidebar from './components/BuiltinTasksSidebar';
import WorkflowCanvas from './components/WorkflowCanvas';
import NodePanel from './components/NodePanel';
import ResultsModal from './components/ResultsModal';
import { api } from './api/client';
import { useWorkflowStore } from './stores/workflowStore';

function App() {
  const saveShortcutInFlightRef = useRef(false);
  const {
    workflowId,
    workflowName,
    workspaceDir,
    currentWorkspaceWorkflowPath,
    nodes,
    edges,
    isRunning,
    setWorkflowId,
    setWorkspaceDir,
    setWorkspaceWorkflows,
    setCurrentWorkspaceWorkflowPath,
    setNodes,
    setEdges,
  } = useWorkflowStore();

  const saveWorkflowToWorkspace = useCallback(async () => {
    if (saveShortcutInFlightRef.current) {
      return;
    }

    if (isRunning) {
      message.warning('Workflow is running, please save after it finishes');
      return;
    }

    if (nodes.length === 0) {
      message.warning('Please add at least one task node before saving');
      return;
    }

    saveShortcutInFlightRef.current = true;
    const hideLoading = message.loading('Saving workflow...', 0);

    try {
      const activeWorkspace = workspaceDir || (await api.getWorkspaceWorkflows()).workspaceDir;
      let activeWorkflowId = workflowId;

      if (!activeWorkflowId) {
        const created = await api.createWorkflow(workflowName);
        activeWorkflowId = created.workflowId;
        setWorkflowId(created.workflowId);
      }

      const saved = await api.saveWorkspaceWorkflow({
        workspaceDir: activeWorkspace,
        relativePath: currentWorkspaceWorkflowPath,
        name: workflowName,
        workflowId: activeWorkflowId,
        nodes,
        edges,
      });

      await api.saveWorkflow(activeWorkflowId, {
        name: workflowName,
        nodes: saved.workflow.nodes,
        edges: saved.workflow.edges,
      });

      const refreshed = await api.getWorkspaceWorkflows(saved.workspaceDir);
      setWorkspaceDir(saved.workspaceDir);
      setCurrentWorkspaceWorkflowPath(saved.relativePath);
      setWorkspaceWorkflows(refreshed.workflows || []);
      setNodes(saved.workflow.nodes);
      setEdges(saved.workflow.edges);
      message.success(`Workflow saved to ${saved.relativePath}`);
    } catch (error: any) {
      console.error('Failed to save workflow:', error);
      message.error(error.response?.data?.error || 'Failed to save workflow');
    } finally {
      hideLoading();
      saveShortcutInFlightRef.current = false;
    }
  }, [
    currentWorkspaceWorkflowPath,
    edges,
    isRunning,
    nodes,
    setCurrentWorkspaceWorkflowPath,
    setEdges,
    setNodes,
    setWorkflowId,
    setWorkspaceDir,
    setWorkspaceWorkflows,
    workflowId,
    workflowName,
    workspaceDir,
  ]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat) {
        return;
      }

      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault();
        saveWorkflowToWorkspace();
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [saveWorkflowToWorkspace]);

  return (
    <ConfigProvider locale={enUS}>
      <div style={{ width: '100vw', height: '100vh', display: 'flex', flexDirection: 'column' }}>
        <Toolbar />
        
        <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
          <BuiltinTasksSidebar />
          
          <div style={{ flex: 1, position: 'relative' }}>
            <WorkflowCanvas />
          </div>
          
          <NodePanel />
        </div>
        
        <ResultsModal />
      </div>
    </ConfigProvider>
  );
}

export default App;
