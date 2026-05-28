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
  workspaceDir: string;
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

export interface WorkspaceFilesResponse {
  success: boolean;
  workspaceDir: string;
  filesDir: string;
  path: string;
  files: WorkspaceFileMeta[];
}

export interface WorkspaceWorkflowsResponse {
  workspaceDir: string;
  workflowsDir: string;
  workflows: WorkspaceWorkflowMeta[];
  errors?: Array<{
    relativePath: string;
    error: string;
  }>;
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
  ray_resources?: Record<string, number>;
}

export interface ClusterResourcesResponse {
  status: 'success' | string;
  cluster: {
    head_node_id?: string | null;
    head_node_ip?: string | null;
    nodes: ClusterResourceNode[];
    unregistered_ray_nodes?: ClusterResourceNode[];
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
  | 'running'
  | 'completed'
  | 'failed'
  | 'canceled'
  | 'interrupted';

export interface StaticWorkflowRunNode {
  node_id: string;
  task_name?: string;
  label?: string;
  category?: string;
  status: 'pending' | 'running' | 'completed' | 'failed';
  created_time?: number | null;
  started_time?: number | null;
  finished_time?: number | null;
  result_summary?: any;
  error?: string | null;
  maze_task_id?: string;
  node_ip?: string | null;
  node_id_runtime?: string | null;
  gpu_id?: string | number | null;
  file_manifest?: any;
  artifacts?: Array<{
    path: string;
    name?: string;
    size?: number;
    sha256?: string;
    mime?: string;
    uri?: string;
    storage_path?: string;
  }>;
}

export interface StaticWorkflowRunSnapshot {
  schema: 'static_workflow_run';
  schema_version: number;
  kind: 'static';
  run_id: string;
  workflow_id: string;
  workflow_name: string;
  workspace_dir?: string;
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
  error?: string | null;
  maze_run_id?: string | null;
}

export interface StaticWorkflowRunEvent {
  type: string;
  seq?: number;
  timestamp?: string;
  schema_version?: number;
  data?: Record<string, any>;
}

export type DynamicRunStatus =
  | 'created'
  | 'running'
  | 'finalized'
  | 'failed'
  | 'canceled'
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
  failure_reason?: string | null;
}

export interface DynamicRunEvent {
  type: string;
  seq?: number;
  timestamp?: string;
  schema_version?: number;
  data?: Record<string, any>;
}
