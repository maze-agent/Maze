export type NodeType = 'task' | 'tool';
export type NodeCategory = 'builtin' | 'custom' | 'workspace' | 'agent';

export interface Resources {
  cpu: number;
  cpu_mem: number;
  gpu: number;
  gpu_mem: number;
}

export interface TaskInputConfig {
  name: string;
  dataType: string;
  source: 'user' | 'task';
  value?: string;  // 用户输入值
  taskSource?: {   // 任务输入来源
    taskId: string;
    outputKey: string;
  };
}

export interface TaskOutputConfig {
  name: string;
  dataType: string;
}

export interface BuiltinTaskMeta {
  name: string;
  displayName: string;
  description?: string;
  inputs: Array<{ name: string; dataType: string }>;
  outputs: Array<{ name: string; dataType: string }>;
  resources?: Resources;
  functionRef: string;
  module: string;
}

export interface WorkspaceTaskMeta {
  name: string;
  displayName: string;
  description?: string;
  inputs: Array<{ name: string; dataType: string }>;
  outputs: Array<{ name: string; dataType: string }>;
  resources?: Resources;
  functionName: string;
  workspaceDir: string;
  relativePath: string;
  code: string;
}

export interface WorkspaceTasksResponse {
  workspaceId?: string;
  workspaceDir: string;
  workspaceManifestVersion?: number;
  tasksDir: string;
  tasks: WorkspaceTaskMeta[];
  errors?: Array<{
    relativePath: string;
    error: string;
    traceback?: string;
  }>;
}

export interface WorkspaceSkillMeta {
  name: string;
  path: string;
  description?: string;
  metadata?: Record<string, any>;
  resources?: Array<Record<string, any>>;
  truncated?: boolean;
  original_chars?: number;
  returned_chars?: number;
}

export interface WorkspaceSkillsResponse {
  success: boolean;
  workspaceId?: string;
  workspaceDir: string;
  workspaceManifestVersion?: number;
  skillsDir: string;
  skills: WorkspaceSkillMeta[];
  errors?: Array<{
    error: string;
    traceback?: string;
  }>;
}

export interface WorkspaceWorkflowMeta {
  name: string;
  relativePath: string;
  nodeCount: number;
  edgeCount: number;
  updatedAt: string;
  size?: number;
}

export interface WorkspaceFileMeta {
  name: string;
  relativePath: string;
  type: 'file' | 'directory';
  size?: number | null;
  updatedAt?: string;
}

export interface LocalWorkspaceFileMeta {
  name: string;
  relativePath: string;
  type: 'file' | 'directory';
  size?: number | null;
  updatedAt?: string | null;
}

export interface WorkspaceFilesResponse {
  success: boolean;
  workspaceId?: string;
  workspaceDir: string;
  workspaceManifestVersion?: number;
  filesDir: string;
  path: string;
  files: WorkspaceFileMeta[];
}

export interface WorkspaceWorkflowsResponse {
  workspaceId?: string;
  workspaceDir: string;
  workspaceManifestVersion?: number;
  workflowsDir: string;
  workflows: WorkspaceWorkflowMeta[];
  errors?: Array<{
    relativePath: string;
    error: string;
  }>;
}

export interface WorkspaceManifest {
  schema?: string;
  schema_version?: number;
  manifest_version?: number;
  workspace_id: string;
  name: string;
  created_at?: string;
  updated_at?: string;
  mode?: string;
  default_sandbox?: 'workspace_sandbox' | 'docker' | string;
  files_dir?: string;
  workflows_dir?: string;
  tasks_dir?: string;
  skills_dir?: string;
  runs_dir?: string;
  policy_path?: string;
  imports?: Array<Record<string, any>>;
  local_mounts?: Array<Record<string, any>>;
  last_change?: Record<string, any>;
}

export interface WorkspaceContextResponse {
  success: boolean;
  workspaceId: string;
  workspaceDir: string;
  workspaceManifestVersion?: number;
  manifest: WorkspaceManifest;
}

