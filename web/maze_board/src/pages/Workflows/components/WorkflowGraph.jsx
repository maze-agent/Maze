import React from 'react';
import WorkflowNode from './WorkflowNode';
import WorkflowEdge from './WorkflowEdge';

const WorkflowGraph = ({ nodes, edges, isStatic = true, taskExecutions = [] }) => {
    // 如果不是静态模式，从执行数据中获取节点状态和 Worker 信息
    const getNodeStatus = (nodeId) => {
        if (isStatic) return null;
        const execution = taskExecutions.find(e => e.node_id === nodeId);
        return execution?.status || 'pending';
    };

    const getNodeWorker = (nodeId) => {
        if (isStatic) return null;
        const execution = taskExecutions.find(e => e.node_id === nodeId);
        return execution?.worker_id || null;
    };

    return (
        <div className="relative bg-gray-900 rounded-lg" style={{ height: '500px', overflow: 'hidden' }}>
            {/* 背景网格 */}
            <div
                className="absolute inset-0"
                style={{
                    backgroundImage: 'linear-gradient(rgba(255,255,255,0.05) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.05) 1px, transparent 1px)',
                    backgroundSize: '20px 20px'
                }}
            />

            {/* 连接线 */}
            <svg className="absolute top-0 left-0 w-full h-full pointer-events-none" style={{ zIndex: 0 }}>
                <defs>
                    <marker
                        id="arrowhead"
                        markerWidth="10"
                        markerHeight="10"
                        refX="9"
                        refY="3"
                        orient="auto"
                    >
                        <polygon points="0 0, 10 3, 0 6" fill="#6b7280" />
                    </marker>
                </defs>
                {edges.map((edge, idx) => (
                    <WorkflowEdge
                        key={idx}
                        from={edge.from}
                        to={edge.to}
                        nodes={nodes}
                    />
                ))}
            </svg>

            {/* 节点 */}
            {nodes.map(node => (
                <WorkflowNode
                    key={node.id}
                    node={node}
                    status={getNodeStatus(node.id)}
                    workerId={getNodeWorker(node.id)}
                    isStatic={isStatic}
                />
            ))}
        </div>
    );
};

export default WorkflowGraph;