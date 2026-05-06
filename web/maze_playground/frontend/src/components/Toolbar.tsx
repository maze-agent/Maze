import { ChangeEvent, useRef } from 'react';
import { Button, Space, Typography, message } from 'antd';
import { DownloadOutlined, PlayCircleOutlined, PlusOutlined, ProjectOutlined, UploadOutlined } from '@ant-design/icons';
import { useWorkflowStore } from '@/stores/workflowStore';
import { api } from '@/api/client';
import type { WorkflowEdge, WorkflowNode } from '@/types/workflow';

const { Text } = Typography;

export default function Toolbar() {
  const importInputRef = useRef<HTMLInputElement | null>(null);
  const { 
    workflowId, 
    workflowName, 
    nodes, 
    edges, 
    isRunning,
    setWorkflowId,
    setWorkflowName,
    setNodes,
    setEdges,
    selectNode,
    setIsRunning,
    clearRunResults,
  } = useWorkflowStore();

  const handleCreateWorkflow = async () => {
    try {
      const { workflowId: newId } = await api.createWorkflow();
      setWorkflowId(newId);
      message.success('Workflow created successfully');
    } catch (error) {
      console.error('Failed to create workflow:', error);
      message.error('Failed to create workflow');
    }
  };

  const getExportFileName = () => {
    const safeName = workflowName
      .trim()
      .replace(/[^a-zA-Z0-9-_]+/g, '-')
      .replace(/^-+|-+$/g, '') || 'maze-workflow';
    const date = new Date().toISOString().slice(0, 10);
    return `${safeName}-${date}.json`;
  };

  const handleExportWorkflow = () => {
    if (nodes.length === 0) {
      message.warning('Please add at least one task node before exporting');
      return;
    }

    try {
      const payload = {
        schema: 'maze-playground-workflow',
        version: 1,
        exportedAt: new Date().toISOString(),
        workflow: {
          name: workflowName,
          sourceWorkflowId: workflowId,
          nodes,
          edges,
        },
      };

      const blob = new Blob([JSON.stringify(payload, null, 2)], {
        type: 'application/json',
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = getExportFileName();
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
      message.success('Workflow exported');
    } catch (error) {
      console.error('Failed to export workflow:', error);
      message.error('Failed to export workflow');
    }
  };

  const normalizeImportedWorkflow = (payload: any): {
    name: string;
    nodes: WorkflowNode[];
    edges: WorkflowEdge[];
  } => {
    const workflow = payload?.workflow || payload;
    const importedNodes = workflow?.nodes;
    const importedEdges = workflow?.edges;

    if (!Array.isArray(importedNodes) || !Array.isArray(importedEdges)) {
      throw new Error('Invalid workflow file: nodes and edges are required');
    }

    const normalizedNodes = importedNodes.map((node: any) => {
      if (!node?.id || !node?.data || !node?.position) {
        throw new Error('Invalid workflow file: every node needs id, position, and data');
      }

      return {
        ...node,
        type: 'taskNode' as const,
      };
    });

    const normalizedEdges = importedEdges.map((edge: any) => {
      if (!edge?.id || !edge?.source || !edge?.target) {
        throw new Error('Invalid workflow file: every edge needs id, source, and target');
      }

      return {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        sourceHandle: edge.sourceHandle || undefined,
        targetHandle: edge.targetHandle || undefined,
      };
    });

    return {
      name: workflow?.name || 'Imported Workflow',
      nodes: normalizedNodes,
      edges: normalizedEdges,
    };
  };

  const handleImportWorkflow = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';

    if (!file) {
      return;
    }

    try {
      const text = await file.text();
      const payload = JSON.parse(text);
      const imported = normalizeImportedWorkflow(payload);
      const { workflowId: newId } = await api.createWorkflow(imported.name);

      await api.saveWorkflow(newId, {
        nodes: imported.nodes,
        edges: imported.edges,
      });

      setWorkflowId(newId);
      setWorkflowName(imported.name);
      setNodes(imported.nodes);
      setEdges(imported.edges);
      selectNode(null);
      clearRunResults();
      message.success('Workflow imported');
    } catch (error) {
      console.error('Failed to import workflow:', error);
      message.error(error instanceof Error ? error.message : 'Failed to import workflow');
    }
  };

  const handleRunWorkflow = async () => {
    if (!workflowId) {
      message.warning('Please create a workflow first');
      return;
    }

    if (nodes.length === 0) {
      message.warning('Please add at least one task node');
      return;
    }

    const unconfiguredNodes = nodes.filter(n => !n.data.configured);
    if (unconfiguredNodes.length > 0) {
      message.warning('Some nodes are not configured, please configure all nodes first');
      return;
    }

    try {
      await api.saveWorkflow(workflowId, { nodes, edges });
      setIsRunning(true);
      clearRunResults();
      await api.runWorkflow(workflowId);
      message.info('Workflow started running');
      
    } catch (error) {
      console.error('Failed to run workflow:', error);
      message.error('Failed to run workflow');
      setIsRunning(false);
    }
  };

  return (
    <div
      style={{
        height: '60px',
        borderBottom: '1px solid #f0f0f0',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '0 24px',
        background: '#fff',
      }}
    >
      <Space size="large">
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <ProjectOutlined style={{ fontSize: '24px', color: '#1890ff' }} />
          <Text strong style={{ fontSize: '16px' }}>
            Maze Workflow Playground
          </Text>
        </div>
        
        {workflowId && (
          <Text type="secondary" style={{ fontSize: '14px' }}>
            {workflowName} ({workflowId.substring(0, 8)}...)
          </Text>
        )}
      </Space>

      <Space>
        <input
          ref={importInputRef}
          type="file"
          accept="application/json,.json"
          onChange={handleImportWorkflow}
          style={{ display: 'none' }}
        />

        <Button 
          type="primary" 
          icon={<PlusOutlined />}
          onClick={handleCreateWorkflow}
        >
          New Workflow
        </Button>
        
        <Button 
          icon={<UploadOutlined />}
          onClick={() => importInputRef.current?.click()}
          disabled={isRunning}
        >
          Import
        </Button>

        <Button 
          icon={<DownloadOutlined />}
          onClick={handleExportWorkflow}
          disabled={isRunning || nodes.length === 0}
        >
          Export
        </Button>
        
        <Button 
          type="primary"
          icon={<PlayCircleOutlined />}
          onClick={handleRunWorkflow}
          disabled={!workflowId || isRunning}
          loading={isRunning}
          style={{ background: '#52c41a', borderColor: '#52c41a' }}
        >
          {isRunning ? 'Running...' : 'Run'}
        </Button>
      </Space>
    </div>
  );
}
