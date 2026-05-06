import { ChangeEvent, KeyboardEvent, useEffect, useRef, useState } from 'react';
import { Button, Input, Space, Typography, message } from 'antd';
import { DownloadOutlined, PlayCircleOutlined, PlusOutlined, ProjectOutlined, UploadOutlined } from '@ant-design/icons';
import { useWorkflowStore } from '@/stores/workflowStore';
import { api } from '@/api/client';
import type { TaskDefinition, WorkflowNode } from '@/types/workflow';

const { Text } = Typography;

export default function Toolbar() {
  const importInputRef = useRef<HTMLInputElement | null>(null);
  const { 
    workflowId, 
    workflowName, 
    workspaceDir,
    currentWorkspaceWorkflowPath,
    nodes, 
    edges, 
    isRunning,
    setWorkflowId,
    setWorkflowName,
    setNodes,
    setEdges,
    setWorkspaceDir,
    setWorkspaceTasks,
    selectNode,
    setCurrentWorkspaceWorkflowPath,
    setWorkspaceWorkflows,
    setIsRunning,
    clearRunResults,
  } = useWorkflowStore();
  const [editingWorkflowName, setEditingWorkflowName] = useState(false);
  const [workflowNameDraft, setWorkflowNameDraft] = useState(workflowName);

  useEffect(() => {
    if (!editingWorkflowName) {
      setWorkflowNameDraft(workflowName);
    }
  }, [workflowName, editingWorkflowName]);

  const handleCreateWorkflow = async () => {
    try {
      const { workflowId: newId } = await api.createWorkflow(workflowName);
      setWorkflowId(newId);
      setCurrentWorkspaceWorkflowPath(null);
      message.success('Workflow created successfully');
    } catch (error) {
      console.error('Failed to create workflow:', error);
      message.error('Failed to create workflow');
    }
  };

  const refreshWorkspaceWorkflows = async () => {
    if (!workspaceDir) {
      return;
    }

    try {
      const result = await api.getWorkspaceWorkflows(workspaceDir);
      setWorkspaceWorkflows(result.workflows || []);
    } catch (error) {
      console.error('Failed to refresh workspace workflows:', error);
    }
  };

  const commitWorkflowName = async () => {
    const nextName = workflowNameDraft.trim() || 'Untitled Workflow';
    setEditingWorkflowName(false);
    setWorkflowNameDraft(nextName);

    if (nextName === workflowName) {
      return;
    }

    setWorkflowName(nextName);

    if (workflowId) {
      try {
        await api.saveWorkflow(workflowId, {
          name: nextName,
          nodes,
          edges,
        });
      } catch (error) {
        console.error('Failed to update workflow name:', error);
        message.error('Failed to update workflow name');
      }
    }

    if (currentWorkspaceWorkflowPath && workspaceDir) {
      try {
        await api.saveWorkspaceWorkflow({
          workspaceDir,
          relativePath: currentWorkspaceWorkflowPath,
          name: nextName,
          workflowId,
          nodes,
          edges,
        });
        await refreshWorkspaceWorkflows();
      } catch (error) {
        console.error('Failed to update workspace workflow file:', error);
        message.error('Failed to update workspace workflow file');
      }
    }
  };

  const cancelWorkflowRename = () => {
    setWorkflowNameDraft(workflowName);
    setEditingWorkflowName(false);
  };

  const handleWorkflowNameKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter') {
      event.currentTarget.blur();
    }
    if (event.key === 'Escape') {
      cancelWorkflowRename();
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

  const normalizeTaskRelativePath = (relativePath?: string) => {
    let normalized = String(relativePath || '').trim().replace(/\\/g, '/').replace(/^\/+/, '');
    if (!normalized) {
      return null;
    }
    if (!normalized.startsWith('tasks/')) {
      normalized = `tasks/${normalized}`;
    }
    if (!normalized.endsWith('.py')) {
      normalized = `${normalized}.py`;
    }
    return normalized;
  };

  const collectWorkflowTaskDefinitions = (workflowNodes: WorkflowNode[]): TaskDefinition[] => {
    const definitions = new Map<string, TaskDefinition>();

    workflowNodes.forEach((node) => {
      if (node.data.category !== 'workspace') {
        return;
      }

      const relativePath = normalizeTaskRelativePath(node.data.taskPath);
      if (!relativePath) {
        return;
      }

      const existing = definitions.get(relativePath);
      const incomingCode = node.data.customCode || '';
      const code = incomingCode.trim() ? incomingCode : existing?.code || '';

      definitions.set(relativePath, {
        type: 'workspace',
        ...(existing || {}),
        relativePath,
        functionName: node.data.functionName,
        displayName: node.data.label,
        code,
        inputs: node.data.inputs,
        outputs: node.data.outputs,
        resources: node.data.resources,
      });
    });

    return Array.from(definitions.values());
  };

  const handleExportWorkflow = () => {
    if (nodes.length === 0) {
      message.warning('Please add at least one task node before exporting');
      return;
    }

    try {
      const taskDefinitions = collectWorkflowTaskDefinitions(nodes);
      const payload = {
        schema: 'maze-playground-workflow',
        version: 2,
        exportedAt: new Date().toISOString(),
        workflow: {
          name: workflowName,
          sourceWorkflowId: workflowId,
          nodes,
          edges,
          taskDefinitions,
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
      message.success(`Workflow exported with ${taskDefinitions.length} workspace task definition${taskDefinitions.length === 1 ? '' : 's'}`);
    } catch (error) {
      console.error('Failed to export workflow:', error);
      message.error('Failed to export workflow');
    }
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
      const imported = await api.importWorkspaceWorkflow({
        workspaceDir: workspaceDir || undefined,
        payload,
      });
      const { workflow } = imported;
      const { workflowId: newId } = await api.createWorkflow(workflow.name);

      await api.saveWorkflow(newId, {
        name: workflow.name,
        nodes: workflow.nodes,
        edges: workflow.edges,
      });

      setWorkflowId(newId);
      setWorkflowName(workflow.name);
      setNodes(workflow.nodes);
      setEdges(workflow.edges);
      selectNode(null);
      setCurrentWorkspaceWorkflowPath(null);
      setWorkspaceDir(imported.workspaceDir);
      clearRunResults();

      const [tasksResult, workflowsResult] = await Promise.all([
        api.getWorkspaceTasks(imported.workspaceDir),
        api.getWorkspaceWorkflows(imported.workspaceDir),
      ]);
      setWorkspaceTasks(tasksResult.tasks || []);
      setWorkspaceWorkflows(workflowsResult.workflows || []);

      const importedCount = imported.importedTaskDefinitions?.imported.length || 0;
      const skippedCount = imported.importedTaskDefinitions?.skipped.filter((item) => item.reason === 'exists').length || 0;
      message.success(`Workflow imported. Tasks added: ${importedCount}, skipped existing: ${skippedCount}`);
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
      await api.saveWorkflow(workflowId, { name: workflowName, nodes, edges });
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
      <Space size="large" style={{ minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <ProjectOutlined style={{ fontSize: '24px', color: '#1890ff' }} />
          <Text strong style={{ fontSize: '16px' }}>
            Maze Workflow Playground
          </Text>
        </div>
        
        {editingWorkflowName ? (
          <Input
            autoFocus
            size="small"
            value={workflowNameDraft}
            onChange={(event) => setWorkflowNameDraft(event.target.value)}
            onBlur={commitWorkflowName}
            onKeyDown={handleWorkflowNameKeyDown}
            style={{ width: '260px' }}
          />
        ) : (
          <Text
            type="secondary"
            onClick={() => setEditingWorkflowName(true)}
            title="Click to rename workflow"
            style={{
              fontSize: '14px',
              cursor: 'text',
              maxWidth: '360px',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {workflowName}{workflowId ? ` (${workflowId.substring(0, 8)}...)` : ''}
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