export interface SystemCatalogItem {
  type: 'workflows' | 'tasks' | 'skills' | string;
  id: string;
  name: string;
  path: string;
  kind: 'file' | 'directory';
  size?: number | null;
  updatedAt?: string;
  description?: string;
  tags?: string[];
  recommendedSkills?: string[];
}

export interface SystemCatalogResponse {
  success: boolean;
  catalogDir: string;
  catalog: Record<'workflows' | 'tasks' | 'skills', SystemCatalogItem[]>;
}

export interface TaskDefinition {
  type: 'workspace';
  relativePath: string;
  functionName?: string;
  displayName?: string;
  code: string;
  inputs?: TaskInputConfig[];
  outputs?: TaskOutputConfig[];
  resources?: Resources;
}

export interface WorkflowNode {
  id: string;
  type: 'taskNode';
  position: { x: number; y: number };
  data: {
    category: NodeCategory;
    nodeType: NodeType;
    label: string;
    taskRef?: string;  // 内置任务引用 (module.functionName)
    customCode?: string;
    workspaceDir?: string;
    taskPath?: string;
    functionName?: string;
    agentKind?: 'react';
    reactMode?: 'local' | 'online';
    prompt?: string;
    maxSteps?: number;
    maxTokens?: number;
    taskTimeout?: number;
    skills?: string[];
    recommendedSkills?: string[];
    execBackend?: 'workspace_sandbox' | 'docker';
    inputs: TaskInputConfig[];
    outputs: TaskOutputConfig[];
    resources?: Resources;
    configured: boolean;
  };
}

export interface WorkflowEdge {
  id: string;
  source: string;
  target: string;
  sourceHandle?: string;
  targetHandle?: string;
}

export interface Workflow {
  id: string;
  name: string;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  createdAt: string;
}

export interface ClusterGpuDevice {
  gpu_id: number | string;
  total_count: number;
  available_count: number;
  total_memory: number;
  available_memory: number;
}

export interface ClusterResourceNode {
  node_id: string;
  node_ip?: string | null;
  role: 'head' | 'worker' | string;
  registered: boolean;
  alive: boolean;
  running_task_count?: number;
  registered_time?: number | null;
  last_seen_time?: number | null;
  last_resource_update_time?: number | null;
  resources?: {
    cpu?: {
      total: number;
      available: number;
    };
    cpu_mem?: {
      total: number;
      available: number;
    };
    gpu?: {
      total_count: number;
      available_count: number;
      devices: ClusterGpuDevice[];
    };
  };
  capabilities?: {
    workspace_sandbox?: boolean;
    docker_sandbox?: boolean;
    docker_reason?: string;
    [key: string]: any;
  };
  ray_resources?: Record<string, number>;
}

export interface ClusterResourcesResponse {
  status: 'success' | string;
  cluster: {
    head_node_id?: string | null;
    head_node_ip?: string | null;
    scheduling_policy?: string;
    supported_scheduling_policies?: Record<string, {
      implemented: boolean;
      description: string;
    }>;
    nodes: ClusterResourceNode[];
    unregistered_ray_nodes?: ClusterResourceNode[];
  };
}

export interface ClusterScheduleDecision {
  selected?: boolean;
  reason?: string | null;
  requested_resources?: Resources & Record<string, any>;
  scheduling_policy?: string;
  selected_node?: {
    node_id?: string | null;
    node_ip?: string | null;
    gpu_id?: number | string | null;
  };
  candidate_nodes?: Array<{
    node_id: string;
    node_ip?: string | null;
    role?: string;
    alive?: boolean;
    registered?: boolean;
    running_task_count?: number;
    reject_reasons?: string[];
    can_run?: boolean;
    selected_gpu_id?: number | string;
    available_resources?: any;
  }>;
}

