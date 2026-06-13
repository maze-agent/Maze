import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Collapse, Empty, Input, List, Modal, Select, Space, Tag, Tooltip, Typography, message } from 'antd';
import { Bot, CheckCircle2, Download, Eye, FileText, PanelRightClose, PanelRightOpen, Pencil, Play, Plus, Save, Send, Square, Trash2, Wrench } from 'lucide-react';
import { api, WorkspaceAgentDraft, WorkspaceAgentEvent, WorkspaceAgentMessage, WorkspaceAgentSession } from '@/api/client';
import { useWorkflowStore } from '@/stores/workflowStore';
import { loadLlmSettings } from '@/utils/llmSettings';
import PendingActionCard from './PendingActionCard';

const { Text, Paragraph } = Typography;

function messageText(messageItem: WorkspaceAgentMessage) {
  return (messageItem.parts || [])
    .filter((part) => part.type === 'text' || part.type === 'error')
    .map((part) => part.type === 'error' ? part.message : part.text)
    .filter(Boolean)
    .join('\n');
}

function inspectRunResults(messageItem: WorkspaceAgentMessage) {
  return (messageItem.parts || [])
    .filter((part) => part.type === 'tool_result' && part.name === 'inspect_workflow_run')
    .map((part) => part.result?.run)
    .filter(Boolean);
}

function promotedFileResults(messageItem: WorkspaceAgentMessage) {
  return (messageItem.parts || [])
    .filter((part) => part.type === 'tool_result' && part.name === 'promote_run_artifact')
    .map((part) => part.result)
    .filter((result) => result?.ok && result?.file);
}

function readWorkspaceFileResults(messageItem: WorkspaceAgentMessage) {
  return (messageItem.parts || [])
    .filter((part) => part.type === 'tool_result' && part.name === 'read_workspace_file')
    .map((part) => part.result)
    .filter((result) => result?.ok && result?.relativePath);
}

function readWorkspaceWorkflowResults(messageItem: WorkspaceAgentMessage) {
  return (messageItem.parts || [])
    .filter((part) => part.type === 'tool_result' && part.name === 'read_workspace_workflow')
    .map((part) => part.result)
    .filter((result) => result?.ok && result?.relativePath && result?.workflow);
}

function readWorkspaceTaskResults(messageItem: WorkspaceAgentMessage) {
  return (messageItem.parts || [])
    .filter((part) => part.type === 'tool_result' && part.name === 'read_workspace_task')
    .map((part) => part.result)
    .filter((result) => result?.ok && result?.relativePath);
}

function workspaceInventoryResults(messageItem: WorkspaceAgentMessage) {
  return (messageItem.parts || [])
    .filter((part) => part.type === 'tool_result' && part.name === 'list_workspace_items')
    .map((part) => part.result)
    .filter((result) => result?.ok);
}

function workspaceFileListResults(messageItem: WorkspaceAgentMessage) {
  return (messageItem.parts || [])
    .filter((part) => part.type === 'tool_result' && part.name === 'list_workspace_files')
    .map((part) => part.result)
    .filter((result) => result?.ok && Array.isArray(result.files));
}

function currentWorkflowResults(messageItem: WorkspaceAgentMessage) {
  return (messageItem.parts || [])
    .filter((part) => part.type === 'tool_result' && part.name === 'read_current_workflow')
    .map((part) => part.result?.currentWorkflow)
    .filter((result) => result && typeof result === 'object');
}

function hasToolEvidence(messageItem: WorkspaceAgentMessage) {
  return inspectRunResults(messageItem).length > 0
    || promotedFileResults(messageItem).length > 0
    || readWorkspaceFileResults(messageItem).length > 0
    || readWorkspaceWorkflowResults(messageItem).length > 0
    || readWorkspaceTaskResults(messageItem).length > 0
    || workspaceInventoryResults(messageItem).length > 0
    || workspaceFileListResults(messageItem).length > 0
    || currentWorkflowResults(messageItem).length > 0;
}

function shortAgentValue(value?: string | number | null) {
  const text = String(value || '');
  if (!text) return '-';
  return text.length > 18 ? `${text.slice(0, 8)}...${text.slice(-6)}` : text;
}

