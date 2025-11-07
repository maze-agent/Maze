import React from 'react';
import { NODE_TYPE_COLORS, STATUS_COLORS } from '../../../utils/constants';

const WorkflowNode = ({ node, status, workerId, isStatic }) => {
  const getStatusStyle = () => {
    if (isStatic) return 'border-gray-300';
    return STATUS_COLORS[status] || 'border-gray-500';
  };

  return (
    <div
      className={`absolute bg-gray-800 ${getStatusStyle()} border-2 rounded-lg p-3 shadow-lg min-w-[140px] text-center`}
      style={{ left: `${node.x}px`, top: `${node.y}px`, zIndex: 10 }}
    >
      {/* 节点类型标签 */}
      <div className={`text-xs ${NODE_TYPE_COLORS[node.type]} text-white px-2 py-1 rounded mb-2`}>
        {node.type}
      </div>

      {/* 节点名称 */}
      <div className="text-xs font-semibold text-white mb-1">{node.name}</div>

      {/* 执行状态（仅在动态模式显示） */}
      {!isStatic && status && (
        <div className={`text-[10px] mt-1 ${
          status === 'completed' ? 'text-green-300' :
          status === 'running' ? 'text-yellow-300' :
          status === 'failed' ? 'text-red-300' :
          'text-gray-400'
        }`}>
          {status}
        </div>
      )}

      {/* Worker 信息（仅在动态模式显示） */}
      {!isStatic && workerId && (
        <div className="text-[10px] text-blue-300 mt-1">
          🖥️ {workerId}
        </div>
      )}
    </div>
  );
};

export default WorkflowNode;