export interface ClusterQueueTask {
  workflow_id: string;
  task_id: string;
  task_type?: string;
  status: 'ready' | 'pending' | 'retrying' | 'running' | string;
  runtime_status?: string;
  priority?: number;
  attempt?: number;
  max_retries?: number;
  retry_backoff_seconds?: number;
  retry_wait_seconds?: number;
  next_eligible_time?: number | null;
  pending_reason?: string | null;
  last_error?: any;
  resources?: Resources;
  schedule_decision?: ClusterScheduleDecision | null;
  selected_node?: {
    node_id?: string | null;
    node_ip?: string | null;
    gpu_id?: number | string | null;
  } | null;
  started_time?: number | null;
  elapsed_seconds?: number | null;
  timeout_seconds?: number | null;
}

export interface ClusterQueuesResponse {
  status: 'success' | string;
  queues: {
    snapshot_time?: number;
    scheduling_policy?: string;
    counts: {
      ready: number;
      pending: number;
      retrying: number;
      running: number;
      total_queued: number;
    };
    stopped_workflow_ids?: string[];
    ready_tasks: ClusterQueueTask[];
    pending_tasks: ClusterQueueTask[];
    retrying_tasks: ClusterQueueTask[];
    running_tasks: ClusterQueueTask[];
  };
}

export interface RunResult {
  taskId: string;
  taskName: string;
  status: 'pending' | 'running' | 'completed' | 'failed';
  result?: any;
  error?: string;
  timestamp: string;
}

export type StaticWorkflowRunStatus =
  | 'created'
  | 'queued'
  | 'running'
  | 'completed'
  | 'succeeded'
  | 'failed'
  | 'canceled'
  | 'cancelled'
  | 'timed_out'
  | 'interrupted';

export interface StaticWorkflowRunNode {
  node_id: string;
  task_name?: string;
  label?: string;
  category?: string;
  status: 'pending' | 'queued' | 'running' | 'completed' | 'succeeded' | 'failed' | 'canceled' | 'cancelled' | 'timed_out';
  created_time?: number | null;
  started_time?: number | null;
  finished_time?: number | null;
  duration_seconds?: number | null;
  result_summary?: any;
  error?: any;
  last_error?: any;
  pending_reason?: string | null;
  retry_wait_seconds?: number | null;
  next_eligible_time?: number | null;
  timeout_seconds?: number | null;
  maze_task_id?: string;
  node_ip?: string | null;
  node_id_runtime?: string | null;
  gpu_id?: string | number | null;
  resources?: Resources | Record<string, any>;
  schedule_decision?: ClusterScheduleDecision | null;
  file_manifest?: any;
  artifacts?: RunArtifact[];
}

export interface StaticWorkflowRunSnapshot {
  schema: 'static_workflow_run';
  schema_version: number;
  kind: 'static';
  run_id: string;
  workflow_id: string;
  workflow_name: string;
  workspace_dir?: string;
  workspace_id?: string;
  workspace_manifest_version?: number | null;
  status: StaticWorkflowRunStatus;
  created_time?: number;
  updated_time?: number;
  finished_time?: number | null;
  task_counts?: Record<string, number>;
  task_nodes?: Record<string, StaticWorkflowRunNode>;
  graph?: {
    nodes: string[];
    edges: Array<{ source: string; target: string }>;
  };
  events?: {
    count: number;
    last_seq: number;
  };
  final_result?: any;
  error?: any;
  maze_run_id?: string | null;
  error_summary?: any;
  result_summary?: any;
  metadata?: Record<string, any>;
  tags?: string[];
}

export interface StaticWorkflowRunEvent {
  type: string;
  seq?: number;
  timestamp?: string;
  schema_version?: number;
  data?: Record<string, any>;
}

export type UnifiedRunStatus =
  | 'created'
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'timed_out'
  | 'interrupted'
  | string;