function formatAgentBytes(value?: number | string | null) {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function artifactHref(artifact: any) {
  return artifact?.download?.url
    || artifact?.download?.staticRun?.url
    || artifact?.download?.cas?.url
    || '';
}

function uniqueAgentArtifacts(artifacts: any[] = []) {
  const byKey = new Map<string, any>();
  for (const artifact of artifacts || []) {
    const key = [
      artifact.path || artifact.name || '',
      artifact.taskId || artifact.task_id || '',
      artifact.sha256 || '',
      artifactHref(artifact),
    ].join('::');
    if (!byKey.has(key)) {
      byKey.set(key, artifact);
    }
  }
  return Array.from(byKey.values());
}

function collectDraftsFromEvents(events: WorkspaceAgentEvent[]) {
  const drafts = new Map<string, WorkspaceAgentDraft>();
  for (const event of events) {
    const result = event.result || {};
    const draft = result.draft || event.draft;
    if (draft?.id) {
      drafts.set(draft.id, draft);
    }
  }
  return Array.from(drafts.values());
}

function collectDraftsFromMessages(messages: WorkspaceAgentMessage[]) {
  const drafts = new Map<string, WorkspaceAgentDraft>();
  for (const messageItem of messages) {
    for (const part of messageItem.parts || []) {
      const result = (part as any).result || {};
      const draft = result.draft || (part as any).draft;
      if (draft?.id) {
        drafts.set(draft.id, draft);
      }
    }
  }
  return Array.from(drafts.values());
}

function mergeDraftLists(...lists: WorkspaceAgentDraft[][]) {
  const drafts = new Map<string, WorkspaceAgentDraft>();
  for (const list of lists) {
    for (const draft of list || []) {
      if (draft?.id) {
        drafts.set(draft.id, {
          ...(drafts.get(draft.id) || {}),
          ...draft,
        });
      }
    }
  }
  return Array.from(drafts.values());
}

function draftsFromMessageResponse(result: { messages?: WorkspaceAgentMessage[]; drafts?: WorkspaceAgentDraft[] }) {
  return mergeDraftLists(
    collectDraftsFromMessages(result.messages || []),
    result.drafts || [],
  );
}

function eventLabel(event: WorkspaceAgentEvent) {
  if (event.type === 'tool_call') return `Running ${event.name}`;
  if (event.type === 'tool_result') return `${event.name} ${event.ok === false ? 'failed' : 'done'}`;
  if (event.type === 'context_usage') return `Context ${event.inputTokens || 0} tokens`;
  if (event.type === 'llm_waiting') return `Waiting for LLM (${Math.round(Number(event.timeoutMs || 0) / 1000)}s timeout)`;
  if (event.type === 'context_compacted') return `Compacted ${event.compactedCount || 0} messages`;
  if (event.type === 'interrupted') return 'Interrupted by backend restart';
  if (event.type === 'finish') return `Finished (${event.reason || 'stop'})`;
  return event.type;
}

function visibleAgentEvents(events: WorkspaceAgentEvent[]) {
  const important = events.filter((event) => (
    event.type === 'finish'
    || event.type === 'error'
    || event.type === 'canceled'
    || event.type === 'interrupted'
    || event.type === 'llm_waiting'
    || event.type === 'context_compacted'
    || event.type === 'pending_action'
    || (event.type === 'tool_result' && event.ok === false)
  ));
  return important.slice(-6);
}

function currentAgentRunLabel(events: WorkspaceAgentEvent[]) {
  const lastToolCall = [...events].reverse().find((event) => event.type === 'tool_call');
  if (lastToolCall?.name) return `Running ${lastToolCall.name}`;
  const lastContext = [...events].reverse().find((event) => event.type === 'context_usage');
  if (lastContext) return 'Thinking';
  return 'Working';
}

function stableJson(value: any) {
  try {
    return JSON.stringify(value ?? null);
  } catch {
    return String(value ?? '');
  }
}

function buildDraftDiff(draft: WorkspaceAgentDraft, currentWorkflow: any) {
  const currentNodes = new Map((currentWorkflow.nodes || []).map((node: any) => [node.id, node]));
  const draftNodes = new Map((draft.workflow?.nodes || []).map((node: any) => [node.id, node]));
  const currentEdges = new Map((currentWorkflow.edges || []).map((edge: any) => [edge.id || `${edge.source}->${edge.target}`, edge]));
  const draftEdges = new Map((draft.workflow?.edges || []).map((edge: any) => [edge.id || `${edge.source}->${edge.target}`, edge]));

  const addedNodes = Array.from(draftNodes.values()).filter((node: any) => !currentNodes.has(node.id));
  const removedNodes = Array.from(currentNodes.values()).filter((node: any) => !draftNodes.has(node.id));
  const changedNodes = Array.from(draftNodes.values()).filter((node: any) => {
    const current = currentNodes.get(node.id);
    return current && stableJson(current) !== stableJson(node);
  });

  const addedEdges = Array.from(draftEdges.values()).filter((edge: any) => !currentEdges.has(edge.id || `${edge.source}->${edge.target}`));
  const removedEdges = Array.from(currentEdges.values()).filter((edge: any) => !draftEdges.has(edge.id || `${edge.source}->${edge.target}`));
  const changedEdges = Array.from(draftEdges.values()).filter((edge: any) => {
    const current = currentEdges.get(edge.id || `${edge.source}->${edge.target}`);
    return current && stableJson(current) !== stableJson(edge);
  });

  const taskFiles = (draft.taskDefinitions || []).map((task: any) => ({
    relativePath: task.relativePath,
    functionName: task.functionName,
    bytes: String(task.code || '').length,
  }));

  return {
    addedNodes,
    removedNodes,
    changedNodes,
    addedEdges,
    removedEdges,
    changedEdges,
    taskFiles,
    hasChanges:
      addedNodes.length > 0 ||
      removedNodes.length > 0 ||
      changedNodes.length > 0 ||
      addedEdges.length > 0 ||
      removedEdges.length > 0 ||
      changedEdges.length > 0 ||
      taskFiles.length > 0,
  };
}

type PendingAgentActionType = 'save' | 'run';

interface PendingAgentAction {
  id: string;
  type: PendingAgentActionType;
  draftId: string;
  createdAt: string;
  error?: string;
}

function draftRunTagColor(status?: string) {
  if (status === 'failed') return 'red';
  if (status === 'canceled') return 'orange';
  if (status === 'completed' || status === 'succeeded') return 'green';
  return 'blue';
}

function isAgentRunDone(status?: string) {
  return status === 'succeeded' || status === 'failed' || status === 'canceled' || status === 'interrupted';
}

function isDraftWorkflowRunDone(status?: string) {
  return ['completed', 'succeeded', 'failed', 'canceled', 'interrupted'].includes(String(status || '').toLowerCase());
}

interface WorkspaceAgentPanelProps {
  open: boolean;
  onToggle: () => void;
  onOpenRuns?: (runId?: string) => void;
  workspaceReady?: boolean;
}

export default function WorkspaceAgentPanel({ open, onToggle, onOpenRuns, workspaceReady = true }: WorkspaceAgentPanelProps) {
  const {
    workflowId,
    workflowName,
    workspaceId,
    workspaceDir,
    currentWorkspaceWorkflowPath,
    nodes,
    edges,
    setWorkflowId,
    setWorkflowName,
    setNodes,
    setEdges,
    setWorkspaceContext,
    setCurrentWorkspaceWorkflowPath,
    setWorkspaceWorkflows,
    setActiveRun,
    setIsRunning,
  } = useWorkflowStore();
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [sessions, setSessions] = useState<WorkspaceAgentSession[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string>('');
  const [messages, setMessages] = useState<WorkspaceAgentMessage[]>([]);
  const [events, setEvents] = useState<WorkspaceAgentEvent[]>([]);
  const [drafts, setDrafts] = useState<WorkspaceAgentDraft[]>([]);
  const [activeRunId, setActiveRunId] = useState('');
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [loadingSessions, setLoadingSessions] = useState(false);
  const [pendingActions, setPendingActions] = useState<PendingAgentAction[]>([]);
  const [processingActionId, setProcessingActionId] = useState('');
  const [editingSessionId, setEditingSessionId] = useState('');
  const [editingSessionTitle, setEditingSessionTitle] = useState('');
  const [sessionActionId, setSessionActionId] = useState('');
  const [cancelingRun, setCancelingRun] = useState(false);
  const [promotingArtifactKey, setPromotingArtifactKey] = useState('');
  const [openingWorkflowPath, setOpeningWorkflowPath] = useState('');
  const workspaceKey = `${workspaceId || ''}:${workspaceDir || ''}`;

  const llmReady = useMemo(() => {
    const llm = loadLlmSettings();
    return Boolean(llm.baseUrl && llm.apiKey && llm.model);
  }, [open]);

  const currentWorkflow = useMemo(() => ({
    workflowId,
    name: workflowName,
    relativePath: currentWorkspaceWorkflowPath,
    nodes,
    edges,
  }), [currentWorkspaceWorkflowPath, edges, nodes, workflowId, workflowName]);

  const visibleDrafts = useMemo(
    () => drafts.filter((draft) => draft.status !== 'dismissed'),
    [drafts],
  );
  const activeSession = useMemo(
    () => sessions.find((session) => session.id === activeSessionId) || null,
    [activeSessionId, sessions],
  );

  const runningDraftRunIds = useMemo(
    () => visibleDrafts
      .filter((draft) => draft.run?.runId && !isDraftWorkflowRunDone(String(draft.run?.status || '')))
      .map((draft) => draft.id),
    [visibleDrafts],
  );

  const refreshSessions = useCallback(async () => {
    if (!workspaceReady || !workspaceDir) return;
    setLoadingSessions(true);
    try {
      const result = await api.listAgentSessions({ workspaceId, workspaceDir });
      const nextSessions = result.sessions || [];
      setSessions(nextSessions);
      if (!nextSessions.some((session) => session.id === activeSessionId)) {
        setActiveSessionId(nextSessions[0]?.id || '');
      }
    } catch (error) {
      console.error('Failed to load Workspace Agent sessions:', error);
    } finally {
      setLoadingSessions(false);
    }
  }, [activeSessionId, workspaceDir, workspaceId, workspaceReady]);

  useEffect(() => {
    setSessions([]);
    setActiveSessionId('');
    setMessages([]);
    setEvents([]);
    setDrafts([]);
    setActiveRunId('');
    setBusy(false);
    setPendingActions([]);
    setProcessingActionId('');
    setEditingSessionId('');
    setEditingSessionTitle('');
    setSessionActionId('');
    setCancelingRun(false);
    setPromotingArtifactKey('');
    setOpeningWorkflowPath('');
  }, [workspaceKey]);

  useEffect(() => {
    if (!open || !workspaceReady || !workspaceDir || runningDraftRunIds.length === 0) {
      return undefined;
    }

    let canceled = false;
    const refreshRunningDrafts = async () => {
      const uniqueDraftIds = Array.from(new Set(runningDraftRunIds));
      const results = await Promise.allSettled(
        uniqueDraftIds.map((draftId) => api.getAgentDraft(draftId, { workspaceId, workspaceDir })),
      );
      if (canceled) return;

      const refreshedDrafts = results
        .filter((result): result is PromiseFulfilledResult<Awaited<ReturnType<typeof api.getAgentDraft>>> => result.status === 'fulfilled')
        .map((result) => result.value.draft)
        .filter(Boolean);
      if (refreshedDrafts.length > 0) {
        setDrafts((items) => mergeDraftLists(items, refreshedDrafts));
      }

      results.forEach((result) => {
        if (result.status === 'rejected') {
          console.error('Failed to refresh Workspace Agent draft run:', result.reason);
        }
      });
    };

    const timer = window.setInterval(refreshRunningDrafts, 1500);
    refreshRunningDrafts();
    return () => {
      canceled = true;
      window.clearInterval(timer);
    };
  }, [open, runningDraftRunIds, workspaceDir, workspaceId, workspaceReady]);

  useEffect(() => {
    if (!open || !workspaceReady) return;
    refreshSessions();
  }, [open, refreshSessions, workspaceReady]);

  useEffect(() => {
    if (!open || !workspaceReady || !workspaceDir || !activeSessionId) {
      setMessages([]);
      return;
    }
    let canceled = false;
    api.getAgentMessages(activeSessionId, { workspaceId, workspaceDir })
      .then((result) => {
        if (canceled) return;
        const loadedMessages = result.messages || [];
        setMessages(loadedMessages);
        setDrafts((items) => mergeDraftLists(draftsFromMessageResponse(result), items));
      })
      .catch((error) => {
        console.error('Failed to load Workspace Agent messages:', error);
      });
    return () => {
      canceled = true;
    };
  }, [activeSessionId, open, workspaceDir, workspaceId, workspaceReady]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, events, visibleDrafts, pendingActions]);

  useEffect(() => {
    const visibleDraftIds = new Set(visibleDrafts.map((draft) => draft.id));
    setPendingActions((items) => items.filter((item) => visibleDraftIds.has(item.draftId)));
  }, [visibleDrafts]);

  useEffect(() => {
    if (!workspaceReady || !activeRunId || !activeSessionId || !workspaceDir) return undefined;
    let canceled = false;
    let after = 0;
    let eventSource: EventSource | null = null;

    if (typeof window !== 'undefined' && 'EventSource' in window) {
      eventSource = new EventSource(api.getAgentRunStreamUrl(activeRunId, { workspaceId, workspaceDir }));
      eventSource.onmessage = (event) => {
        try {
          const parsed = JSON.parse(event.data);
          after = Math.max(after, Number(parsed.seq || 0));
          setEvents((items) => {
            const bySeq = new Map(items.map((item) => [item.seq, item]));
            bySeq.set(parsed.seq, parsed);
            const merged = Array.from(bySeq.values()).sort((left, right) => Number(left.seq || 0) - Number(right.seq || 0));
            setDrafts(collectDraftsFromEvents(merged));
            return merged;
          });
          if (parsed.type === 'finish' || parsed.type === 'error' || parsed.type === 'canceled') {
            setBusy(false);
            setActiveRunId('');
            setCancelingRun(false);
            api.getAgentMessages(activeSessionId, { workspaceId, workspaceDir })
              .then((messageResult) => {
                const loadedMessages = messageResult.messages || [];
                setMessages(loadedMessages);
                setDrafts((items) => mergeDraftLists(draftsFromMessageResponse(messageResult), items));
              })
              .catch((error) => console.error('Failed to refresh Workspace Agent messages:', error));
            eventSource?.close();
          }
        } catch (error) {
          console.error('Failed to parse Workspace Agent SSE event:', error);
        }
      };
      eventSource.onerror = () => {
        eventSource?.close();
        eventSource = null;
      };
    }

    const poll = async () => {
      try {
        const [eventResult, messageResult] = await Promise.all([
          api.getAgentRunEvents(activeRunId, { workspaceId, workspaceDir, after }),
          api.getAgentMessages(activeSessionId, { workspaceId, workspaceDir }),
        ]);
        if (canceled) return;
        if (eventResult.events?.length) {
          after = Math.max(after, ...eventResult.events.map((event) => Number(event.seq || 0)));
          setEvents((items) => {
            const bySeq = new Map(items.map((event) => [event.seq, event]));
            eventResult.events.forEach((event) => bySeq.set(event.seq, event));
            const merged = Array.from(bySeq.values()).sort((left, right) => Number(left.seq || 0) - Number(right.seq || 0));
            setDrafts(collectDraftsFromEvents(merged));
            return merged;
          });
        }
        const loadedMessages = messageResult.messages || [];
        setMessages(loadedMessages);
        setDrafts((items) => mergeDraftLists(draftsFromMessageResponse(messageResult), items));
        if (isAgentRunDone(String(eventResult.run?.status || ''))) {
          setBusy(false);
          setActiveRunId('');
          setCancelingRun(false);
        }
      } catch (error) {
        console.error('Failed to poll Workspace Agent run:', error);
      }
    };
    const timer = window.setInterval(poll, 1000);
    poll();
    return () => {
      canceled = true;
      eventSource?.close();
      window.clearInterval(timer);
    };
  }, [activeRunId, activeSessionId, workspaceDir, workspaceId, workspaceReady]);

  const sendMessage = async () => {
    const text = input.trim();
    if (!text || busy) return;
    if (!llmReady) {
      message.warning('Set LLM base URL, key, and model in the ReAct runner settings first');
      return;
    }
    setBusy(true);
    setInput('');
    let keepBusyForAsyncRun = false;
    try {
      const result = await api.runWorkspaceAgent({
        workspaceId,
        workspaceDir,
        sessionId: activeSessionId || undefined,
        message: text,
        llm: loadLlmSettings(),
        currentWorkflow,
        maxSteps: 8,
        maxTokens: 2048,
        timeoutMs: 180000,
        async: true,
      });
      setActiveSessionId(result.session.id);
      const loadedMessages = result.messages || [];
      const loadedEvents = result.events || [];
      setMessages(loadedMessages);
      setEvents(loadedEvents);
      setDrafts(mergeDraftLists(
        collectDraftsFromMessages(loadedMessages),
        collectDraftsFromEvents(loadedEvents),
        result.drafts || [],
      ));
      setActiveRunId(result.run?.id || '');
      keepBusyForAsyncRun = Boolean(result.run?.id);
      await refreshSessions();
    } catch (error: any) {
      message.error(error.response?.data?.error || error.message || 'Workspace Agent failed');
    } finally {
      if (!keepBusyForAsyncRun) {
        setBusy(false);
      }
    }
  };

  const cancelActiveRun = async () => {
    if (!activeRunId || cancelingRun) return;
    setCancelingRun(true);
    try {
      const result = await api.cancelAgentRun(activeRunId, {
        workspaceId,
        workspaceDir,
        reason: 'Canceled from Workspace Agent panel',
      });
      if (isAgentRunDone(String(result.run?.status || ''))) {
        setBusy(false);
        setActiveRunId('');
      }
      message.success('Workspace Agent run canceled');
    } catch (error: any) {
      message.error(error.response?.data?.error || 'Failed to cancel Workspace Agent run');
    } finally {
      setCancelingRun(false);
    }
  };

  const previewDraft = (draft: WorkspaceAgentDraft) => {
    if (!draft.workflow) return;
    setWorkflowId(draft.saved?.workflowId || workflowId || '');
    setWorkflowName(draft.workflow.name || draft.name);
    setNodes(draft.workflow.nodes || []);
    setEdges(draft.workflow.edges || []);
    setCurrentWorkspaceWorkflowPath(draft.relativePath || null);
    message.success('Draft loaded into canvas');
  };

  const openSavedWorkflow = async (draft: WorkspaceAgentDraft) => {
    const relativePath = String(draft.saved?.relativePath || draft.relativePath || '').trim();
    if (!workspaceDir || !relativePath) {
      message.warning('Saved workflow path is not available');
      return;
    }

    setOpeningWorkflowPath(relativePath);
    try {
      const loaded = await api.loadWorkspaceWorkflow({
        workspaceId,
        workspaceDir,
        relativePath,
      });
      const created = await api.createWorkflow(loaded.workflow.name);

      await api.saveWorkflow(created.workflowId, {
        name: loaded.workflow.name,
        nodes: loaded.workflow.nodes,
        edges: loaded.workflow.edges,
      });

      setWorkspaceContext(loaded);
      setWorkflowId(created.workflowId);
      setWorkflowName(loaded.workflow.name);
      setNodes(loaded.workflow.nodes || []);
      setEdges(loaded.workflow.edges || []);
      setCurrentWorkspaceWorkflowPath(loaded.relativePath);
      const refreshed = await api.getWorkspaceWorkflows(loaded.workspaceDir);
      setWorkspaceWorkflows(refreshed.workflows || []);
      message.success(`Opened ${loaded.relativePath}`);
    } catch (error: any) {
      message.error(error.response?.data?.error || 'Failed to open saved workflow');
    } finally {
      setOpeningWorkflowPath('');
    }
  };

  const validateDraft = async (draft: WorkspaceAgentDraft) => {
    try {
      const result = await api.validateAgentDraft(draft.id, { workspaceId, workspaceDir });
      setDrafts((items) => items.map((item) => item.id === draft.id ? result.draft : item));
      message.success(result.draft.validation?.ok ? 'Draft is valid' : 'Draft has issues');
    } catch (error: any) {
      message.error(error.response?.data?.error || 'Failed to validate draft');
    }
  };

  const dismissDraft = async (draft: WorkspaceAgentDraft) => {
    try {
      const result = await api.dismissAgentDraft(draft.id, {
        workspaceId,
        workspaceDir,
        reason: 'Dismissed from Workspace Agent panel',
      });
      setDrafts((items) => items.map((item) => item.id === draft.id ? result.draft : item));
      setPendingActions((items) => items.filter((item) => item.draftId !== draft.id));
      message.success('Draft dismissed');
    } catch (error: any) {
      message.error(error.response?.data?.error || 'Failed to dismiss draft');
    }
  };

  const queuePendingAction = (draft: WorkspaceAgentDraft, type: PendingAgentActionType) => {
    const id = `${type}:${draft.id}`;
    setPendingActions((items) => [
      ...items.filter((item) => item.id !== id),
      {
        id,
        type,
        draftId: draft.id,
        createdAt: new Date().toISOString(),
      },
    ]);
    message.info(`${type === 'save' ? 'Save' : 'Run'} is waiting for approval`);
  };

  const cancelPendingAction = (actionId: string) => {
    setPendingActions((items) => items.filter((item) => item.id !== actionId));
  };

  const startRenameSession = (session: WorkspaceAgentSession) => {
    setEditingSessionId(session.id);
    setEditingSessionTitle(session.title || session.id);
  };

  const saveSessionTitle = async () => {
    const title = editingSessionTitle.trim();
    if (!editingSessionId || !title) {
      setEditingSessionId('');
      setEditingSessionTitle('');
      return;
    }
    setSessionActionId(editingSessionId);
    try {
      const result = await api.updateAgentSession(editingSessionId, {
        workspaceId,
        workspaceDir,
        title,
      });
      setSessions((items) => items.map((item) => item.id === result.session.id ? result.session : item));
      setEditingSessionId('');
      setEditingSessionTitle('');
      message.success('Session renamed');
    } catch (error: any) {
      message.error(error.response?.data?.error || 'Failed to rename session');
    } finally {
      setSessionActionId('');
    }
  };

  const deleteSession = (session: WorkspaceAgentSession) => {
    Modal.confirm({
      title: 'Delete this Agent session?',
      content: 'This removes the chat history only. Drafts, saved workflows, task files, and runs are kept.',
      okText: 'Delete',
      okButtonProps: { danger: true },
      onOk: async () => {
        setSessionActionId(session.id);
        try {
          const result = await api.deleteAgentSession(session.id, { workspaceId, workspaceDir });
          setSessions(result.sessions || []);
          if (activeSessionId === session.id) {
            const nextSessionId = result.sessions?.[0]?.id || '';
            setActiveSessionId(nextSessionId);
            setMessages([]);
            setEvents([]);
            setDrafts([]);
            setPendingActions([]);
          }
          message.success('Session deleted');
        } catch (error: any) {
          message.error(error.response?.data?.error || 'Failed to delete session');
        } finally {
          setSessionActionId('');
        }
      },
    });
  };

  const exportSession = async (session: WorkspaceAgentSession) => {
    setSessionActionId(session.id);
    try {
      const result = await api.exportAgentSession(session.id, { workspaceId, workspaceDir });
      const blob = new Blob([JSON.stringify(result.export, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      const safeTitle = String(session.title || session.id)
        .trim()
        .replace(/[^a-zA-Z0-9_.-]+/g, '-')
        .replace(/^-+|-+$/g, '')
        .slice(0, 80) || 'workspace-agent-session';
      anchor.href = url;
      anchor.download = `${safeTitle}.agent-session.json`;
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      URL.revokeObjectURL(url);
      message.success('Session export downloaded');
    } catch (error: any) {
      message.error(error.response?.data?.error || 'Failed to export session');
    } finally {
      setSessionActionId('');
    }
  };

  const setPendingActionError = (actionId: string, error: string) => {
    setPendingActions((items) => items.map((item) => (
      item.id === actionId ? { ...item, error } : item
    )));
  };

  const executeSaveDraft = async (draft: WorkspaceAgentDraft, actionId: string) => {
    setProcessingActionId(actionId);
    try {
      const result = await api.saveAgentDraft(draft.id, {
        workspaceId,
        workspaceDir,
        confirmed: true,
        workflowId,
      });
      setWorkspaceContext(result);
      setCurrentWorkspaceWorkflowPath(result.relativePath);
      setWorkflowId(result.workflow.id);
      setWorkflowName(result.workflow.name);
      setNodes(result.workflow.nodes || []);
      setEdges(result.workflow.edges || []);
      setDrafts((items) => items.map((item) => item.id === draft.id ? result.draft : item));
      const refreshed = await api.getWorkspaceWorkflows(result.workspaceDir);
      setWorkspaceWorkflows(refreshed.workflows || []);
      cancelPendingAction(actionId);
      message.success(`Saved ${result.relativePath}`);
    } catch (error: any) {
      const errorText = error.response?.data?.error || 'Failed to save draft';
      setPendingActionError(actionId, errorText);
      message.error(errorText);
    } finally {
      setProcessingActionId('');
    }
  };

  const executeRunDraft = async (draft: WorkspaceAgentDraft, actionId: string) => {
    setProcessingActionId(actionId);
    try {
      const result = await api.runAgentDraft(draft.id, {
        workspaceId,
        workspaceDir,
        confirmed: true,
      });
      setWorkspaceContext(result);
      setWorkflowId(result.workflow.id);
      setWorkflowName(result.workflow.name);
      setNodes(result.workflow.nodes || []);
      setEdges(result.workflow.edges || []);
      setCurrentWorkspaceWorkflowPath(result.draft.saved?.relativePath || draft.relativePath || null);
      setDrafts((items) => items.map((item) => item.id === draft.id ? result.draft : item));
      setActiveRun(result.run);
      setIsRunning(true);
      onOpenRuns?.(result.runId);
      cancelPendingAction(actionId);
      message.success(`Run started: ${result.runId}`);
    } catch (error: any) {
      const errorText = error.response?.data?.error || 'Failed to run draft';
      setPendingActionError(actionId, errorText);
      message.error(errorText);
    } finally {
      setProcessingActionId('');
    }
  };

  const approvePendingAction = async (action: PendingAgentAction, draft: WorkspaceAgentDraft) => {
    if (action.type === 'save') {
      await executeSaveDraft(draft, action.id);
      return;
    }
    await executeRunDraft(draft, action.id);
  };

  const promoteEvidenceArtifact = async (run: any, artifact: any, key: string) => {
    const artifactPath = artifact.path || artifact.name || artifact.sha256;
    if (!workspaceDir || !artifactPath) {
      message.warning('Workspace or artifact path is not available');
      return;
    }
    setPromotingArtifactKey(key);
    try {
      const result = await api.promoteArtifactToWorkspaceFile({
        workspaceId,
        workspaceDir,
        artifact,
        targetPath: artifactPath,
        runId: run.runId,
        taskId: artifact.taskId || artifact.task_id || artifact.nodeId || artifact.node_id,
        overwrite: true,
      });
      message.success(`Added to Workspace Files: ${result.file?.relativePath || artifactPath}`);
    } catch (error: any) {
      message.error(error.response?.data?.error || 'Failed to add artifact to Workspace Files');
    } finally {
      setPromotingArtifactKey('');
    }
  };

  const renderRunEvidence = (run: any, key: string) => {
    const artifacts = uniqueAgentArtifacts(run?.artifacts || []);
    const failedNodes = (run?.nodes || []).filter((node: any) => (
      node.status === 'failed' || node.error
    ));
    return (
      <div className="workspace-agent-run-evidence" key={key}>
        <div className="workspace-agent-run-title">
          <Space size={6} wrap>
            <Text strong>{run.workflowName || 'Workflow run'}</Text>
            <Tag color={draftRunTagColor(String(run.status || ''))}>{run.status || 'unknown'}</Tag>
            <Tag>{run.kind || 'run'}</Tag>
          </Space>
          {onOpenRuns && run.runId && (
            <Button size="small" icon={<Eye size={14} />} onClick={() => onOpenRuns(run.runId)}>
              Open
            </Button>
          )}
        </div>
        <div className="workspace-agent-run-meta">
          <Text type="secondary">run {shortAgentValue(run.runId)}</Text>
          {run.taskCounts?.total !== undefined && (
            <Tag>{run.taskCounts.completed || 0}/{run.taskCounts.total || 0} tasks</Tag>
          )}
          {failedNodes.length > 0 && <Tag color="red">{failedNodes.length} failed</Tag>}
          {artifacts.length > 0 && <Tag color="green">{artifacts.length} artifacts</Tag>}
        </div>
        {run.finalResult?.answer && (
          <Text className="workspace-agent-run-answer">{run.finalResult.answer}</Text>
        )}
        {run.guidance?.[0]?.suggestion && (
          <Alert
            type={failedNodes.length > 0 ? 'warning' : 'info'}
            showIcon
            message={run.guidance[0].stage || 'guidance'}
            description={run.guidance[0].suggestion}
          />
        )}
        {artifacts.length > 0 && (
          <div className="workspace-agent-artifact-list">
            {artifacts.slice(0, 6).map((artifact: any, index: number) => {
              const href = artifactHref(artifact);
              const name = artifact.path || artifact.name || artifact.sha256 || `artifact-${index + 1}`;
              const artifactKey = `${run.runId || key}:${artifact.taskId || artifact.nodeId || index}:${name}`;
              return (
                <div className="workspace-agent-artifact-item" key={`${name}-${artifact.taskId || index}`}>
                  <div className="workspace-agent-artifact-text">
                    <Text strong>{name}</Text>
                    <Text type="secondary">
                      {[artifact.contentType, formatAgentBytes(artifact.sizeBytes), artifact.description]
                        .filter(Boolean)
                        .join(' · ')}
                    </Text>
                  </div>
                  <div className="workspace-agent-artifact-actions">
                    <Button
                      size="small"
                      icon={<Download size={14} />}
                      href={href || undefined}
                      target="_blank"
                      disabled={!href}
                    >
                      Download
                    </Button>
                    <Button
                      size="small"
                      icon={<Save size={14} />}
                      loading={promotingArtifactKey === artifactKey}
                      onClick={() => promoteEvidenceArtifact(run, artifact, artifactKey)}
                    >
                      Add
                    </Button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    );
  };

  const renderPromotedFileEvidence = (result: any, key: string) => {
    const file = result.file || {};
    const relativePath = file.relativePath || file.path || file.name || '';
    const href = relativePath && (result.workspaceDir || workspaceDir)
      ? api.getWorkspaceFileDownloadUrl(result.workspaceDir || workspaceDir, relativePath)
      : '';
    return (
      <div className="workspace-agent-file-evidence" key={key}>
        <div className="workspace-agent-run-title">
          <Space size={6} wrap>
            <FileText size={14} />
            <Text strong>Workspace File</Text>
            <Tag color="green">added</Tag>
          </Space>
          <Button
            size="small"
            icon={<Download size={14} />}
            href={href || undefined}
            target="_blank"
            disabled={!href}
          >
            Download
          </Button>
        </div>
        <div className="workspace-agent-file-meta">
          <Text strong>{relativePath || file.name || 'workspace file'}</Text>
          <Space size={6} wrap>
            {formatAgentBytes(file.size) && <Tag>{formatAgentBytes(file.size)}</Tag>}
            {result.workspaceManifestVersion !== undefined && <Tag>manifest {result.workspaceManifestVersion}</Tag>}
            {file.updatedAt && <Text type="secondary">{file.updatedAt}</Text>}
          </Space>
        </div>
        {result.nextStep && (
          <Text type="secondary" className="workspace-agent-run-answer">{result.nextStep}</Text>
        )}
      </div>
    );
  };

  const renderReadFileEvidence = (result: any, key: string) => {
    const relativePath = result.relativePath || result.path || '';
    const href = relativePath && (result.workspaceDir || workspaceDir)
      ? api.getWorkspaceFileDownloadUrl(result.workspaceDir || workspaceDir, relativePath)
      : '';
    const content = result.binary ? '' : String(result.content || '');
    const preview = content.length > 1600 ? `${content.slice(0, 1600)}\n...` : content;
    return (
      <div className="workspace-agent-file-read-evidence" key={key}>
        <div className="workspace-agent-run-title">
          <Space size={6} wrap>
            <Eye size={14} />
            <Text strong>Workspace File Read</Text>
            {result.binary ? <Tag color="orange">binary</Tag> : <Tag color="blue">text</Tag>}
            {result.truncated && <Tag color="gold">truncated</Tag>}
          </Space>
          <Button
            size="small"
            icon={<Download size={14} />}
            href={href || undefined}
            target="_blank"
            disabled={!href}
          >
            Download
          </Button>
        </div>
        <div className="workspace-agent-file-meta">
          <Text strong>{relativePath || 'workspace file'}</Text>
          <Space size={6} wrap>
            {formatAgentBytes(result.size) && <Tag>{formatAgentBytes(result.size)}</Tag>}
            {result.encoding && <Tag>{result.encoding}</Tag>}
            {result.updatedAt && <Text type="secondary">{result.updatedAt}</Text>}
          </Space>
        </div>
        {result.binary ? (
          <Text type="secondary">Binary content is not previewed in chat.</Text>
        ) : preview ? (
          <pre className="workspace-agent-file-preview">{preview}</pre>
        ) : (
          <Text type="secondary">No preview content returned.</Text>
        )}
      </div>
    );
  };

  const renderReadWorkflowEvidence = (result: any, key: string) => {
    const workflow = result.workflow || {};
    const taskDefinitions = result.taskDefinitions || [];
    return (
      <div className="workspace-agent-asset-evidence" key={key}>
        <div className="workspace-agent-run-title">
          <Space size={6} wrap>
            <FileText size={14} />
            <Text strong>Saved Workflow Read</Text>
            <Tag color="blue">{workflow.nodes?.length || 0} nodes</Tag>
            <Tag>{workflow.edges?.length || 0} edges</Tag>
            <Tag>{taskDefinitions.length} tasks</Tag>
          </Space>
        </div>
        <div className="workspace-agent-file-meta">
          <Text strong>{result.relativePath}</Text>
          <Space size={6} wrap>
            {workflow.name && <Tag color="purple">{workflow.name}</Tag>}
            {formatAgentBytes(result.size) && <Tag>{formatAgentBytes(result.size)}</Tag>}
            {result.updatedAt && <Text type="secondary">{result.updatedAt}</Text>}
          </Space>
        </div>
        {taskDefinitions.length > 0 && (
          <Collapse
            size="small"
            ghost
            items={[{
              key: 'tasks',
              label: `Task definitions (${taskDefinitions.length})`,
              children: (
                <div className="workspace-agent-preview">
                  {taskDefinitions.slice(0, 4).map((task: any, index: number) => (
                    <div className="workspace-agent-preview-row" key={`${task.relativePath || task.functionName || index}`}>
                      <Text strong>{task.relativePath || task.functionName || `task-${index + 1}`}</Text>
                      <Text type="secondary">{task.functionName || task.displayName || ''}</Text>
                      {task.code && (
                        <pre className="workspace-agent-code-preview">
                          {String(task.code).slice(0, 1200)}
                          {task.truncated ? '\n...' : ''}
                        </pre>
                      )}
                    </div>
                  ))}
                  {taskDefinitions.length > 4 && (
                    <Text type="secondary">Showing 4 of {taskDefinitions.length} task definitions.</Text>
                  )}
                </div>
              ),
            }]}
          />
        )}
      </div>
    );
  };

  const renderReadTaskEvidence = (result: any, key: string) => {
    return (
      <div className="workspace-agent-asset-evidence" key={key}>
        <div className="workspace-agent-run-title">
          <Space size={6} wrap>
            <Wrench size={14} />
            <Text strong>Workspace Task Read</Text>
            <Tag color="blue">python</Tag>
            {result.truncated && <Tag color="gold">truncated</Tag>}
          </Space>
        </div>
        <div className="workspace-agent-file-meta">
          <Text strong>{result.relativePath}</Text>
          <Space size={6} wrap>
            {formatAgentBytes(result.size) && <Tag>{formatAgentBytes(result.size)}</Tag>}
            {result.updatedAt && <Text type="secondary">{result.updatedAt}</Text>}
          </Space>
        </div>
        {result.code ? (
          <pre className="workspace-agent-code-preview">
            {String(result.code).slice(0, 2000)}
            {result.truncated ? '\n...' : ''}
          </pre>
        ) : (
          <Text type="secondary">No code preview returned.</Text>
        )}
      </div>
    );
  };

  const renderInventoryEvidence = (result: any, key: string) => {
    const workflows = result.workflows || [];
    const tasks = result.tasks || [];
    const files = result.files || [];
    const skills = result.skills || [];
    const rows = [
      ['Workflows', workflows, (item: any) => item.relativePath || item.name],
      ['Tasks', tasks, (item: any) => item.relativePath || item.functionName || item.displayName],
      ['Files', files, (item: any) => item.relativePath || item.name],
      ['Skills', skills, (item: any) => item.path || item.name],
    ] as Array<[string, any[], (item: any) => string]>;
    return (
      <div className="workspace-agent-inventory-evidence" key={key}>
        <div className="workspace-agent-run-title">
          <Space size={6} wrap>
            <Eye size={14} />
            <Text strong>Workspace Inventory</Text>
            {workflows.length > 0 && <Tag color="blue">{workflows.length} workflows</Tag>}
            {tasks.length > 0 && <Tag color="purple">{tasks.length} tasks</Tag>}
            {files.length > 0 && <Tag color="cyan">{files.length} files</Tag>}
            {skills.length > 0 && <Tag color="green">{skills.length} skills</Tag>}
          </Space>
        </div>
        <div className="workspace-agent-inventory-grid">
          {rows.filter(([, items]) => items.length > 0).map(([label, items, pick]) => (
            <div className="workspace-agent-inventory-section" key={label}>
              <Text strong>{label}</Text>
              {items.slice(0, 5).map((item: any, index: number) => (
                <Text type="secondary" key={`${label}-${index}`}>
                  {pick(item)}
                </Text>
              ))}
              {items.length > 5 && <Text type="secondary">+{items.length - 5} more</Text>}
            </div>
          ))}
        </div>
        {(result.filesTruncated || result.taskErrors?.length || result.skillErrors?.length) && (
          <Space size={6} wrap>
            {result.filesTruncated && <Tag color="gold">files truncated</Tag>}
            {result.taskErrors?.length > 0 && <Tag color="red">task errors {result.taskErrors.length}</Tag>}
            {result.skillErrors?.length > 0 && <Tag color="red">skill errors {result.skillErrors.length}</Tag>}
          </Space>
        )}
      </div>
    );
  };

  const renderFileListEvidence = (result: any, key: string) => {
    const files = result.files || [];
    return (
      <div className="workspace-agent-inventory-evidence" key={key}>
        <div className="workspace-agent-run-title">
          <Space size={6} wrap>
            <FileText size={14} />
            <Text strong>Workspace Files Listed</Text>
            <Tag color="cyan">{files.length} shown</Tag>
            {Number.isFinite(Number(result.totalEntries)) && <Tag>{result.totalEntries} total</Tag>}
            {result.truncated && <Tag color="gold">truncated</Tag>}
          </Space>
        </div>
        <div className="workspace-agent-file-meta">
          <Text strong>{result.path ? `files/${result.path}` : 'files/'}</Text>
        </div>
        <div className="workspace-agent-inventory-grid">
          {files.slice(0, 8).map((file: any, index: number) => (
            <div className="workspace-agent-file-list-row" key={`${file.relativePath || file.name || index}`}>
              <Space size={6} wrap>
                <Tag color={file.type === 'directory' ? 'blue' : undefined}>{file.type || 'file'}</Tag>
                <Text>{file.relativePath || file.name}</Text>
              </Space>
              <Text type="secondary">
                {[formatAgentBytes(file.size), file.updatedAt].filter(Boolean).join(' · ')}
              </Text>
            </div>
          ))}
          {files.length > 8 && <Text type="secondary">Showing 8 of {files.length} entries.</Text>}
        </div>
      </div>
    );
  };

  const renderCurrentWorkflowEvidence = (workflow: any, key: string) => {
    const nodesList = workflow.nodes || [];
    const edgesList = workflow.edges || [];
    return (
      <div className="workspace-agent-asset-evidence" key={key}>
        <div className="workspace-agent-run-title">
          <Space size={6} wrap>
            <Eye size={14} />
            <Text strong>Current Workflow Read</Text>
            <Tag color="blue">{nodesList.length} nodes</Tag>
            <Tag>{edgesList.length} edges</Tag>
          </Space>
        </div>
        <div className="workspace-agent-file-meta">
          <Text strong>{workflow.name || 'Untitled workflow'}</Text>
          <Space size={6} wrap>
            {workflow.relativePath && <Tag>{workflow.relativePath}</Tag>}
            {workflow.workflowId && <Tag>{shortAgentValue(workflow.workflowId)}</Tag>}
          </Space>
        </div>
        {nodesList.length > 0 ? (
          <div className="workspace-agent-inventory-grid">
            {nodesList.slice(0, 6).map((node: any, index: number) => {
              const data = node.data || {};
              return (
                <div className="workspace-agent-file-list-row" key={node.id || index}>
                  <Space size={6} wrap>
                    <Tag>{data.nodeType || data.category || 'node'}</Tag>
                    <Text>{data.label || node.id || `node-${index + 1}`}</Text>
                  </Space>
                  <Text type="secondary">
                    {[data.taskPath, data.functionName].filter(Boolean).join(' · ')}
                  </Text>
                </div>
              );
            })}
            {nodesList.length > 6 && <Text type="secondary">Showing 6 of {nodesList.length} nodes.</Text>}
          </div>
        ) : (
          <Text type="secondary">The current canvas has no nodes.</Text>
        )}
      </div>
    );
  };

  if (!open) {
    return (
      <Tooltip title="Workspace Agent">
        <Button
          className="workspace-agent-float"
          icon={<PanelRightOpen size={18} />}
          onClick={onToggle}
        />
      </Tooltip>
    );
  }

  return (
    <aside className="workspace-agent-panel">
      <div className="workspace-agent-header">
        <Space size={8}>
          <Bot size={18} />
          <Text strong>Workspace Agent</Text>
        </Space>
        <Tooltip title="Collapse">
          <Button size="small" icon={<PanelRightClose size={16} />} onClick={onToggle} />
        </Tooltip>
      </div>

      {!llmReady && (
        <Alert
          type="warning"
          showIcon
          message="LLM settings missing"
          description="Open the ReAct runner once and save base URL, key, and model."
          style={{ marginBottom: 8 }}
        />
      )}

      <div className="workspace-agent-sessions">
        <Tooltip title="New session">
          <Button
            size="small"
            className="workspace-agent-session-icon"
            aria-label="New Workspace Agent session"
            icon={<Plus size={14} />}
            disabled={!workspaceReady || !workspaceDir}
            onClick={async () => {
              if (!workspaceReady || !workspaceDir) return;
              const created = await api.createAgentSession({ workspaceId, workspaceDir, title: 'Workspace Agent' });
              setActiveSessionId(created.session.id);
              setMessages([]);
              setEvents([]);
              setDrafts([]);
              await refreshSessions();
              setActiveSessionId(created.session.id);
            }}
          />
        </Tooltip>
        <div className="workspace-agent-session-main">
          {editingSessionId && activeSession?.id === editingSessionId ? (
            <Input
              size="small"
              className="workspace-agent-session-title-input"
              value={editingSessionTitle}
              autoFocus
              disabled={sessionActionId === editingSessionId}
              onChange={(event) => setEditingSessionTitle(event.target.value)}
              onBlur={saveSessionTitle}
              onPressEnter={saveSessionTitle}
              onKeyDown={(event) => {
                if (event.key === 'Escape') {
                  setEditingSessionId('');
                  setEditingSessionTitle('');
                }
              }}
            />
          ) : (
            <Select
              size="small"
              className="workspace-agent-session-select"
              value={activeSessionId || undefined}
              placeholder={loadingSessions ? 'Loading sessions' : 'No sessions'}
              loading={loadingSessions}
              disabled={loadingSessions || sessions.length === 0}
              optionLabelProp="label"
              onChange={(sessionId) => {
                setActiveSessionId(sessionId);
                setEvents([]);
                setDrafts([]);
              }}
              options={sessions.map((session) => ({
                value: session.id,
                label: session.title || session.id,
              }))}
            />
          )}
        </div>
        <div className="workspace-agent-session-actions">
          <Tooltip title="Rename">
            <Button
              size="small"
              type="text"
              className="workspace-agent-session-icon"
              aria-label={`Rename ${activeSession?.title || activeSession?.id || 'current session'}`}
              icon={<Pencil size={12} />}
              disabled={!activeSession || Boolean(editingSessionId)}
              loading={Boolean(activeSession && sessionActionId === activeSession.id)}
              onClick={() => activeSession && startRenameSession(activeSession)}
            />
          </Tooltip>
          <Tooltip title="Export">
            <Button
              size="small"
              type="text"
              className="workspace-agent-session-icon"
              aria-label={`Export ${activeSession?.title || activeSession?.id || 'current session'}`}
              icon={<Download size={12} />}
              disabled={!activeSession || Boolean(editingSessionId)}
              loading={Boolean(activeSession && sessionActionId === activeSession.id)}
              onClick={() => activeSession && exportSession(activeSession)}
            />
          </Tooltip>
          <Tooltip title="Delete chat history">
            <Button
              size="small"
              type="text"
              danger
              className="workspace-agent-session-icon"
              aria-label={`Delete ${activeSession?.title || activeSession?.id || 'current session'}`}
              icon={<Trash2 size={12} />}
              disabled={!activeSession || Boolean(editingSessionId)}
              loading={Boolean(activeSession && sessionActionId === activeSession.id)}
              onClick={() => activeSession && deleteSession(activeSession)}
            />
          </Tooltip>
        </div>
      </div>

      <div className="workspace-agent-body" ref={scrollRef}>
        {messages.length === 0 && visibleDrafts.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="Ask the agent to inspect, draft, validate, save, or run a workflow."
          />
        ) : (
          <>
            <List
              size="small"
              dataSource={messages.filter((item) => item.role !== 'tool' || hasToolEvidence(item))}
              renderItem={(item) => {
                const runEvidence = inspectRunResults(item);
                const promotedFiles = promotedFileResults(item);
                const readFiles = readWorkspaceFileResults(item);
                const readWorkflows = readWorkspaceWorkflowResults(item);
                const readTasks = readWorkspaceTaskResults(item);
                const inventoryResults = workspaceInventoryResults(item);
                const fileLists = workspaceFileListResults(item);
                const currentWorkflowReads = currentWorkflowResults(item);
                const text = messageText(item);
                return (
                  <List.Item className={`workspace-agent-message workspace-agent-message-${item.role}`}>
                    <div>
                      <Text type="secondary">{item.role === 'tool' && hasToolEvidence(item) ? 'evidence' : item.role}</Text>
                      {text ? (
                        <Paragraph style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}>
                          {text}
                        </Paragraph>
                      ) : !hasToolEvidence(item) ? (
                        <Paragraph style={{ marginBottom: 0 }}>
                          <Text type="secondary">No text content</Text>
                        </Paragraph>
                      ) : null}
                      {runEvidence.map((run, index) => renderRunEvidence(run, `${item.id}-${index}`))}
                      {promotedFiles.map((result, index) => renderPromotedFileEvidence(result, `${item.id}-file-${index}`))}
                      {readFiles.map((result, index) => renderReadFileEvidence(result, `${item.id}-read-file-${index}`))}
                      {readWorkflows.map((result, index) => renderReadWorkflowEvidence(result, `${item.id}-read-workflow-${index}`))}
                      {readTasks.map((result, index) => renderReadTaskEvidence(result, `${item.id}-read-task-${index}`))}
                      {inventoryResults.map((result, index) => renderInventoryEvidence(result, `${item.id}-inventory-${index}`))}
                      {fileLists.map((result, index) => renderFileListEvidence(result, `${item.id}-file-list-${index}`))}
                      {currentWorkflowReads.map((result, index) => renderCurrentWorkflowEvidence(result, `${item.id}-current-workflow-${index}`))}
                    </div>
                  </List.Item>
                );
              }}
            />

            {activeRunId && (
              <div className="workspace-agent-live-status">
                <Space size={8} wrap>
                  <Bot size={14} />
                  <Text strong>{currentAgentRunLabel(events)}</Text>
                  {visibleAgentEvents(events).slice(-1).map((event) => (
                    <Tag key={event.seq}>{eventLabel(event)}</Tag>
                  ))}
                </Space>
                <Text type="secondary">The final response will appear here when this step finishes.</Text>
              </div>
            )}

            {visibleAgentEvents(events).length > 0 && (
              <div className="workspace-agent-events">
                {visibleAgentEvents(events).map((event) => (
                  <Tag key={event.seq} icon={event.type.includes('tool') ? <Wrench size={12} /> : undefined}>
                    {eventLabel(event)}
                  </Tag>
                ))}
              </div>
            )}

            {(pendingActions.length > 0 || visibleDrafts.length > 0) && (
              <Collapse
                className="workspace-agent-workspace-collapse"
                size="small"
                defaultActiveKey={pendingActions.length > 0 ? ['actions'] : []}
                items={[
                  ...(pendingActions.length > 0 ? [{
                    key: 'actions',
                    label: `Pending choices (${pendingActions.length})`,
                    children: (
                      <div className="pending-action-list">
                        {pendingActions.map((action) => {
                          const draft = visibleDrafts.find((item) => item.id === action.draftId);
                          if (!draft) return null;
                          const diff = buildDraftDiff(draft, currentWorkflow);
                          const isRun = action.type === 'run';
                          const loading = processingActionId === action.id;
                          return (
                            <PendingActionCard
                              key={action.id}
                              actionLabel={isRun ? 'Run' : 'Save'}
                              actionColor={isRun ? 'blue' : 'cyan'}
                              subject={draft.name}
                              description={isRun
                                ? `Save ${draft.relativePath} and start a workflow run.`
                                : `Write workflow and task files to ${draft.relativePath}.`}
                              tags={[
                                <Tag>{draft.workflow?.nodes?.length || 0} nodes</Tag>,
                                <Tag>{draft.workflow?.edges?.length || 0} edges</Tag>,
                                <Tag>{draft.taskDefinitions?.length || 0} tasks</Tag>,
                                <Tag color={diff.hasChanges ? 'gold' : 'default'}>
                                  {diff.hasChanges ? 'canvas changes' : 'no canvas diff'}
                                </Tag>,
                              ]}
                              error={action.error}
                              approveLabel={isRun ? 'Allow Run' : 'Allow Save'}
                              approveIcon={isRun ? <Play size={14} /> : <Save size={14} />}
                              loading={loading}
                              onApprove={() => approvePendingAction(action, draft)}
                              onDeny={() => cancelPendingAction(action.id)}
                            />
                          );
                        })}
                      </div>
                    ),
                  }] : []),
                  ...(visibleDrafts.length > 0 ? [{
                    key: 'drafts',
                    label: `Drafts (${visibleDrafts.length})`,
                    children: (
                      <div className="workspace-agent-draft-list">
                        {visibleDrafts.map((draft) => (
                          <div className="workspace-agent-draft" key={draft.id}>
                <div className="workspace-agent-draft-title">
                  <Text strong>{draft.name}</Text>
                  <Tag color={draft.validation?.ok ? 'green' : 'gold'}>
                    {draft.validation?.ok ? 'valid' : 'draft'}
                  </Tag>
                </div>
                <Text type="secondary">{draft.relativePath}</Text>
                <div className="workspace-agent-draft-meta">
                  <Tag>rev {draft.revision || 1}</Tag>
                  <Tag>{draft.workflow?.nodes?.length || 0} nodes</Tag>
                  <Tag>{draft.workflow?.edges?.length || 0} edges</Tag>
                  <Tag>{draft.taskDefinitions?.length || 0} tasks</Tag>
                  {draft.saved?.relativePath && <Tag color="cyan">saved</Tag>}
                  {draft.run?.status && (
                    <Tag color={draftRunTagColor(String(draft.run.status))}>
                      run {String(draft.run.status)}
                    </Tag>
                  )}
                </div>
                {draft.fixContext && (
                  <Alert
                    type="warning"
                    showIcon
                    style={{ marginTop: 8 }}
                    message={`Fix draft for run ${String(draft.fixContext.runId || '').slice(0, 8)}`}
                    description={
                      <Space size={6} wrap>
                        <Tag color="red">{(draft.fixContext.failedNodes || []).length} failed nodes</Tag>
                        {draft.fixContext.sourceWorkflow?.relativePath && (
                          <Tag>{draft.fixContext.sourceWorkflow.relativePath}</Tag>
                        )}
                        {draft.fixContext.guidance?.[0]?.stage && (
                          <Tag color="gold">{draft.fixContext.guidance[0].stage}</Tag>
                        )}
                      </Space>
                    }
                  />
                )}
                {(() => {
                  const diff = buildDraftDiff(draft, currentWorkflow);
                  return (
                    <div className="workspace-agent-diff">
                      <Tag color={diff.addedNodes.length ? 'green' : 'default'}>+ nodes {diff.addedNodes.length}</Tag>
                      <Tag color={diff.changedNodes.length ? 'blue' : 'default'}>~ nodes {diff.changedNodes.length}</Tag>
                      <Tag color={diff.removedNodes.length ? 'red' : 'default'}>- nodes {diff.removedNodes.length}</Tag>
                      <Tag color={diff.addedEdges.length ? 'green' : 'default'}>+ edges {diff.addedEdges.length}</Tag>
                      <Tag color={diff.taskFiles.length ? 'purple' : 'default'}>task files {diff.taskFiles.length}</Tag>
                    </div>
                  );
                })()}
                {draft.validation && (
                  <div className={`workspace-agent-validation-result ${draft.validation.ok ? 'workspace-agent-validation-ok' : 'workspace-agent-validation-error'}`}>
                    <Space size={6} wrap>
                      <CheckCircle2 size={14} />
                      <Text strong>Validation</Text>
                      <Tag color={draft.validation.ok ? 'green' : 'red'}>
                        {draft.validation.ok ? 'valid' : 'invalid'}
                      </Tag>
                      <Tag>{draft.validation.nodeCount ?? draft.workflow?.nodes?.length ?? 0} nodes</Tag>
                      <Tag>{draft.validation.edgeCount ?? draft.workflow?.edges?.length ?? 0} edges</Tag>
                      <Tag>{draft.validation.taskDefinitionCount ?? draft.taskDefinitions?.length ?? 0} tasks</Tag>
                      {(draft.validation.warnings || []).length > 0 && (
                        <Tag color="gold">{draft.validation.warnings.length} warnings</Tag>
                      )}
                    </Space>
                    {draft.validation.validatedAt && (
                      <Text type="secondary">{draft.validation.validatedAt}</Text>
                    )}
                    {(draft.validation.errors || []).length > 0 && (
                      <div className="workspace-agent-validation-list">
                        {(draft.validation.errors || []).slice(0, 6).map((item: string, index: number) => (
                          <Text type="danger" key={`error-${index}`}>{item}</Text>
                        ))}
                        {draft.validation.errors.length > 6 && (
                          <Text type="secondary">+{draft.validation.errors.length - 6} more errors</Text>
                        )}
                      </div>
                    )}
                    {(draft.validation.warnings || []).length > 0 && (
                      <div className="workspace-agent-validation-list">
                        {(draft.validation.warnings || []).slice(0, 6).map((item: string, index: number) => (
                          <Text type="warning" key={`warning-${index}`}>{item}</Text>
                        ))}
                        {draft.validation.warnings.length > 6 && (
                          <Text type="secondary">+{draft.validation.warnings.length - 6} more warnings</Text>
                        )}
                      </div>
                    )}
                  </div>
                )}
                {draft.run?.error ? (
                  <Alert
                    type="error"
                    showIcon
                    message={String(draft.run.error)}
                    style={{ marginTop: 8 }}
                  />
                ) : null}
                {(draft.saved || draft.run) && (
                  <div className="workspace-agent-action-result">
                    {draft.saved && (
                      <div className="workspace-agent-action-result-row">
                        <div className="workspace-agent-run-title">
                          <Space size={6} wrap>
                            <Save size={14} />
                            <Text strong>Saved Workflow</Text>
                            {draft.saved.workspaceManifestVersion !== undefined && (
                              <Tag>manifest {draft.saved.workspaceManifestVersion}</Tag>
                            )}
                          </Space>
                          <Button
                            size="small"
                            icon={<Eye size={14} />}
                            loading={openingWorkflowPath === (draft.saved.relativePath || draft.relativePath)}
                            onClick={() => openSavedWorkflow(draft)}
                          >
                            Open
                          </Button>
                        </div>
                        <Text type="secondary">{draft.saved.relativePath || draft.relativePath}</Text>
                        <Space size={6} wrap>
                          {draft.saved.workflowId && <Tag>{shortAgentValue(draft.saved.workflowId)}</Tag>}
                          {draft.saved.savedAt && <Text type="secondary">{draft.saved.savedAt}</Text>}
                          {draft.saved.importedTaskDefinitions?.imported?.length > 0 && (
                            <Tag color="purple">
                              imported {draft.saved.importedTaskDefinitions.imported.length} tasks
                            </Tag>
                          )}
                        </Space>
                      </div>
                    )}
                    {draft.run && (() => {
                      const run = draft.run;
                      const runId = run.runId || run.id || run.workflowRunId || '';
                      return (
                        <div className="workspace-agent-action-result-row">
                          <div className="workspace-agent-run-title">
                            <Space size={6} wrap>
                              <Play size={14} />
                              <Text strong>Workflow Run</Text>
                              <Tag color={draftRunTagColor(String(run.status || ''))}>
                                {String(run.status || 'started')}
                              </Tag>
                            </Space>
                            {onOpenRuns && runId && (
                              <Button
                                size="small"
                                icon={<Eye size={14} />}
                                onClick={() => onOpenRuns(runId)}
                              >
                                Open
                              </Button>
                            )}
                          </div>
                          <Text type="secondary">{runId}</Text>
                          <Space size={6} wrap>
                            {run.startedAt && <Text type="secondary">started {run.startedAt}</Text>}
                            {run.finishedAt && <Text type="secondary">finished {run.finishedAt}</Text>}
                          </Space>
                        </div>
                      );
                    })()}
                  </div>
                )}
                <Collapse
                  size="small"
                  ghost
                  style={{ marginTop: 8 }}
                  items={[
                    ...(draft.fixContext ? [{
                      key: 'fix-context',
                      label: 'Fix evidence',
                      children: (
                        <div className="workspace-agent-preview">
                          <div className="workspace-agent-preview-row">
                            <Text strong>Failed run</Text>
                            <Text type="secondary">{draft.fixContext.runId}</Text>
                          </div>
                          {draft.fixContext.sourceWorkflow?.relativePath && (
                            <div className="workspace-agent-preview-row">
                              <Text strong>Source workflow</Text>
                              <Text type="secondary">{draft.fixContext.sourceWorkflow.relativePath}</Text>
                            </div>
                          )}
                          {(draft.fixContext.failedNodes || []).map((node: any) => (
                            <div key={node.nodeId || node.taskId} className="workspace-agent-preview-row">
                              <Text strong>{node.label || node.nodeId}</Text>
                              <Text type="secondary">
                                {node.taskPath || node.taskId || ''} {node.error || ''}
                              </Text>
                            </div>
                          ))}
                          {(draft.fixContext.guidance || []).slice(0, 3).map((item: any) => (
                            <Alert
                              key={`${item.stage}-${item.issue}`}
                              type="info"
                              showIcon
                              message={item.stage || 'guidance'}
                              description={item.suggestion || item.issue}
                            />
                          ))}
                        </div>
                      ),
                    }] : []),
                    {
                      key: 'diff',
                      label: 'Diff',
                      children: (() => {
                        const diff = buildDraftDiff(draft, currentWorkflow);
                        return (
                          <div className="workspace-agent-preview">
                            {!diff.hasChanges && <Text type="secondary">No visible change from the current canvas.</Text>}
                            {diff.addedNodes.length > 0 && (
                              <div className="workspace-agent-preview-row">
                                <Text strong>Added nodes</Text>
                                <Text type="secondary">{diff.addedNodes.map((node: any) => node.data?.label || node.id).join(', ')}</Text>
                              </div>
                            )}
                            {diff.changedNodes.length > 0 && (
                              <div className="workspace-agent-preview-row">
                                <Text strong>Changed nodes</Text>
                                <Text type="secondary">{diff.changedNodes.map((node: any) => node.data?.label || node.id).join(', ')}</Text>
                              </div>
                            )}
                            {diff.removedNodes.length > 0 && (
                              <div className="workspace-agent-preview-row">
                                <Text strong>Removed nodes</Text>
                                <Text type="secondary">{diff.removedNodes.map((node: any) => node.data?.label || node.id).join(', ')}</Text>
                              </div>
                            )}
                            {(diff.addedEdges.length > 0 || diff.changedEdges.length > 0 || diff.removedEdges.length > 0) && (
                              <div className="workspace-agent-preview-row">
                                <Text strong>Edges</Text>
                                <Text type="secondary">
                                  +{diff.addedEdges.length} ~{diff.changedEdges.length} -{diff.removedEdges.length}
                                </Text>
                              </div>
                            )}
                            {diff.taskFiles.length > 0 && (
                              <div className="workspace-agent-preview-row">
                                <Text strong>Task files to materialize</Text>
                                <Text type="secondary">
                                  {diff.taskFiles.map((task) => `${task.relativePath} (${task.bytes} chars)`).join(', ')}
                                </Text>
                              </div>
                            )}
                          </div>
                        );
                      })(),
                    },
                    {
                      key: 'workflow',
                      label: 'Workflow preview',
                      children: (
                        <div className="workspace-agent-preview">
                          {(draft.workflow?.nodes || []).map((node) => (
                            <div key={node.id} className="workspace-agent-preview-row">
                              <Text strong>{node.data?.label || node.id}</Text>
                              <Text type="secondary">
                                {node.data?.category} {node.data?.functionName || node.data?.taskRef || ''}
                              </Text>
                            </div>
                          ))}
                          {(draft.workflow?.edges || []).length > 0 && (
                            <Text type="secondary">
                              Edges: {(draft.workflow.edges || []).map((edge) => `${edge.source}->${edge.target}`).join(', ')}
                            </Text>
                          )}
                        </div>
                      ),
                    },
                    {
                      key: 'tasks',
                      label: `Task files (${draft.taskDefinitions?.length || 0})`,
                      children: (
                        <div className="workspace-agent-preview">
                          {(draft.taskDefinitions || []).length === 0 ? (
                            <Text type="secondary">No inline task files.</Text>
                          ) : (draft.taskDefinitions || []).map((task: any) => (
                            <div key={`${task.relativePath}-${task.functionName}`} className="workspace-agent-preview-row">
                              <Text strong>{task.relativePath}</Text>
                              <Text type="secondary">{task.functionName}</Text>
                              {task.code && (
                                <pre className="workspace-agent-code-preview">{String(task.code).slice(0, 900)}</pre>
                              )}
                            </div>
                          ))}
                        </div>
                      ),
                    },
                  ]}
                />
                <Space size={6} wrap style={{ marginTop: 8 }}>
                  <Button size="small" icon={<Eye size={14} />} onClick={() => previewDraft(draft)}>Preview</Button>
                  {draft.saved && (
                    <Button
                      size="small"
                      icon={<Eye size={14} />}
                      loading={openingWorkflowPath === (draft.saved.relativePath || draft.relativePath)}
                      onClick={() => openSavedWorkflow(draft)}
                    >
                      Open Saved
                    </Button>
                  )}
                  <Button size="small" icon={<CheckCircle2 size={14} />} onClick={() => validateDraft(draft)}>Validate</Button>
                  <Button
                    size="small"
                    icon={<Save size={14} />}
                    onClick={() => queuePendingAction(draft, 'save')}
                    disabled={pendingActions.some((action) => action.id === `save:${draft.id}`)}
                  >
                    Save
                  </Button>
                  <Button
                    size="small"
                    type="primary"
                    icon={<Play size={14} />}
                    onClick={() => queuePendingAction(draft, 'run')}
                    disabled={pendingActions.some((action) => action.id === `run:${draft.id}`)}
                  >
                    Run
                  </Button>
                  <Button
                    size="small"
                    icon={<Trash2 size={14} />}
                    onClick={() => dismissDraft(draft)}
                  >
                    Dismiss
                  </Button>
                </Space>
              </div>
                        ))}
                      </div>
                    ),
                  }] : []),
                ]}
              />
            )}
          </>
        )}
      </div>

      <div className="workspace-agent-input">
        <Input.TextArea
          value={input}
          onChange={(event) => setInput(event.target.value)}
          onPressEnter={(event) => {
            if (!event.shiftKey) {
              event.preventDefault();
              sendMessage();
            }
          }}
          autoSize={{ minRows: 2, maxRows: 5 }}
          placeholder="Ask for a workflow draft..."
          disabled={busy}
        />
        {activeRunId ? (
          <Tooltip title="Stop Workspace Agent run">
            <Button
              danger
              icon={<Square size={15} />}
              onClick={cancelActiveRun}
              loading={cancelingRun}
              aria-label="Stop Workspace Agent run"
            />
          </Tooltip>
        ) : (
          <Button
            type="primary"
            icon={<Send size={15} />}
            onClick={sendMessage}
            loading={busy}
            aria-label="Send"
          />
        )}
      </div>
    </aside>
  );
}
