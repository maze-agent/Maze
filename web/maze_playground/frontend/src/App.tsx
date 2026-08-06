import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ConfigProvider } from 'antd';
import enUS from 'antd/locale/en_US';
import { message } from 'antd';
import Toolbar from './components/Toolbar';
import BuiltinTasksSidebar from './components/BuiltinTasksSidebar';
import WorkflowCanvas from './components/WorkflowCanvas';
import RunsInspector from './components/RunsInspector';
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
    workflowSaveState,
    workflowOperation,
    setWorkflowId,
    setWorkflowName,
    setWorkspaceContext,
    setWorkspaceDir,
    setWorkspaceWorkflows,
    setCurrentWorkspaceWorkflowPath,
    setNodes,
    setEdges,
    setWorkflowSaveState,
    acquireWorkflowOperation,
    releaseWorkflowOperation,
    upsertStaticRun,
    removeStaticRun,
  } = useWorkflowStore();

  const workflowFingerprint = useMemo(
    () => workflowDraftFingerprint(workflowName, nodes, edges),
    [edges, nodes, workflowName],
  );
  const trackedRunIds = useMemo(
    () => Array.from(new Set([
      activeRunId,
      runsOpen ? null : selectedRunId,
    ].filter((id): id is string => Boolean(id)))),
    [activeRunId, runsOpen, selectedRunId],
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
        window.sessionStorage.setItem(storageKey, workspace.workspaceId);
        setWorkspaceContext(workspace);
        try {
          const draft = await api.loadWorkspaceWorkflow({
            workspaceId: workspace.workspaceId,
            workspaceDir: workspace.workspaceDir,
            relativePath: WORKFLOW_DRAFT_PATH,
          });
          if (canceled) return;
          latestWorkflowFingerprintRef.current = workflowDraftFingerprint(
            draft.workflow.name,
            draft.workflow.nodes,
            draft.workflow.edges,
          );
          setWorkflowId(createLocalWorkflowId());
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
    setEdges,
    setNodes,
    setWorkflowId,
    setWorkflowName,
    setWorkflowSaveState,
    setWorkspaceContext,
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
    if (selectedRunId) {
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

  return (
    <ConfigProvider locale={enUS}>
      <WorkbenchShell
        topBar={(
          <Toolbar
            onOpenClusterResources={() => setClusterResourcesOpen(true)}
          />
        )}
        leftSidebar={(
          <BuiltinTasksSidebar
            onOpenRuns={() => setRunsOpen(true)}
            workspaceReady={workspaceReady}
          />
        )}
        canvas={<WorkflowCanvas />}
        runsInspector={(
          <RunsInspector
            open={runsOpen}
            onClose={() => setRunsOpen(false)}
            focusStaticRunId={selectedRunId}
          />
        )}
        clusterDrawer={(
          <ClusterResourcesDrawer
            open={clusterResourcesOpen}
            onClose={() => setClusterResourcesOpen(false)}
          />
        )}
      />
    </ConfigProvider>
  );
}

export default App;
