import { useMemo, useState } from 'react';
import { Button, Input, Select, Space, Switch, Typography } from 'antd';
import {
  CompressOutlined,
  ExpandAltOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
} from '@ant-design/icons';
import ArtifactsTable from './ArtifactsTable';
import EventsTable from './EventsTable';
import LogsPanel from './LogsPanel';
import QueueReasonBreakdown from './QueueReasonBreakdown';
import RuntimeConsoleTabs from './RuntimeConsoleTabs';
import TimelineTable from './TimelineTable';
import type {
  RuntimeArtifact,
  RuntimeBreakdownItem,
  RuntimeConsoleTab,
  RuntimeEvent,
  RuntimeLevel,
  RuntimeLogLine,
  TimelineItem,
} from './types';

const { Text } = Typography;

type RuntimeConsoleProps = {
  runId?: string | null;
  runStatus?: string | null;
  activeTab?: RuntimeConsoleTab;
  events?: RuntimeEvent[];
  timeline?: TimelineItem[];
  logs?: RuntimeLogLine[];
  artifacts?: RuntimeArtifact[];
  taskTypeBreakdown?: RuntimeBreakdownItem[];
  selectedTaskId?: string;
  onSelectTask?: (taskId: string) => void;
  onTabChange?: (tab: RuntimeConsoleTab) => void;
};

function matchesQuery(query: string, values: Array<string | undefined>) {
  if (!query) return true;
  return values.some((value) => value?.toLowerCase().includes(query));
}

function uniqueTasks(items: Array<{ taskId?: string; taskName?: string }>) {
  const seen = new Map<string, string>();
  items.forEach((item) => {
    if (item.taskId) {
      seen.set(item.taskId, item.taskName || item.taskId);
    }
  });
  return Array.from(seen.entries()).map(([value, label]) => ({ value, label }));
}

