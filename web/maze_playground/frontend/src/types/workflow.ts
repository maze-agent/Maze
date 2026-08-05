export type NodeCategory = 'builtin' | 'custom' | 'workspace';

export interface Resources {
  cpu_num: number;
  gpu_mem: number;
  io_num: number;
}

export interface LocalModel {
  id: string;
  name: string;
  path: string;
  type: 'local' | string;
  model_type?: string;
  backend?: string;
  backends?: string[];
  model_scope?: string;
  weight_bytes?: number;
  weight_size?: string;
  estimated_params?: number;
  estimated_params_label?: string;
  estimated_weight_memory_bytes?: number;
  estimated_weight_memory?: string;
  estimated_gpu_mem_mb?: number;
  estimate_method?: string;
}

export interface ModelAnchor {
  local_model: string;
  model_scope: 'head' | string;
  backend: 'transformers' | string;
  estimated_weight_memory_bytes?: number;
  estimated_gpu_mem_mb?: number;
  estimated_params?: number;
}

export interface ModelsResponse {
  status: 'success' | string;
  model_dir: string;
  models: LocalModel[];
}

export interface ModelTestResponse {
  status: 'success' | string;
  ok: boolean;
  model: LocalModel;
  checks: Array<{
    name: string;
    ok: boolean;
    message: string;
  }>;
  runtime?: {
    tokenizer_seconds?: number;
    load_seconds?: number;
    generate_seconds?: number;
    device?: string;
    generated_text?: string;
    cuda?: boolean;
    peak_cuda_allocated_bytes?: number;
    peak_cuda_reserved_bytes?: number;
  };
  run_id?: string | null;
  workflow_id?: string;
  task_id?: string;
  resources?: Resources & Record<string, any>;
  message: string;
}

export interface ResourceHistoryResponse {
  status: 'success' | string;
  history: {
    schema?: string;
    schema_version?: number;
    updated_time?: number;
    models?: Record<string, any>;
    tasks?: Record<string, any>;
    recent_observations?: Array<Record<string, any>>;
  };
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
  type: 'workflows' | 'tasks';
  id: string;
  name: string;
  path: string;
  kind: 'file' | 'directory';
  size?: number | null;
  updatedAt?: string;
  description?: string;
  tags?: string[];
}

export interface SystemCatalogResponse {
  success: boolean;
  catalogDir: string;
  catalog: Record<'workflows' | 'tasks', SystemCatalogItem[]>;
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
    nodeType: 'task';
    label: string;
    taskRef?: string;  // 内置任务引用 (module.functionName)
    customCode?: string;
    workspaceDir?: string;
    taskPath?: string;
    functionName?: string;
    prompt?: string;
    taskTimeout?: number;
    localModel?: string;
    modelAnchor?: ModelAnchor;
    execBackend?: 'workspace_sandbox' | 'docker';
    inputs: TaskInputConfig[];
    outputs: TaskOutputConfig[];
    resources?: Resources;
    task_kind?: 'cpu' | 'gpu' | 'io';
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
  disabled?: boolean;
  stale?: boolean;
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
      total_memory?: number;
      available_memory?: number;
    };
  };
  capabilities?: {
    workspace_sandbox?: boolean;
    docker_sandbox?: boolean;
    docker_reason?: string;
    [key: string]: any;
  };
  local_models?: LocalModel[];
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
    disabled_node_ids?: string[];
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
    disabled?: boolean;
    registered?: boolean;
    running_task_count?: number;
    reject_reasons?: string[];
    can_run?: boolean;
    selected_gpu_id?: number | string;
    available_resources?: any;
  }>;
}

export interface ClusterHacsBreakdown {
  mode?: 'static' | 'dynamic' | string;
  task_kind?: 'gpu' | 'cpu' | 'io' | string;
  predicted_duration?: number;
  prediction_source?: string;
  prediction_confidence?: number;
  prediction_sample_count?: number;
  code_hash?: string | null;
  n_desc?: number;
  n_anc?: number;
  topological_weight?: number;
  workflow_wait_time?: number;
  remaining_value_tasks?: number;
  avg_completion_seconds?: number;
  alpha?: number;
  beta?: number;
  phi?: number;
  value_multiplier?: number;
  score?: number;
}

export interface FaultToleranceTrace {
  enabled?: boolean;
  status?: string;
  attempts?: Array<{
    attempt?: number | null;
    failure?: any;
    diagnosis?: any;
    repair_action?: any;
    retry?: any;
    outcome?: any;
    timestamp?: string;
  }>;
}

