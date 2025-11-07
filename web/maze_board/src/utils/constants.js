// 状态相关常量
export const WORKER_STATUS = {
  ACTIVE: 'active',
  IDLE: 'idle',
  OFFLINE: 'offline'
};

export const NODE_STATUS = {
  COMPLETED: 'completed',
  RUNNING: 'running',
  PENDING: 'pending',
  FAILED: 'failed'
};

// 状态颜色映射
export const STATUS_COLORS = {
  active: 'bg-green-900 text-green-300',
  idle: 'bg-yellow-900 text-yellow-300',
  offline: 'bg-red-900 text-red-300',
  completed: 'border-green-400',
  running: 'border-yellow-400 animate-pulse',
  pending: 'border-gray-500',
  failed: 'border-red-400'
};

// 节点类型颜色
export const NODE_TYPE_COLORS = {
  input: 'bg-blue-600',
  transform: 'bg-purple-600',
  compute: 'bg-green-600',
  output: 'bg-orange-600'
};