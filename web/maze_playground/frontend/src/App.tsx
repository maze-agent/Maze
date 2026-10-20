import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ConfigProvider } from 'antd';
import enUS from 'antd/locale/en_US';
import { message } from 'antd';
import Toolbar from './components/Toolbar';
import BuiltinTasksSidebar from './components/BuiltinTasksSidebar';
import WorkflowCanvas from './components/WorkflowCanvas';
import RunsInspector from './components/RunsInspector';
import RunWorkbench from './components/RunWorkbench';
import ClusterResourcesDrawer from './components/ClusterResourcesDrawer';
import WorkbenchShell from './components/WorkbenchShell';
import { api } from './api/client';
import { createLocalWorkflowId, useWorkflowStore } from './stores/workflowStore';

const WORKFLOW_DRAFT_PATH = 'workflows/.drafts/current.workflow.json';
const ACTIVE_RUN_STATUSES = new Set(['created', 'queued', 'running']);

function workflowDraftFingerprint(name: string, nodes: any[], edges: any[]) {
  return JSON.stringify({ name, nodes, edges });
}

function App() {
  const autosaveRequestRef = useRef(0);
  const latestWorkflowFingerprintRef = useRef('');
  const [workspaceReady, setWorkspaceReady] = useState(false);
  const [runsOpen, setRunsOpen] = useState(false);
  const [linkedRunId] = useState(() => new URLSearchParams(window.location.search).get('run_id'));
  const [linkedCanvas] = useState(() => new URLSearchParams(window.location.search).get('view') === 'canvas');
  const [canvasRunId, setCanvasRunId] = useState<string | null>(null);
  const [runCanvasOpen, setRunCanvasOpen] = useState(false);
  const [focusDynamicRunId, setFocusDynamicRunId] = useState<string | null>(null);
  const [clusterResourcesOpen, setClusterResourcesOpen] = useState(false);
  const {
    workflowId,
    workflowName,
    workspaceId,
    workspaceDir,
    currentWorkspaceWorkflowPath,
    nodes,
    edges,
    activeRunId,
    selectedRunId,
    staticRuns,
    workflowSaveState,
    workflowOperation,
    setWorkflowId,
    setWorkspaceContext,
    setWorkspaceDir,
    setWorkspaceWorkflows,
    setCurrentWorkspaceWorkflowPath,
    setNodes,
    setEdges,
    setWorkflowSaveState,
    activateWorkspace,
    acquireWorkflowOperation,
    releaseWorkflowOperation,
    upsertStaticRun,
    setSelectedRunId,
    removeStaticRun,
  } = useWorkflowStore();

  const openRunCanvas = useCallback((runId: string) => {
    setCanvasRunId(runId);
    setRunCanvasOpen(true);
    setRunsOpen(false);
    setSelectedRunId(null);
    setFocusDynamicRunId(null);
    const url = new URL(window.location.href);
    url.searchParams.set('run_id', runId);
    url.searchParams.set('view', 'canvas');
    window.history.replaceState(null, '', url);
  }, [setSelectedRunId]);

  const showDesign = useCallback(() => {
    setRunCanvasOpen(false);
    setSelectedRunId(null);
    const url = new URL(window.location.href);
    url.searchParams.delete('run_id');
    url.searchParams.delete('view');
    window.history.replaceState(null, '', url);
  }, [setSelectedRunId]);

  useEffect(() => {
    if (!workspaceReady || !linkedRunId) return;
    if (linkedCanvas) {
      openRunCanvas(linkedRunId);
      return;
    }
    let disposed = false;
    setRunsOpen(true);
    void api.getRun(linkedRunId).then(({ run }) => {
      if (disposed) return;
      if (run.kind === 'dynamic') setFocusDynamicRunId(run.run_id);
      else {
        upsertStaticRun(run);
        setSelectedRunId(run.run_id);
      }
    }).catch(() => { if (!disposed) message.error('The linked run could not be loaded.'); });
    return () => { disposed = true; };
  }, [workspaceReady, linkedRunId, linkedCanvas, openRunCanvas, setSelectedRunId, upsertStaticRun]);

  const workflowFingerprint = useMemo(
    () => workflowDraftFingerprint(workflowName, nodes, edges),
    [edges, nodes, workflowName],
  );
  const activeStaticRunIds = staticRuns
    .filter((run) => (
      ACTIVE_RUN_STATUSES.has(run.status)
      && (!run.workspace_id || run.workspace_id === workspaceId)
    ))
    .map((run) => run.run_id)
    .sort()
    .join('\n');
  const trackedRunIds = useMemo(
    () => Array.from(new Set([
      activeRunId,
      runsOpen ? null : selectedRunId,
      ...activeStaticRunIds.split('\n').filter(Boolean),
    ].filter((id): id is string => Boolean(id)))),
    [activeRunId, activeStaticRunIds, runsOpen, selectedRunId],
  );

  useEffect(() => {
    latestWorkflowFingerprintRef.current = workflowFingerprint;
  }, [workflowFingerprint]);

  useEffect(() => {
    let canceled = false;
    const operationToken = acquireWorkflowOperation('Initializing workspace');
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
        let draft = null;
        try {
          const restored = await api.loadWorkspaceWorkflow({
            workspaceId: workspace.workspaceId,
            workspaceDir: workspace.workspaceDir,
            relativePath: WORKFLOW_DRAFT_PATH,
          });
          if (canceled) return;
          latestWorkflowFingerprintRef.current = workflowDraftFingerprint(
            restored.workflow.name,
            restored.workflow.nodes,
            restored.workflow.edges,
          );
          draft = restored.workflow;
        } catch (error) {
          console.debug('No restorable workflow draft found:', error);
        }
        window.sessionStorage.setItem(storageKey, workspace.workspaceId);
        activateWorkspace(workspace, draft);
        setWorkspaceReady(true);
      })
      .catch((error) => {
        console.error('Failed to initialize workspace:', error);
        if (!canceled) {
          setWorkspaceReady(true);
        }
      })
      .finally(() => {
        if (operationToken) {
          releaseWorkflowOperation(operationToken);
        }
      });
    return () => {
      canceled = true;
      if (operationToken) {
        releaseWorkflowOperation(operationToken);
      }
    };
  }, [
    acquireWorkflowOperation,
    releaseWorkflowOperation,
    activateWorkspace,
  ]);

  const switchWorkspace = useCallback(async (targetWorkspaceId: string) => {
    if (!targetWorkspaceId || targetWorkspaceId === workspaceId) return true;
    const operationToken = acquireWorkflowOperation('Switching workspace');
    if (!operationToken) {
      message.info(`Please wait for ${useWorkflowStore.getState().workflowOperation?.label || 'the current workflow operation'}`);
      return false;
    }

    try {
      if (workspaceDir) {
        await api.saveWorkspaceWorkflow({
          workspaceId: workspaceId || undefined,
          workspaceDir,
          relativePath: WORKFLOW_DRAFT_PATH,
          name: workflowName,
          workflowId: workflowId || createLocalWorkflowId(),
          nodes,
          edges,
        });
      }

      const target = await api.getWorkspace(targetWorkspaceId);
      let draft = null;
      try {
        const restored = await api.loadWorkspaceWorkflow({
          workspaceId: target.workspaceId,
          workspaceDir: target.workspaceDir,
          relativePath: WORKFLOW_DRAFT_PATH,
        });
        draft = restored.workflow;
      } catch (error: any) {
        if (error?.response?.status !== 404) throw error;
      }

      latestWorkflowFingerprintRef.current = workflowDraftFingerprint(
        draft?.name || 'Untitled Workflow',
        draft?.nodes || [],
        draft?.edges || [],
      );
      activateWorkspace(target, draft);
      window.sessionStorage.setItem('maze.playground.workspaceId', target.workspaceId);
      message.success(`Workspace loaded: ${target.manifest.name}`);
      return true;
    } catch (error: any) {
      console.error('Failed to switch workspace:', error);
      message.error(error.response?.data?.error || error.message || 'Failed to switch workspace');
      return false;
    } finally {
      releaseWorkflowOperation(operationToken);
    }
  }, [
    acquireWorkflowOperation,
    activateWorkspace,
    edges,
    nodes,
    releaseWorkflowOperation,
    workflowId,
    workflowName,
    workspaceDir,
    workspaceId,
  ]);

  useEffect(() => {
    if (!workspaceReady || !workspaceDir || workflowOperation) {
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
      const operationToken = acquireWorkflowOperation('Autosaving workflow');
      if (!operationToken) {
        return;
      }
      setWorkflowSaveState({
        status: 'saving_draft',
        draftPath: WORKFLOW_DRAFT_PATH,
        error: null,
      });

      try {
        const activeWorkflowId = workflowId || createLocalWorkflowId();
        if (!workflowId) {
          setWorkflowId(activeWorkflowId);
        }
        const saved = await api.saveWorkspaceWorkflow({
          workspaceId: workspaceId || undefined,
          workspaceDir,
          relativePath: WORKFLOW_DRAFT_PATH,
          name: workflowName,
          workflowId: activeWorkflowId,
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
      } finally {
        releaseWorkflowOperation(operationToken);
      }
    }, 900);

    return () => window.clearTimeout(timer);
  }, [
    edges,
    acquireWorkflowOperation,
    nodes,
    releaseWorkflowOperation,
    setWorkflowId,
    setWorkflowSaveState,
    setWorkspaceContext,
    workflowFingerprint,
    workflowId,
    workflowName,
    workflowOperation,
    workflowSaveState,
    workspaceDir,
    workspaceId,
    workspaceReady,
  ]);

  const saveWorkflowToWorkspace = useCallback(async () => {
    if (runCanvasOpen || selectedRunId) {
      message.info('Switch to Design mode to save the workflow draft');
      return;
    }
    if (nodes.length === 0) {
      message.warning('Please add at least one task node before saving');
      return;
    }

    const operationToken = acquireWorkflowOperation('Saving workflow');
    if (!operationToken) {
      message.info(`Please wait for ${useWorkflowStore.getState().workflowOperation?.label || 'the current workflow operation'}`);
      return;
    }
    const hideLoading = message.loading('Saving workflow...', 0);

    try {
      const activeWorkspace = workspaceDir || (await api.getWorkspaceWorkflows()).workspaceDir;
      const activeWorkflowId = workflowId || createLocalWorkflowId();
      if (!workflowId) {
        setWorkflowId(activeWorkflowId);
      }

      const saved = await api.saveWorkspaceWorkflow({
        workspaceDir: activeWorkspace,
        relativePath: currentWorkspaceWorkflowPath,
        name: workflowName,
        workflowId: activeWorkflowId,
        nodes,
        edges,
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
      releaseWorkflowOperation(operationToken);
    }
  }, [
    acquireWorkflowOperation,
    currentWorkspaceWorkflowPath,
    edges,
    nodes,
    releaseWorkflowOperation,
    setCurrentWorkspaceWorkflowPath,
    setEdges,
    setNodes,
    setWorkflowId,
    setWorkflowSaveState,
    setWorkspaceContext,
    setWorkspaceDir,
    setWorkspaceWorkflows,
    selectedRunId,
    runCanvasOpen,
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
    if (trackedRunIds.length === 0) {
      return undefined;
    }

    let canceled = false;
    const timers = new Set<number>();

    function scheduleNextPoll(runId: string) {
      if (canceled) return;
      const timer = window.setTimeout(() => {
        timers.delete(timer);
        void poll(runId);
      }, 1500);
      timers.add(timer);
    }

    async function poll(runId: string) {
      try {
        const runResult = await api.getRun(runId);
        if (canceled) return;
        upsertStaticRun(runResult.run);
        if (ACTIVE_RUN_STATUSES.has(runResult.run.status)) {
          scheduleNextPoll(runId);
        }
      } catch (error) {
        if (canceled) return;
        if ((error as any)?.response?.status === 404) {
          removeStaticRun(runId);
          return;
        }
        console.error('Failed to refresh active workflow run:', error);
        scheduleNextPoll(runId);
      }
    }

    trackedRunIds.forEach((runId) => {
      const cached = useWorkflowStore.getState().staticRuns.find((run) => run.run_id === runId);
      if (!cached || ACTIVE_RUN_STATUSES.has(cached.status)) {
        void poll(runId);
      }
    });
    return () => {
      canceled = true;
      timers.forEach((timer) => window.clearTimeout(timer));
      timers.clear();
    };
  }, [removeStaticRun, trackedRunIds, upsertStaticRun]);

  const runsInspector = <RunsInspector
    open={runsOpen} onClose={() => setRunsOpen(false)}
    focusStaticRunId={selectedRunId} focusDynamicRunId={focusDynamicRunId}
    initialShowAllRuns={Boolean(linkedRunId) || runCanvasOpen}
    onOpenCanvas={openRunCanvas}
  />;
  const clusterDrawer = <ClusterResourcesDrawer open={clusterResourcesOpen} onClose={() => setClusterResourcesOpen(false)} />;

  return (
    <ConfigProvider locale={enUS}>
      {runCanvasOpen && canvasRunId ? <RunWorkbench
        runId={canvasRunId} onSelectRun={openRunCanvas} onDesign={showDesign}
        onOpenHistory={(run) => {
          if (run?.kind === 'dynamic') setFocusDynamicRunId(run.run_id);
          else if (run) { upsertStaticRun(run); setSelectedRunId(run.run_id); }
          setRunsOpen(true);
        }}
        onOpenCluster={() => setClusterResourcesOpen(true)}
        runsInspector={runsInspector} clusterDrawer={clusterDrawer}
      /> : <WorkbenchShell
        topBar={(
          <Toolbar
            onOpenClusterResources={() => setClusterResourcesOpen(true)}
            onOpenRunMode={() => canvasRunId ? openRunCanvas(canvasRunId) : setRunsOpen(true)}
            onShowDesign={showDesign}
          />
        )}
        leftSidebar={(
          <BuiltinTasksSidebar
            onOpenRuns={() => setRunsOpen(true)}
            onSwitchWorkspace={switchWorkspace}
            workspaceReady={workspaceReady}
          />
        )}
        canvas={<WorkflowCanvas />}
        runsInspector={runsInspector}
        clusterDrawer={clusterDrawer}
      />}
    </ConfigProvider>
  );
}

export default App;
