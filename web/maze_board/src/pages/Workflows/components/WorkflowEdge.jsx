
import React from 'react';

const WorkflowEdge = ({ from, to, nodes }) => {
    const fromNode = nodes.find(n => n.id === from);
    const toNode = nodes.find(n => n.id === to);

    if (!fromNode || !toNode) return null;

    const x1 = fromNode.x + 70; // Center of node
    const y1 = fromNode.y + 25;
    const x2 = toNode.x + 70;
    const y2 = toNode.y + 25;

    // 创建曲线路径
    const midX = (x1 + x2) / 2;
    const path = `M ${x1} ${y1} Q ${midX} ${y1}, ${midX} ${(y1 + y2) / 2} T ${x2} ${y2}`;

    return (
        <path
            d={path}
            stroke="#6b7280"
            strokeWidth="2"
            fill="none"
            markerEnd="url(#arrowhead)"
        />
    );
};

export default WorkflowEdge;