export default function RuntimeConsole({
  runId,
  runStatus,
  activeTab,
  events = [],
  timeline = [],
  logs = [],
  artifacts = [],
  taskTypeBreakdown = [],
  selectedTaskId,
  onSelectTask,
  onTabChange,
}: RuntimeConsoleProps) {
  const [internalTab, setInternalTab] = useState<RuntimeConsoleTab>('events');
  const [levelFilter, setLevelFilter] = useState<'all' | RuntimeLevel>('all');
  const [taskFilter, setTaskFilter] = useState<string>('all');
  const [search, setSearch] = useState('');
  const [autoScroll, setAutoScroll] = useState(true);
  const [collapsed, setCollapsed] = useState(false);
  const currentTab = activeTab || internalTab;
  const query = search.trim().toLowerCase();
  const hasRun = Boolean(runId);

  const taskOptions = useMemo(() => [
    { value: 'all', label: 'All tasks' },
    ...uniqueTasks([...events, ...timeline, ...logs, ...artifacts]),
  ], [artifacts, events, logs, timeline]);

  const filteredEvents = useMemo(() => events
    .filter((event) => levelFilter === 'all' || event.level === levelFilter)
    .filter((event) => taskFilter === 'all' || event.taskId === taskFilter)
    .filter((event) => matchesQuery(query, [event.taskName, event.event, event.details, event.category])),
  [events, levelFilter, query, taskFilter]);

  const filteredTimeline = useMemo(() => timeline
    .filter((item) => taskFilter === 'all' || item.taskId === taskFilter)
    .filter((item) => matchesQuery(query, [item.taskName, item.worker, item.queueReason, item.state])),
  [query, taskFilter, timeline]);

  const filteredLogs = useMemo(() => logs
    .filter((line) => levelFilter === 'all' || line.level === levelFilter)
    .filter((line) => taskFilter === 'all' || line.taskId === taskFilter)
    .filter((line) => matchesQuery(query, [line.taskName, line.message])),
  [levelFilter, logs, query, taskFilter]);

  const filteredArtifacts = useMemo(() => artifacts
    .filter((artifact) => taskFilter === 'all' || artifact.taskId === taskFilter)
    .filter((artifact) => matchesQuery(query, [
      artifact.name,
      artifact.taskName,
      artifact.type,
      artifact.size,
      artifact.uri,
    ])),
  [artifacts, query, taskFilter]);

  const tabCounts = useMemo(() => ({
    timeline: timeline.length,
    events: events.length,
    logs: logs.length,
    artifacts: artifacts.length,
  }), [artifacts.length, events.length, logs.length, timeline.length]);

  function changeTab(tab: RuntimeConsoleTab) {
    setInternalTab(tab);
    onTabChange?.(tab);
  }

  return (
    <section
      className={`workbench-runtime-console${collapsed ? ' is-collapsed' : ''}`}
      data-workbench-region="RuntimeConsole"
    >
      <div className="runtime-console-header">
        <div className="runtime-console-title">
          <strong>Runtime Console</strong>
          <Text type="secondary">{runId ? `${runId}${runStatus ? ` · ${runStatus}` : ''}` : 'No run selected'}</Text>
        </div>
        <RuntimeConsoleTabs activeTab={currentTab} counts={tabCounts} onTabChange={changeTab} />
        <Space size={6} className="runtime-console-header-actions">
          <Button
            size="small"
            type="text"
            icon={collapsed ? <ExpandAltOutlined /> : <CompressOutlined />}
            aria-label={collapsed ? 'Expand runtime console' : 'Collapse runtime console'}
            onClick={() => setCollapsed((value) => !value)}
          />
        </Space>
      </div>

      {!collapsed && (
        <>
          {hasRun && (
            <div className="runtime-console-toolbar">
              <Select
                size="small"
                value={levelFilter}
                onChange={setLevelFilter}
                options={[
                  { value: 'all', label: 'All levels' },
                  { value: 'debug', label: 'Debug' },
                  { value: 'info', label: 'Info' },
                  { value: 'warn', label: 'Warn' },
                  { value: 'error', label: 'Error' },
                ]}
              />
              <Select
                size="small"
                value={taskFilter}
                onChange={setTaskFilter}
                options={taskOptions}
                showSearch
                optionFilterProp="label"
              />
              <Input
                size="small"
                allowClear
                placeholder="Search runtime..."
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
              <Button
                size="small"
                icon={autoScroll ? <PauseCircleOutlined /> : <PlayCircleOutlined />}
                onClick={() => setAutoScroll((value) => !value)}
              >
                {autoScroll ? 'Pause' : 'Auto-scroll'}
              </Button>
              <Switch
                size="small"
                checked={Boolean(selectedTaskId)}
                disabled
                checkedChildren="Linked"
                unCheckedChildren="All"
              />
            </div>
          )}

          <div className="runtime-console-body">
            <div className="runtime-console-content">
              {!hasRun && (
                <div className="runtime-empty-state runtime-empty-run">
                  <strong>No workflow run selected</strong>
                  <span>Run a workflow or open a recorded run to inspect runtime events, timeline, logs, and artifacts.</span>
                </div>
              )}
              {hasRun && currentTab === 'events' && (
                <EventsTable events={filteredEvents} onSelectTask={onSelectTask} />
              )}
              {hasRun && currentTab === 'timeline' && (
                <TimelineTable timeline={filteredTimeline} onSelectTask={onSelectTask} />
              )}
              {hasRun && currentTab === 'logs' && (
                <LogsPanel logs={filteredLogs} onSelectTask={onSelectTask} />
              )}
              {hasRun && currentTab === 'artifacts' && (
                <ArtifactsTable artifacts={filteredArtifacts} onSelectTask={onSelectTask} />
              )}
            </div>
            <QueueReasonBreakdown
              items={taskTypeBreakdown}
              title="Queue Breakdown"
              centerLabel="queued"
            />
          </div>
        </>
      )}
    </section>
  );
}
