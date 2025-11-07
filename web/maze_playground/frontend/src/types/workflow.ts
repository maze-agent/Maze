export type NodeType = 'task' | 'tool';
export type NodeCategory = 'builtin' | 'custom';

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
  nodeType: NodeType;
  inputs: Array<{ name: string; dataType: string }>;
  outputs: Array<{ name: string; dataType: string }>;
  resources?: Resources;
  functionRef: string;
  module: string;
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

export interface RunResult {
  taskId: string;
  taskName: string;
  status: 'pending' | 'running' | 'completed' | 'failed';
  result?: any;
  error?: string;
  timestamp: string;
}

