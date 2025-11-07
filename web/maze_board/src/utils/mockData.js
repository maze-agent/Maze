// Mock API 数据
export const mockAPI = {
  workers: [
    { 
      worker_id: 'worker-1', 
      hostname: 'node-01.cluster', 
      cpu_total: 16, 
      cpu_used: 8, 
      memory_total_gb: 64, 
      memory_used_gb: 32, 
      gpu_total: 2, 
      gpu_used: 1, 
      status: 'active' 
    },
    { 
      worker_id: 'worker-2', 
      hostname: 'node-02.cluster', 
      cpu_total: 16, 
      cpu_used: 14, 
      memory_total_gb: 64, 
      memory_used_gb: 48, 
      gpu_total: 2, 
      gpu_used: 2, 
      status: 'active' 
    },
    { 
      worker_id: 'worker-3', 
      hostname: 'node-03.cluster', 
      cpu_total: 8, 
      cpu_used: 2, 
      memory_total_gb: 32, 
      memory_used_gb: 8, 
      gpu_total: 1, 
      gpu_used: 0, 
      status: 'idle' 
    },
    { 
      worker_id: 'worker-4', 
      hostname: 'node-04.cluster', 
      cpu_total: 16, 
      cpu_used: 0, 
      memory_total_gb: 64, 
      memory_used_gb: 4, 
      gpu_total: 2, 
      gpu_used: 0, 
      status: 'offline' 
    },
  ],
  
  workflows: [
    { 
      workflow_id: 'wf-001', 
      workflow_name: 'ML Training Pipeline', 
      created_at: '2025-10-15T08:00:00', 
      total_requests: 125,
      service_status: 'running' // running, paused, stopped
    },
    { 
      workflow_id: 'wf-002', 
      workflow_name: 'Data Processing Job', 
      created_at: '2025-10-18T10:00:00', 
      total_requests: 58,
      service_status: 'running'
    },
    { 
      workflow_id: 'wf-003', 
      workflow_name: 'Model Evaluation', 
      created_at: '2025-10-19T14:00:00', 
      total_requests: 32,
      service_status: 'paused'
    },
  ],
  
  workflowDetails: {
    'wf-001': {
      nodes: [
        { id: 'node-1', name: 'Data Ingestion', type: 'input', x: 100, y: 150, status: 'completed' },
        { id: 'node-2', name: 'Data Preprocessing', type: 'transform', x: 300, y: 100, status: 'completed' },
        { id: 'node-3', name: 'Feature Engineering', type: 'transform', x: 300, y: 200, status: 'completed' },
        { id: 'node-4', name: 'Model Training', type: 'compute', x: 500, y: 150, status: 'running' },
        { id: 'node-5', name: 'Model Validation', type: 'compute', x: 700, y: 100, status: 'pending' },
        { id: 'node-6', name: 'Model Deployment', type: 'output', x: 700, y: 200, status: 'pending' },
      ],
      edges: [
        { from: 'node-1', to: 'node-2' },
        { from: 'node-1', to: 'node-3' },
        { from: 'node-2', to: 'node-4' },
        { from: 'node-3', to: 'node-4' },
        { from: 'node-4', to: 'node-5' },
        { from: 'node-4', to: 'node-6' },
      ],
      // API 配置
      api_config: {
        endpoint: 'http://localhost:5173/wf-001',
        method: 'POST',
        auth: {
          type: 'API Key',
          header: 'X-Maze-API-Key',
          required: true
        },
        request_schema: {
          type: 'object',
          properties: {
            dataset_path: {
              type: 'string',
              description: '训练数据集路径',
              required: true,
              example: 's3://bucket/dataset.csv'
            },
            batch_size: {
              type: 'integer',
              description: '批次大小',
              required: false,
              default: 32,
              example: 64
            },
            learning_rate: {
              type: 'number',
              description: '学习率',
              required: false,
              default: 0.001,
              example: 0.01
            },
            epochs: {
              type: 'integer',
              description: '训练轮数',
              required: false,
              default: 10,
              example: 50
            }
          }
        },
        response_schema: {
          type: 'object',
          properties: {
            run_id: {
              type: 'string',
              description: '运行实例ID'
            },
            status: {
              type: 'string',
              description: '请求状态',
              enum: ['accepted', 'queued', 'running', 'completed', 'failed']
            },
            message: {
              type: 'string',
              description: '响应消息'
            }
          }
        }
      }
    },
    'wf-002': {
      nodes: [
        { id: 'node-1', name: 'Load Data', type: 'input', x: 100, y: 150 },
        { id: 'node-2', name: 'Transform', type: 'transform', x: 300, y: 150 },
        { id: 'node-3', name: 'Save Results', type: 'output', x: 500, y: 150 },
      ],
      edges: [
        { from: 'node-1', to: 'node-2' },
        { from: 'node-2', to: 'node-3' },
      ],
      api_config: {
        endpoint: 'http://localhost:5173/wf-002',
        method: 'POST',
        auth: {
          type: 'API Key',
          header: 'X-Maze-API-Key',
          required: true
        },
        request_schema: {
          type: 'object',
          properties: {
            input_file: {
              type: 'string',
              description: '输入文件路径',
              required: true,
              example: '/data/input.json'
            },
            output_format: {
              type: 'string',
              description: '输出格式',
              required: false,
              default: 'parquet',
              enum: ['parquet', 'csv', 'json'],
              example: 'csv'
            }
          }
        },
        response_schema: {
          type: 'object',
          properties: {
            run_id: {
              type: 'string',
              description: '运行实例ID'
            },
            status: {
              type: 'string',
              description: '请求状态'
            }
          }
        }
      }
    },
    'wf-003': {
      nodes: [
        { id: 'node-1', name: 'Load Model', type: 'input', x: 100, y: 150 },
        { id: 'node-2', name: 'Evaluate', type: 'compute', x: 300, y: 150 },
        { id: 'node-3', name: 'Generate Report', type: 'output', x: 500, y: 150 },
      ],
      edges: [
        { from: 'node-1', to: 'node-2' },
        { from: 'node-2', to: 'node-3' },
      ],
      api_config: {
        endpoint: 'http://localhost:5173/wf-003',
        method: 'POST',
        auth: {
          type: 'API Key',
          header: 'X-Maze-API-Key',
          required: true
        },
        request_schema: {
          type: 'object',
          properties: {
            model_id: {
              type: 'string',
              description: '模型ID',
              required: true,
              example: 'model-v1.0.0'
            },
            test_dataset: {
              type: 'string',
              description: '测试数据集',
              required: true,
              example: 's3://bucket/test.csv'
            },
            metrics: {
              type: 'array',
              items: { type: 'string' },
              description: '评估指标列表',
              required: false,
              default: ['accuracy', 'f1'],
              example: ['accuracy', 'precision', 'recall', 'f1']
            }
          }
        },
        response_schema: {
          type: 'object',
          properties: {
            run_id: {
              type: 'string',
              description: '运行实例ID'
            },
            status: {
              type: 'string',
              description: '请求状态'
            }
          }
        }
      }
    }
  }
};

