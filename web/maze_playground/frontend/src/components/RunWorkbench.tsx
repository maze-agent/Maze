import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Alert, Button, Descriptions, Empty, Input, Space, Spin, Switch, Tag, Typography } from 'antd';
import { DoubleLeftOutlined, DoubleRightOutlined, HistoryOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import { api } from '@/api/client';
import { useWorkflowStore } from '@/stores/workflowStore';
import type { UnifiedRunEvent, UnifiedRunSnapshot } from '@/types/workflow';
import { runWorkflowName, runSubmissionOrderTime } from '@/utils/runSnapshot';
import RunGraph, { RunTaskDetails } from './RunGraph';
import Toolbar from './Toolbar';
import WorkbenchShell from './WorkbenchShell';

const activeStatuses = new Set(['created', 'submitted', 'queued', 'running']);
function statusColor(status: string) {
  if (['completed', 'succeeded', 'finalized'].includes(status)) return 'green';
  if (['failed', 'timed_out', 'interrupted'].includes(status)) return 'red';
  if (status === 'running') return 'processing';
  return 'default';
}

function runName(run: UnifiedRunSnapshot) {
  const name = runWorkflowName(run);
  return name === 'Workflow Run' ? `${run.kind === 'dynamic' ? 'Dynamic Run' : 'Run'} ${run.run_id.slice(0, 8)}` : name;
}

export default function RunWorkbench({ runId, onSelectRun, onDesign, onOpenHistory, onOpenCluster, runsInspector, clusterDrawer }: {
  runId: string; onSelectRun: (id: string) => void; onDesign: () => void;
  onOpenHistory: (run?: UnifiedRunSnapshot | null) => void; onOpenCluster: () => void;
  runsInspector: ReactNode; clusterDrawer: ReactNode;
}) {
  const workspaceId = useWorkflowStore((state) => state.workspaceId);
  const [snapshot, setSnapshot] = useState<UnifiedRunSnapshot | null>(null);
  const [events, setEvents] = useState<UnifiedRunEvent[]>([]);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [runs, setRuns] = useState<UnifiedRunSnapshot[]>([]);
  const [query, setQuery] = useState('');
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [showAll, setShowAll] = useState(true);
  const [error, setError] = useState('');
  const [listError, setListError] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [listVersion, setListVersion] = useState(0);
  const refreshRef = useRef<() => void>(() => {});
  const run = snapshot?.run_id === runId ? snapshot : null;

  useEffect(() => {
    let disposed = false;
    let timer: number | undefined;
    let loading = false;
    let lastSeq = 0;
    const collected = new Map<string, UnifiedRunEvent>();
    setSnapshot(null);
    setEvents([]);
    setSelectedTaskId(null);
    setError('');
    async function load() {
      if (disposed || loading) return;
      loading = true;
      window.clearTimeout(timer);
      setRefreshing(true);
      const [snapshotResult, eventResult] = await Promise.allSettled([
        api.getRun(runId), api.getRunEvents(runId, lastSeq),
      ]);
      if (disposed) return;
      let retry = false;
      if (eventResult.status === 'fulfilled') {
        const incoming = eventResult.value.events || [];
        for (const event of incoming) {
          collected.set(event.seq === undefined ? JSON.stringify(event) : String(event.seq), event);
          lastSeq = Math.max(lastSeq, Number(event.seq || 0));
        }
        if (incoming.length) setEvents([...collected.values()]);
      } else retry = true;
      if (snapshotResult.status === 'fulfilled') {
        const next = snapshotResult.value.run;
        setSnapshot(next);
        setError(eventResult.status === 'rejected' ? 'Some run events could not be refreshed. Retrying…' : '');
        retry ||= activeStatuses.has(next.status) || Number(next.last_event_seq ?? next.events?.last_seq ?? 0) > lastSeq;
      } else {
        setError('This run could not be loaded. Retrying…');
        retry = true;
      }
      loading = false;
      setRefreshing(false);
      if (retry) timer = window.setTimeout(() => { void load(); }, 1500);
    }
    refreshRef.current = () => { void load(); };
    void load();
    return () => { disposed = true; window.clearTimeout(timer); refreshRef.current = () => {}; };
  }, [runId]);

  useEffect(() => {
    let disposed = false;
    let timer: number | undefined;
    async function load() {
      try {
        const result = await api.getRuns({ limit: 200, workspaceId: showAll ? undefined : workspaceId });
        if (disposed) return;
        setRuns(result.runs.sort((a, b) => runSubmissionOrderTime(b) - runSubmissionOrderTime(a)));
        setListError('');
      } catch {
        if (!disposed) setListError('Run history could not be refreshed.');
      }
      if (!disposed) timer = window.setTimeout(() => { void load(); }, 5000);
    }
    void load();
    return () => { disposed = true; window.clearTimeout(timer); };
  }, [workspaceId, showAll, listVersion]);

  const visibleRuns = runs.map((item) => item.run_id === runId && run ? run : item)
    .filter((item) => `${runName(item)} ${item.run_id} ${item.status}`.toLowerCase().includes(query.trim().toLowerCase()));
  const tasks = Object.values(run?.task_nodes || {});
  const edgeCount = run?.graph?.edges.length || 0;
  const refresh = () => { refreshRef.current(); setListVersion((value) => value + 1); };
  const inspector = run && selectedTaskId && run.task_nodes?.[selectedTaskId]
    ? <RunTaskDetails key={`${runId}:${selectedTaskId}`} run={run} events={events} taskId={selectedTaskId} onBack={() => setSelectedTaskId(null)} refreshVersion={listVersion} />
    : <div className="run-workbench-overview" data-testid="run-overview">
      <Typography.Title level={5}>{run ? runWorkflowName(run) : 'Run overview'}</Typography.Title>
      <Space size={4} wrap><Tag color={statusColor(run?.status || '')}>{run?.status || 'Loading'}</Tag><Tag>{run?.kind || 'run'}</Tag></Space>
      <Typography.Paragraph type="secondary" style={{ marginTop: 16 }}>Select a task to inspect its results, resources, inputs, logs and artifacts.</Typography.Paragraph>
      <Descriptions column={1} size="small" layout="vertical">
        <Descriptions.Item label="Run ID"><Typography.Text copyable style={{ wordBreak: 'break-all' }}>{runId}</Typography.Text></Descriptions.Item>
        <Descriptions.Item label="Tasks">{tasks.length}</Descriptions.Item>
        <Descriptions.Item label="Dependencies">{edgeCount}</Descriptions.Item>
        <Descriptions.Item label="Execution">
          <Space size={4} wrap>{['cpu', 'gpu', 'io'].map((kind) => <Tag key={kind}>{kind.toUpperCase()} {tasks.filter((task) => task.task_kind === kind).length}</Tag>)}</Space>
        </Descriptions.Item>
      </Descriptions>
      {(run?.failure_reason || run?.error_summary || run?.error) && <Alert type="error" message="Run failed" description={<pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(run.failure_reason || run.error_summary || run.error, null, 2)}</pre>} />}
    </div>;

  return <WorkbenchShell
    topBar={<Toolbar executionRunId={runId} executionRun={run} onShowDesign={onDesign} onOpenRuns={() => onOpenHistory(run)} onOpenClusterResources={onOpenCluster} />}
    leftSidebar={<div className="run-workbench-sidebar" data-testid="run-sidebar" data-sidebar-collapsed={sidebarCollapsed}>
      <Space style={{ justifyContent: 'space-between', width: '100%', marginBottom: 16 }}>
        {!sidebarCollapsed && <Typography.Text strong><HistoryOutlined /> Runs</Typography.Text>}
        <Space size={4}>
          {!sidebarCollapsed && <Button size="small" icon={<ReloadOutlined />} aria-label="Refresh run list" onClick={refresh} />}
          <Button size="small" icon={sidebarCollapsed ? <DoubleRightOutlined /> : <DoubleLeftOutlined />} aria-label={sidebarCollapsed ? 'Expand run sidebar' : 'Collapse run sidebar'} onClick={() => setSidebarCollapsed((value) => !value)} />
        </Space>
      </Space>
      {!sidebarCollapsed && <>
      <Input prefix={<SearchOutlined />} placeholder="Filter runs" value={query} allowClear onChange={(event) => setQuery(event.target.value)} />
      <Space style={{ margin: '14px 0' }}><Switch size="small" checked={showAll} onChange={setShowAll} /><Typography.Text type="secondary" style={{ fontSize: 12 }}>All workspaces and external runs</Typography.Text></Space>
      {listError && <Alert message={listError} type="warning" style={{ marginBottom: 12 }} />}
      {!visibleRuns.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No matching runs" />}
      {visibleRuns.map((item) => <button key={item.run_id} className="run-workbench-item" aria-pressed={item.run_id === runId} onClick={() => onSelectRun(item.run_id)}>
        <span className="run-workbench-item-title">{runName(item)}</span>
        <Space size={4} wrap><Tag color={statusColor(item.status)}>{item.status}</Tag><Tag>{item.kind}</Tag></Space>
        <span className="run-workbench-item-id">{item.run_id.slice(0, 8)} · {item.created_time ? new Date(item.created_time * 1000).toLocaleString() : ''}</span>
      </button>)}
      </>}
    </div>}
    canvas={<div className="run-workbench-canvas" data-testid="run-workbench" data-run-id={runId}>
      <div className="run-workbench-canvas-header"><Space size={6} wrap><Typography.Text strong>Execution graph</Typography.Text><Tag>{tasks.length} tasks</Tag><Tag>{edgeCount} dependencies</Tag><Tag>Read only</Tag></Space><Button size="small" icon={<ReloadOutlined />} loading={refreshing} onClick={refresh}>Refresh</Button></div>
      {error && <Alert type="warning" message={error} />}
      {run ? <RunGraph run={run} events={events} fullHeight selectedTaskId={selectedTaskId} onSelectTask={setSelectedTaskId} /> : <div style={{ display: 'grid', placeItems: 'center', flex: 1 }}><Spin size="large" /></div>}
    </div>}
    inspector={inspector}
    runsInspector={runsInspector} clusterDrawer={clusterDrawer}
  />;
}