export interface UnifiedRunTaskSnapshot {
  task_id: string;
  task_name?: string;
  task_spec_id?: string | null;
  request_id?: string | null;
  status: string;
  parents?: string[];
  children?: string[];
  created_time?: number | null;
  started_time?: number | null;
  start_time?: number | null;
  finished_time?: number | null;
  finish_time?: number | null;
  duration_seconds?: number | null;
  resources?: Resources | Record<string, any>;
  inputs?: any[];
  outputs?: any[];
  selected_node?: {
    node_id?: string | null;
    node_ip?: string | null;
    gpu_id?: string | number | null;
  } | null;
  schedule_decision?: ClusterScheduleDecision | null;
  result_summary?: any;
  error?: any;
  last_error?: any;
  pending_reason?: string | null;
  retry_wait_seconds?: number | null;
  next_eligible_time?: number | null;
  timeout_seconds?: number | null;
  file_manifest?: any;
}

export interface UnifiedRunSnapshot {
  schema?: string;
  schema_version?: number;
  kind: 'static' | 'dynamic' | string;
  summary?: boolean;
  run_type?: string;
  run_id: string;
  workflow_id?: string;
  status: UnifiedRunStatus;
  native_status?: string;
  mode?: string;
  created_time?: number;
  updated_time?: number;
  started_time?: number | null;
  finished_time?: number | null;
  duration_seconds?: number | null;
  timeout_seconds?: number | null;
  progress?: {
    completed?: number;
    total?: number;
    fraction?: number;
  };
  task_counts?: Record<string, number>;
  task_nodes?: Record<string, UnifiedRunTaskSnapshot>;
  graph?: {
    nodes: string[];
    edges: Array<{ source: string; target: string }>;
  };
  event_count?: number;
  last_event_seq?: number;
  result_summary?: any;
  error_summary?: any;
  final_result?: any;
  metadata?: Record<string, any>;
  tags?: string[];
  max_tasks?: number;
  cancel_reason?: string | null;
  failure_reason?: any;
}

export interface UnifiedRunEvent {
  type: string;
  seq?: number;
  timestamp?: string;
  schema_version?: number;
  data?: Record<string, any>;
}

export interface RunArtifact {
  run_id?: string;
  task_id?: string;
  producer_task_id?: string;
  path: string;
  name?: string;
  size?: number | null;
  sha256?: string;
  mime?: string;
  uri?: string;
  storage_uri?: string;
  storage_path?: string;
  created_time?: number | null;
}

export interface RunLogLine {
  timestamp?: string | null;
  seq?: number | null;
  stream: 'event' | 'stdout' | 'stderr' | 'metadata' | string;
  task_id?: string | null;
  type?: string;
  path?: string;
  line?: number;
  message: string;
}

export type DynamicRunStatus =
  | 'created'
  | 'running'
  | 'finalized'
  | 'succeeded'
  | 'failed'
  | 'canceled'
  | 'cancelled'
  | 'timed_out'
  | 'interrupted';

export interface DynamicRunSnapshot {
  schema?: string;
  schema_version?: number;
  run_id: string;
  status: DynamicRunStatus;
  kind?: 'dynamic';
  summary?: boolean;
  mode?: string;
  max_tasks?: number;
  timeout_seconds?: number | null;
  created_time?: number;
  updated_time?: number;
  finished_time?: number | null;
  task_counts?: Record<string, number>;
  tasks?: Record<string, string[]>;
  task_specs?: Record<string, any>;
  task_nodes?: Record<string, any>;
  graph?: {
    nodes: string[];
    edges: Array<{ source: string; target: string }>;
  };
  request_ids?: Record<string, string>;
  event_count?: number;
  last_event_seq?: number;
  final_result?: any;
  cancel_reason?: string | null;
  failure_reason?: any;
  metadata?: Record<string, any>;
}

export interface DynamicRunEvent {
  type: string;
  seq?: number;
  timestamp?: string;
  schema_version?: number;
  data?: Record<string, any>;
}
