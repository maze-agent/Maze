import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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
import WorkspaceAgentPanel from './components/WorkspaceAgentPanel';
import { api } from './api/client';
import { useWorkflowStore } from './stores/workflowStore';

const WORKFLOW_DRAFT_PATH = 'workflows/.drafts/current.workflow.json';

function workflowDraftFingerprint(name: string, nodes: any[], edges: any[]) {
  return JSON.stringify({ name, nodes, edges });
}

function App() {
  const saveShortcutInFlightRef = useRef(false);
  const autosaveRequestRef = useRef(0);
  const latestWorkflowFingerprintRef = useRef('');
  const [workspaceReady, setWorkspaceReady] = useState(false);
  const [runsOpen, setRunsOpen] = useState(false);
  const [dynamicRunFocusId, setDynamicRunFocusId] = useState<string | null>(null);
  const [staticRunFocusId, setStaticRunFocusId] = useState<string | null>(null);
  const [activeDynamicRunId, setActiveDynamicRunId] = useState<string | null>(null);
  const [reactRunnerOpen, setReactRunnerOpen] = useState(false);
  const [clusterResourcesOpen, setClusterResourcesOpen] = useState(false);
  const [workspaceAgentOpen, setWorkspaceAgentOpen] = useState(true);
  const {
    workflowId,
    workflowName,
    workspaceId,
    workspaceDir,
    currentWorkspaceWorkflowPath,
    nodes,
    edges,
    isRunning,
    activeRunId,
    staticRuns,
    workflowSaveState,
    setWorkflowId,
    setWorkflowName,
    setWorkspaceContext,
    setWorkspaceDir,
    setWorkspaceWorkflows,
    setCurrentWorkspaceWorkflowPath,
    setNodes,
    setEdges,
    setWorkflowSaveState,
    setIsRunning,
    upsertStaticRun,
    setStaticRunEvents,
    removeStaticRun,
  } = useWorkflowStore();

  const workflowFingerprint = useMemo(
    () => workflowDraftFingerprint(workflowName, nodes, edges),
    [edges, nodes, workflowName],
  );

  useEffect(() => {
    latestWorkflowFingerprintRef.current = workflowFingerprint;
  }, [workflowFingerprint]);

  useEffect(() => {
    let canceled = false;
    const storageKey = 'maze.playground.workspaceId';
    const restoreOrCreateWorkspace = async () => {
      const existingWorkspaceId = window.sessionStorage.getItem(storageKey);
      if (existingWorkspaceId) {
        try {
          return await api.getWorkspace(existingWorkspaceId);
        } catch (error) {
          console.warn('Failed to restore session workspace, creating a new one:', error);
          window.sessionStorage.removeItem(storageKey);
        }
      }
      return api.createWorkspace({ mode: 'session' });
    };

    restoreOrCreateWorkspace()
      .then(async (workspace) => {
        if (canceled) return;
        window.sessionStorage.setItem(storageKey, workspace.workspaceId);
        setWorkspaceContext(workspace);
        try {
          const draft = await api.loadWorkspaceWorkflow({
            workspaceId: workspace.workspaceId,
            workspaceDir: workspace.workspaceDir,
            relativePath: WORKFLOW_DRAFT_PATH,
          });
          if (canceled) return;
          const created = await api.createWorkflow(draft.workflow.name);
          if (canceled) return;
          await api.saveWorkflow(created.workflowId, {
            name: draft.workflow.name,
            nodes: draft.workflow.nodes,
            edges: draft.workflow.edges,
          });
          latestWorkflowFingerprintRef.current = workflowDraftFingerprint(
            draft.workflow.name,
            draft.workflow.nodes,
            draft.workflow.edges,
          );
          setWorkflowId(created.workflowId);
          setWorkflowName(draft.workflow.name);
          setNodes(draft.workflow.nodes);
          setEdges(draft.workflow.edges);
          setWorkflowSaveState({
            status: 'saved_draft',
            draftPath: WORKFLOW_DRAFT_PATH,
            savedAt: new Date().toISOString(),
            error: null,
          });
        } catch (error) {
          console.debug('No restorable workflow draft found:', error);
        }
        setWorkspaceReady(true);
      })
      .catch((error) => {
        console.error('Failed to initialize workspace:', error);
        if (!canceled) {
          setWorkspaceReady(true);
        }
      });
    return () => {
      canceled = true;
    };
  }, [setEdges, setNodes, setWorkflowId, setWorkflowName, setWorkflowSaveState, setWorkspaceContext]);

  useEffect(() => {
    if (!workspaceReady || !workspaceDir) {
      return undefined;
    }

    if (nodes.length === 0) {
      if (workflowSaveState !== 'empty') {
        setWorkflowSaveState({
          status: 'empty',
          draftPath: WORKFLOW_DRAFT_PATH,
          savedAt: null,
          error: null,
        });
      }
      return undefined;
    }

    if (workflowSaveState !== 'unsaved_draft' && workflowSaveState !== 'error') {
      return undefined;
    }

    const requestId = autosaveRequestRef.current + 1;
    autosaveRequestRef.current = requestId;
    const saveFingerprint = workflowFingerprint;

    const timer = window.setTimeout(async () => {
      setWorkflowSaveState({
        status: 'saving_draft',
        draftPath: WORKFLOW_DRAFT_PATH,
        error: null,
      });

      try {
        const saved = await api.saveWorkspaceWorkflow({
          workspaceId: workspaceId || undefined,
          workspaceDir,
          relativePath: WORKFLOW_DRAFT_PATH,
          name: workflowName,
          workflowId,
          nodes,
          edges,
        });

        setWorkspaceContext(saved);
        if (autosaveRequestRef.current === requestId && latestWorkflowFingerprintRef.current === saveFingerprint) {
          setWorkflowSaveState({
            status: 'saved_draft',
            draftPath: WORKFLOW_DRAFT_PATH,
            savedAt: new Date().toISOString(),
            error: null,
          });
        }
      } catch (error: any) {
        console.error('Failed to autosave workflow draft:', error);
        if (autosaveRequestRef.current === requestId) {
          setWorkflowSaveState({
            status: 'error',
            draftPath: WORKFLOW_DRAFT_PATH,
            error: error.response?.data?.error || error.message || 'Failed to autosave workflow draft',
          });
        }
      }
    }, 900);

    return () => window.clearTimeout(timer);
  }, [
    edges,
    nodes,
    setWorkflowSaveState,
    setWorkspaceContext,
    workflowFingerprint,
    workflowId,
    workflowName,
    workflowSaveState,
    workspaceDir,
    workspaceId,
    workspaceReady,
  ]);

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
      setWorkspaceContext(saved);
      setWorkspaceDir(saved.workspaceDir);
      setCurrentWorkspaceWorkflowPath(saved.relativePath);
      setWorkspaceWorkflows(refreshed.workflows || []);
      setNodes(saved.workflow.nodes);
      setEdges(saved.workflow.edges);
      setWorkflowSaveState({
        status: 'saved_workflow',
        draftPath: WORKFLOW_DRAFT_PATH,
        savedAt: new Date().toISOString(),
        error: null,
      });
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
    setWorkflowSaveState,
    setWorkspaceContext,
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
            setStaticRunFocusId(null);
          }}
        />
        
        <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
          {workspaceReady && workspaceDir ? <BuiltinTasksSidebar /> : null}
          
          <div style={{ flex: 1, position: 'relative' }}>
            <WorkflowCanvas
              activeDynamicRunId={activeDynamicRunId}
              onOpenRuns={() => setRunsOpen(true)}
            />
          </div>
          
          <NodePanel />
          {workspaceReady && workspaceDir ? (
            <WorkspaceAgentPanel
              open={workspaceAgentOpen}
              workspaceReady={workspaceReady}
              onToggle={() => setWorkspaceAgentOpen((value) => !value)}
              onOpenRuns={(runId) => {
                if (runId) {
                  setActiveDynamicRunId(null);
                  setDynamicRunFocusId(null);
                  setStaticRunFocusId(runId);
                }
                setRunsOpen(true);
              }}
            />
          ) : null}
        </div>
        
        <ResultsModal />
        <RunsInspector
          open={runsOpen}
          onClose={() => setRunsOpen(false)}
          focusDynamicRunId={dynamicRunFocusId}
          focusStaticRunId={staticRunFocusId}
        />
        <ReActRunModal
          open={reactRunnerOpen}
          onClose={() => setReactRunnerOpen(false)}
          onCompleted={(runId) => {
            setActiveDynamicRunId(runId);
            setDynamicRunFocusId(runId);
            setStaticRunFocusId(null);
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
