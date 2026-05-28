import { useCallback, useEffect, useRef, useState } from 'react';
import { ConfigProvider } from 'antd';
import enUS from 'antd/locale/en_US';
import { message } from 'antd';
import Toolbar from './components/Toolbar';
import BuiltinTasksSidebar from './components/BuiltinTasksSidebar';
import WorkflowCanvas from './components/WorkflowCanvas';
import NodePanel from './components/NodePanel';
import ResultsModal from './components/ResultsModal';
import RunsInspector from './components/RunsInspector';
import ReActRunModal from './components/ReActRunModal';
import ClusterResourcesDrawer from './components/ClusterResourcesDrawer';
import { api } from './api/client';
import { useWorkflowStore } from './stores/workflowStore';

function App() {
  const saveShortcutInFlightRef = useRef(false);
  const [runsOpen, setRunsOpen] = useState(false);
  const [dynamicRunFocusId, setDynamicRunFocusId] = useState<string | null>(null);
  const [activeDynamicRunId, setActiveDynamicRunId] = useState<string | null>(null);
  const [reactRunnerOpen, setReactRunnerOpen] = useState(false);
  const [clusterResourcesOpen, setClusterResourcesOpen] = useState(false);
  const {
    workflowId,
    workflowName,
    workspaceDir,
    currentWorkspaceWorkflowPath,
    nodes,
    edges,
    isRunning,
    activeRunId,
    staticRuns,
    setWorkflowId,
    setWorkspaceDir,
    setWorkspaceWorkflows,
    setCurrentWorkspaceWorkflowPath,
    setNodes,
    setEdges,
    setIsRunning,
    upsertStaticRun,
    setStaticRunEvents,
    removeStaticRun,
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

  useEffect(() => {
    if (!activeRunId) {
      return;
    }

    const activeRun = staticRuns.find((run) => run.run_id === activeRunId);
    if (activeRun && activeRun.status !== 'running') {
      setIsRunning(false);
      return;
    }

    let canceled = false;
    const poll = async () => {
      try {
        const [runResult, eventsResult] = await Promise.all([
          api.getStaticWorkflowRun(activeRunId, workspaceDir || undefined),
          api.getStaticWorkflowRunEvents(activeRunId, workspaceDir || undefined),
        ]);
        if (canceled) return;
        upsertStaticRun(runResult.run);
        setStaticRunEvents(activeRunId, eventsResult.events || []);
        if (runResult.run.status !== 'running') {
          setIsRunning(false);
        }
      } catch (error) {
        if ((error as any)?.response?.status === 404) {
          removeStaticRun(activeRunId);
          return;
        }
        console.error('Failed to refresh active workflow run:', error);
      }
    };

    const timer = window.setInterval(poll, 1500);
    poll();
    return () => {
      canceled = true;
      window.clearInterval(timer);
    };
  }, [activeRunId, removeStaticRun, setIsRunning, setStaticRunEvents, staticRuns, upsertStaticRun, workspaceDir]);

  return (
    <ConfigProvider locale={enUS}>
      <div style={{ width: '100vw', height: '100vh', display: 'flex', flexDirection: 'column' }}>
        <Toolbar
          onOpenRuns={() => setRunsOpen(true)}
          onOpenReactRunner={() => setReactRunnerOpen(true)}
          onOpenClusterResources={() => setClusterResourcesOpen(true)}
          onReactRunStarted={(runId) => {
            setActiveDynamicRunId(runId);
            setDynamicRunFocusId(runId);
          }}
        />
        
        <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
          <BuiltinTasksSidebar />
          
          <div style={{ flex: 1, position: 'relative' }}>
            <WorkflowCanvas
              activeDynamicRunId={activeDynamicRunId}
              onOpenRuns={() => setRunsOpen(true)}
            />
          </div>
          
          <NodePanel />
        </div>
        
        <ResultsModal />
        <RunsInspector
          open={runsOpen}
          onClose={() => setRunsOpen(false)}
          focusDynamicRunId={dynamicRunFocusId}
        />
        <ReActRunModal
          open={reactRunnerOpen}
          onClose={() => setReactRunnerOpen(false)}
          onCompleted={(runId) => {
            setActiveDynamicRunId(runId);
            setDynamicRunFocusId(runId);
          }}
        />
        <ClusterResourcesDrawer
          open={clusterResourcesOpen}
          onClose={() => setClusterResourcesOpen(false)}
        />
      </div>
    </ConfigProvider>
  );
}

export default App;
