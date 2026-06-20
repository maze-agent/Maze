export type RuntimeConsoleTab = 'timeline' | 'events' | 'logs' | 'artifacts';

export type RuntimeLevel = 'debug' | 'info' | 'warn' | 'error';

export type RuntimeEventCategory =
  | 'task'
  | 'scheduler'
  | 'worker'
  | 'resource'
  | 'artifact'
  | 'dynamic'
  | 'inference';

export type RuntimeEvent = {
  id: string;
  time: string;
  level: RuntimeLevel;
  taskId?: string;
  taskName?: string;
  event: string;
  details: string;
  category: RuntimeEventCategory;
};

export type SchedulerDecision = {
  id: string;
  time: string;
  type:
    | 'place_task'
    | 'queue_task'
    | 'retry_task'
    | 'append_dynamic_task'
    | 'append_dynamic_subdag'
    | 'scale_inference'
    | 'release_resource'
    | 'failover_worker';
  taskId?: string;
  taskName?: string;
  reason: string;
  result: string;
  worker?: string;
  resource?: string;
  severity?: 'info' | 'warn' | 'error';
};

export type TimelineItem = {
  taskId: string;
  taskName: string;
  state: 'pending' | 'queued' | 'running' | 'succeeded' | 'failed';
  worker?: string;
  startedAt?: string;
  endedAt?: string;
  duration?: string;
  queueReason?: string;
  isDynamic?: boolean;
};

export type RuntimeLogLine = {
  id: string;
  time: string;
  level: RuntimeLevel;
  taskId: string;
  taskName: string;
  message: string;
};

export type RuntimeArtifact = {
  id: string;
  name: string;
  taskId: string;
  taskName: string;
  type: 'file' | 'dataset' | 'model' | 'log' | 'tensor' | 'json' | 'parquet';
  size?: string;
  createdAt: string;
  uri?: string;
};

export type RuntimeBreakdownItem = {
  reason: string;
  percent: number;
  count: number;
  color?: string;
};