// 新增：每个 workflow 的 runs（运行实例）
export const workflowRuns = {
  'wf-001': [
    {
      run_id: 'run-001-001',
      workflow_id: 'wf-001',
      status: 'completed',
      started_at: '2025-10-22T10:00:00',
      completed_at: '2025-10-22T10:15:30',
      duration: 930, // 秒
      total_tasks: 6,
      completed_tasks: 6
    },
    {
      run_id: 'run-001-002',
      workflow_id: 'wf-001',
      status: 'running',
      started_at: '2025-10-22T11:00:00',
      completed_at: null,
      duration: null,
      total_tasks: 6,
      completed_tasks: 3
    },
    {
      run_id: 'run-001-003',
      workflow_id: 'wf-001',
      status: 'failed',
      started_at: '2025-10-22T09:00:00',
      completed_at: '2025-10-22T09:08:45',
      duration: 525,
      total_tasks: 6,
      completed_tasks: 4,
      error: 'Task node-4 failed: CUDA out of memory'
    }
  ],
  'wf-002': [
    {
      run_id: 'run-002-001',
      workflow_id: 'wf-002',
      status: 'completed',
      started_at: '2025-10-22T08:00:00',
      completed_at: '2025-10-22T08:25:12',
      duration: 1512,
      total_tasks: 6,
      completed_tasks: 6
    }
  ]
};