export interface ClusterQueueTask {
  workflow_id: string;
  task_id: string;
  task_type?: string;
  status: 'ready' | 'pending' | 'retrying' | 'running' | string;
  runtime_status?: string;
  task_kind?: 'gpu' | 'cpu' | 'io' | string;
  queue_name?: 'gpu' | 'cpu' | 'io' | string;
  priority?: number;
  predicted_duration?: number | null;
  prediction_source?: string | null;
  prediction_confidence?: number | null;
  prediction_sample_count?: number | null;
  topological_weight?: number | null;
  workflow_wait_time?: number | null;
  remaining_value_tasks?: number | null;
  hacs_score?: number | null;
  hacs_breakdown?: ClusterHacsBreakdown | null;
  code_hash?: string | null;
  attempt?: number;
  max_retries?: number;
  retry_backoff_seconds?: number;
  retry_wait_seconds?: number;
  next_eligible_time?: number | null;
  pending_reason?: string | null;
  last_error?: any;
  resources?: Resources;
  schedule_decision?: ClusterScheduleDecision | null;
  fault_tolerance?: FaultToleranceTrace;
  selected_node?: {
    node_id?: string | null;
    node_ip?: string | null;
    gpu_id?: number | string | null;
  } | null;
  started_time?: number | null;
  elapsed_seconds?: number | null;
  timeout_seconds?: number | null;
}

export interface ClusterQueueBucket {
  total: number;
  ready: number;
  pending: number;
  retrying: number;
  tasks: ClusterQueueTask[];
}

export interface ClusterQueuesResponse {
  status: 'success' | string;
  queues: {
    snapshot_time?: number;
    scheduling_algorithm?: 'FCFS' | 'HACS' | string;
    scheduling_policy?: string;
    counts: {
      ready: number;
      pending: number;
      retrying: number;
      running: number;
      total_queued: number;
      by_queue?: Record<string, {
        ready: number;
        pending: number;
        retrying: number;
        total: number;
      }>;
    };
    queues?: Record<string, ClusterQueueBucket>;
    stopped_workflow_ids?: string[];
    ready_tasks: ClusterQueueTask[];
    pending_tasks: ClusterQueueTask[];
    retrying_tasks: ClusterQueueTask[];
    running_tasks: ClusterQueueTask[];
  };
}

export interface WorkerProfile {
  id: string;
  name: string;
  host: string;
  port: number;
  username: string;
  remoteProjectDir: string;
  condaEnv: string;
  condaSh?: string;
  headUrl?: string;
  heartbeatInterval?: number;
  logDir?: string;
  auth?: {
    method?: 'password' | 'key' | string;
    hasPassword?: boolean;
    hasPrivateKey?: boolean;
    privateKeyPath?: string;
  };
  createdAt?: string;
  updatedAt?: string;
  lastAction?: {
    action?: string;
    ok?: boolean;
    at?: string;
    stdoutTail?: string;
    stderrTail?: string;
  } | null;
}

export interface WorkerProfilesResponse {
  success: boolean;
  workspaceId?: string;
  workspaceDir?: string;
  profiles: WorkerProfile[];
}

export interface WorkerProfileActionResponse {
  success: boolean;
  workspaceId?: string;
  workspaceDir?: string;
  action: string;
  profile?: WorkerProfile;
  result?: {
    ok: boolean;
    code: number;
    stdout: string;
    stderr: string;
  };
  results?: Array<{
    profileId: string;
    ok: boolean;
    profile?: WorkerProfile;
    result?: {
      ok: boolean;
      code: number;
      stdout: string;
      stderr: string;
    };
    error?: string;
  }>;
}

export interface WorkerProfileDraftTestResponse {
  success: boolean;
  workspaceId?: string;
  workspaceDir?: string;
  profile?: WorkerProfile;
  test?: {
    ok: boolean;
    checks: Array<{
      name: string;
      ok: boolean;
      warning?: boolean;
      stdout?: string;
      stderr?: string;
    }>;
    result?: {
      ok: boolean;
      code: number;
      stdout: string;
      stderr: string;
    };
  };
}

export interface ClusterConsoleRunResponse {
  success: boolean;
  workspaceId?: string;
  workspaceDir?: string;
  target: string;
  targetLabel?: string;
  command: string;
  timeoutMs: number;
  ranAt: string;
  result?: {
    ok: boolean;
    code: number;
    stdout: string;
    stderr: string;
  };
  error?: string;
}

export interface RunResult {
  taskId: string;
  taskName: string;
  status: 'pending' | 'running' | 'completed' | 'failed';
  result?: any;
  error?: string;
  timestamp: string;
}

export type UnifiedRunStatus =
  | 'created'
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'timed_out'
  | 'interrupted';

export interface UnifiedRunTaskSnapshot {
  task_id: string;
  node_id?: string;
  task_name?: string;
  task_kind?: 'cpu' | 'gpu' | 'io';
  label?: string;
  category?: string;
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
  maze_task_id?: string;
  node_ip?: string | null;
  node_id_runtime?: string | null;
  gpu_id?: string | number | null;
  file_manifest?: any;
  fault_tolerance?: FaultToleranceTrace;
  artifacts?: RunArtifact[];
}

export interface UnifiedRunSnapshot {
  schema?: string;
  schema_version?: number;
  kind: 'static' | 'dynamic' | string;
  summary?: boolean;
  run_type?: string;
  run_id: string;
  workflow_id?: string;
  workflow_name?: string;
  workspace_dir?: string;
  workspace_id?: string;
  workspace_manifest_version?: number | null;
  status: UnifiedRunStatus;
  native_status?: string;
  mode?: string;
  created_time?: number;
  submitted_time?: number | null;
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
  events?: {
    count: number;
    last_seq: number;
  };
  result_summary?: any;
  error_summary?: any;
  error?: any;
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