// 新增：Run 的任务执行详情
export const runTaskExecutions = {
  'run-001-001': [
    {
      task_id: 'task-001-001-1',
      node_id: 'node-1',
      node_name: 'Data Ingestion',
      status: 'completed',
      worker_id: 'worker-1',
      started_at: '2025-10-22T10:00:00',
      completed_at: '2025-10-22T10:02:30',
      duration: 150,
      output: { data_size: '10GB', records: 1000000 }
    },
    {
      task_id: 'task-001-001-2',
      node_id: 'node-2',
      node_name: 'Data Preprocessing',
      status: 'completed',
      worker_id: 'worker-2',
      started_at: '2025-10-22T10:02:35',
      completed_at: '2025-10-22T10:05:00',
      duration: 145,
      output: { cleaned_records: 998000 }
    },
    {
      task_id: 'task-001-001-3',
      node_id: 'node-3',
      node_name: 'Feature Engineering',
      status: 'completed',
      worker_id: 'worker-3',
      started_at: '2025-10-22T10:02:35',
      completed_at: '2025-10-22T10:06:10',
      duration: 215,
      output: { features: 256 }
    },
    {
      task_id: 'task-001-001-4',
      node_id: 'node-4',
      node_name: 'Model Training',
      status: 'completed',
      worker_id: 'worker-1',
      started_at: '2025-10-22T10:06:15',
      completed_at: '2025-10-22T10:12:45',
      duration: 390,
      output: { accuracy: 0.95, loss: 0.05 }
    },
    {
      task_id: 'task-001-001-5',
      node_id: 'node-5',
      node_name: 'Model Validation',
      status: 'completed',
      worker_id: 'worker-2',
      started_at: '2025-10-22T10:12:50',
      completed_at: '2025-10-22T10:14:20',
      duration: 90,
      output: { validation_score: 0.93 }
    },
    {
      task_id: 'task-001-001-6',
      node_id: 'node-6',
      node_name: 'Model Deployment',
      status: 'completed',
      worker_id: 'worker-3',
      started_at: '2025-10-22T10:12:50',
      completed_at: '2025-10-22T10:15:30',
      duration: 160,
      output: { endpoint: 'http://api.example.com/model/v1' }
    }
  ],
  'run-001-002': [
    {
      task_id: 'task-001-002-1',
      node_id: 'node-1',
      node_name: 'Data Ingestion',
      status: 'completed',
      worker_id: 'worker-2',
      started_at: '2025-10-22T11:00:00',
      completed_at: '2025-10-22T11:02:15',
      duration: 135,
      output: { data_size: '12GB', records: 1200000 }
    },
    {
      task_id: 'task-001-002-2',
      node_id: 'node-2',
      node_name: 'Data Preprocessing',
      status: 'completed',
      worker_id: 'worker-1',
      started_at: '2025-10-22T11:02:20',
      completed_at: '2025-10-22T11:04:50',
      duration: 150,
      output: { cleaned_records: 1195000 }
    },
    {
      task_id: 'task-001-002-3',
      node_id: 'node-3',
      node_name: 'Feature Engineering',
      status: 'completed',
      worker_id: 'worker-3',
      started_at: '2025-10-22T11:02:20',
      completed_at: '2025-10-22T11:05:35',
      duration: 195,
      output: { features: 256 }
    },
    {
      task_id: 'task-001-002-4',
      node_id: 'node-4',
      node_name: 'Model Training',
      status: 'running',
      worker_id: 'worker-2',
      started_at: '2025-10-22T11:05:40',
      completed_at: null,
      duration: null,
      output: null
    },
    {
      task_id: 'task-001-002-5',
      node_id: 'node-5',
      node_name: 'Model Validation',
      status: 'pending',
      worker_id: null,
      started_at: null,
      completed_at: null,
      duration: null,
      output: null
    },
    {
      task_id: 'task-001-002-6',
      node_id: 'node-6',
      node_name: 'Model Deployment',
      status: 'pending',
      worker_id: null,
      started_at: null,
      completed_at: null,
      duration: null,
      output: null
    }
  ],
  'run-001-003': [
    {
      task_id: 'task-001-003-1',
      node_id: 'node-1',
      node_name: 'Data Ingestion',
      status: 'completed',
      worker_id: 'worker-1',
      started_at: '2025-10-22T09:00:00',
      completed_at: '2025-10-22T09:02:10',
      duration: 130,
      output: { data_size: '15GB', records: 1500000 }
    },
    {
      task_id: 'task-001-003-2',
      node_id: 'node-2',
      node_name: 'Data Preprocessing',
      status: 'completed',
      worker_id: 'worker-2',
      started_at: '2025-10-22T09:02:15',
      completed_at: '2025-10-22T09:04:55',
      duration: 160,
      output: { cleaned_records: 1492000 }
    },
    {
      task_id: 'task-001-003-3',
      node_id: 'node-3',
      node_name: 'Feature Engineering',
      status: 'completed',
      worker_id: 'worker-3',
      started_at: '2025-10-22T09:02:15',
      completed_at: '2025-10-22T09:05:20',
      duration: 185,
      output: { features: 256 }
    },
    {
      task_id: 'task-001-003-4',
      node_id: 'node-4',
      node_name: 'Model Training',
      status: 'failed',
      worker_id: 'worker-1',
      started_at: '2025-10-22T09:05:25',
      completed_at: '2025-10-22T09:08:45',
      duration: 200,
      output: null,
      error: 'CUDA out of memory: tried to allocate 2.5GB'
    },
    {
      task_id: 'task-001-003-5',
      node_id: 'node-5',
      node_name: 'Model Validation',
      status: 'cancelled',
      worker_id: null,
      started_at: null,
      completed_at: null,
      duration: null,
      output: null
    },
    {
      task_id: 'task-001-003-6',
      node_id: 'node-6',
      node_name: 'Model Deployment',
      status: 'cancelled',
      worker_id: null,
      started_at: null,
      completed_at: null,
      duration: null,
      output: null
    }
  ]
};