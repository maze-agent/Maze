import { ChangeEvent, KeyboardEvent, useEffect, useRef, useState } from 'react';
import { Button, Dropdown, Input, Segmented, Tag, Typography, message } from 'antd';
import {
  CheckOutlined,
  DownloadOutlined,
  EditOutlined,
  MoreOutlined,
  PlayCircleOutlined,
  ProjectOutlined,
  SaveOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import { useWorkflowStore } from '@/stores/workflowStore';
import { api } from '@/api/client';
import type { TaskDefinition, WorkflowNode } from '@/types/workflow';

const { Text } = Typography;

interface ToolbarProps {
  onOpenClusterResources?: () => void;
}

export default function Toolbar({ onOpenClusterResources }: ToolbarProps) {
  const importInputRef = useRef<HTMLInputElement | null>(null);
  const { 
    workflowId, 
    workflowName, 
    workspaceId,
    workspaceDir,
    workspaceTasks,
    currentWorkspaceWorkflowPath,
    nodes, 
    edges, 
    isRunning,
    workflowSaveState,
    workflowDraftError,
    workflowSavedAt,
    workflowOperation,
    setWorkflowId,
    setWorkflowName,
    setNodes,
    setEdges,
    setWorkspaceDir,
    setWorkspaceContext,
    setWorkspaceTasks,
    selectNode,
    setCurrentWorkspaceWorkflowPath,
    setWorkspaceWorkflows,
    clearRunResults,
    setIsRunning,
    setActiveRun,
    acquireWorkflowOperation,
    releaseWorkflowOperation,
  } = useWorkflowStore();
  const [editingWorkflowName, setEditingWorkflowName] = useState(false);
  const [workflowNameDraft, setWorkflowNameDraft] = useState(workflowName);
  const [validatingWorkflow, setValidatingWorkflow] = useState(false);
  const [savingWorkflow, setSavingWorkflow] = useState(false);
  const [runningWorkflow, setRunningWorkflow] = useState(false);

  useEffect(() => {
    if (!editingWorkflowName) {
      setWorkflowNameDraft(workflowName);
    }
  }, [workflowName, editingWorkflowName]);

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

    if (nextName === workflowName) {
      setEditingWorkflowName(false);
      setWorkflowNameDraft(nextName);
      return;
    }

    const operationToken = acquireWorkflowOperation('Renaming workflow');
    if (!operationToken) {
      setEditingWorkflowName(false);
      setWorkflowNameDraft(workflowName);
      message.info(`Please wait for ${useWorkflowStore.getState().workflowOperation?.label || 'the current workflow operation'}`);
      return;
    }

    setEditingWorkflowName(false);
    setWorkflowNameDraft(nextName);
    setWorkflowName(nextName);

    try {
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
    } finally {
      releaseWorkflowOperation(operationToken);
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

  const stripNodeTaskCode = (node: WorkflowNode): WorkflowNode => {
    if (node.data.category !== 'workspace') {
      return node;
    }

    const { customCode, ...data } = node.data;

    return {
      ...node,
      data: {
        ...data,
        taskPath: normalizeTaskRelativePath(node.data.taskPath) || node.data.taskPath,
      },
    };
  };

  const getWorkspaceTaskCode = (relativePath: string) => {
    const normalizedPath = normalizeTaskRelativePath(relativePath);
    const task = workspaceTasks.find((item) => normalizeTaskRelativePath(item.relativePath) === normalizedPath);
    return task?.code || '';
  };

  const collectIncludedTasks = (workflowNodes: WorkflowNode[]): TaskDefinition[] => {
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
      const incomingCode = node.data.customCode || getWorkspaceTaskCode(relativePath);
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
      const workflowNodes = nodes.map(stripNodeTaskCode);
      const includedTasks = collectIncludedTasks(nodes);
      const payload = {
        schema: 'maze-playground-bundle',
        version: 3,
        exportedAt: new Date().toISOString(),
        workflow: {
          name: workflowName,
          sourceWorkflowId: workflowId,
          nodes: workflowNodes,
          edges,
        },
        includedTasks,
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
      message.success(`Workflow bundle exported with ${includedTasks.length} included workspace task${includedTasks.length === 1 ? '' : 's'}`);
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

    const operationToken = acquireWorkflowOperation('Importing workflow');
    if (!operationToken) {
      message.info(`Please wait for ${useWorkflowStore.getState().workflowOperation?.label || 'the current workflow operation'}`);
      return;
    }

    try {
      const text = await file.text();
      const payload = JSON.parse(text);
      const imported = await api.importWorkspaceWorkflow({
        workspaceId: workspaceId || undefined,
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
      setWorkspaceContext(imported);
      setWorkspaceDir(imported.workspaceDir);
      clearRunResults();

      const [tasksResult, workflowsResult] = await Promise.all([
        api.getWorkspaceTasks(imported.workspaceDir),
        api.getWorkspaceWorkflows(imported.workspaceDir),
      ]);
      setWorkspaceTasks(tasksResult.tasks || []);
      setWorkspaceWorkflows(workflowsResult.workflows || []);

      const importedCount = imported.importedTaskDefinitions?.imported.length || 0;
      const reusedCount = imported.importedTaskDefinitions?.skipped.filter((item) => item.reason === 'exists-same').length || 0;
      const remappedCount = imported.importedTaskDefinitions?.remapped?.length || 0;
      message.success(`Workflow imported. Tasks added: ${importedCount}, reused: ${reusedCount}, remapped: ${remappedCount}`);
    } catch (error) {
      console.error('Failed to import workflow:', error);
      message.error(error instanceof Error ? error.message : 'Failed to import workflow');
    } finally {
      releaseWorkflowOperation(operationToken);
    }
  };

  const handleSaveWorkflow = async () => {
    if (nodes.length === 0) {
      message.warning('Please add at least one task node before saving');
      return;
    }

    const operationToken = acquireWorkflowOperation('Saving workflow');
    if (!operationToken) {
      message.info(`Please wait for ${useWorkflowStore.getState().workflowOperation?.label || 'the current workflow operation'}`);
      return;
    }

    setSavingWorkflow(true);
    try {
      let activeWorkflowId = workflowId;
      if (!activeWorkflowId) {
        const created = await api.createWorkflow(workflowName);
        activeWorkflowId = created.workflowId;
        setWorkflowId(created.workflowId);
      }

      const saved = await api.saveWorkspaceWorkflow({
        workspaceId: workspaceId || undefined,
        workspaceDir: workspaceDir || (await api.getWorkspaceWorkflows()).workspaceDir,
        relativePath: currentWorkspaceWorkflowPath,
        name: workflowName,
        workflowId: activeWorkflowId,
        nodes,
        edges,
      });
      await api.saveWorkflow(activeWorkflowId, {
        name: workflowName,
        nodes: saved.workflow.nodes,
        edges: saved.workflow.edges,
      });
      setWorkspaceContext(saved);
      setWorkspaceDir(saved.workspaceDir);
      setCurrentWorkspaceWorkflowPath(saved.relativePath);
      const workflowsResult = await api.getWorkspaceWorkflows(saved.workspaceDir);
      setWorkspaceWorkflows(workflowsResult.workflows || []);
      message.success(`Workflow saved to ${saved.relativePath}`);
    } catch (error: any) {
      console.error('Failed to save workflow:', error);
      message.error(error.response?.data?.error || 'Failed to save workflow');
    } finally {
      setSavingWorkflow(false);
      releaseWorkflowOperation(operationToken);
    }
  };

  const handleValidateWorkflow = async () => {
    setValidatingWorkflow(true);
    try {
      if (nodes.length === 0) {
        message.warning('Workflow needs at least one task node');
        return;
      }
      const unconfiguredNodes = nodes.filter((node) => !node.data.configured);
      if (unconfiguredNodes.length > 0) {
        message.warning(`${unconfiguredNodes.length} task node${unconfiguredNodes.length === 1 ? '' : 's'} need configuration`);
        return;
      }
      const missingTaskBindings = nodes.flatMap((node) => (
        node.data.inputs
          .filter((input) => input.source === 'task' && (!input.taskSource?.taskId || !input.taskSource?.outputKey))
          .map((input) => `${node.data.label}.${input.name}`)
      ));
      if (missingTaskBindings.length > 0) {
        message.warning(`Missing task input binding: ${missingTaskBindings[0]}`);
        return;
      }
      message.success('Workflow structure validated');
    } finally {
      setValidatingWorkflow(false);
    }
  };

  const handleRunWorkflow = async () => {
    if (nodes.length === 0) {
      message.warning('Please add at least one task node before running');
      return;
    }

    const unconfiguredNodes = nodes.filter((node) => !node.data.configured);
    if (unconfiguredNodes.length > 0) {
      message.warning(`${unconfiguredNodes.length} task node${unconfiguredNodes.length === 1 ? '' : 's'} need configuration`);
      return;
    }

    const operationToken = acquireWorkflowOperation('Starting workflow run');
    if (!operationToken) {
      message.info(`Please wait for ${useWorkflowStore.getState().workflowOperation?.label || 'the current workflow operation'}`);
      return;
    }

    setRunningWorkflow(true);
    try {
      let activeWorkflowId = workflowId;
      if (!activeWorkflowId) {
        const created = await api.createWorkflow(workflowName);
        activeWorkflowId = created.workflowId;
        setWorkflowId(created.workflowId);
      }

      const activeWorkspaceDir = workspaceDir || (await api.getWorkspaceWorkflows()).workspaceDir;
      const saved = await api.saveWorkspaceWorkflow({
        workspaceId: workspaceId || undefined,
        workspaceDir: activeWorkspaceDir,
        relativePath: currentWorkspaceWorkflowPath,
        name: workflowName,
        workflowId: activeWorkflowId,
        nodes,
        edges,
      });

      await api.saveWorkflow(activeWorkflowId, {
        name: workflowName,
        nodes: saved.workflow.nodes,
        edges: saved.workflow.edges,
      });

      setWorkspaceContext(saved);
      setWorkspaceDir(saved.workspaceDir);
      setCurrentWorkspaceWorkflowPath(saved.relativePath);
      const workflowsResult = await api.getWorkspaceWorkflows(saved.workspaceDir);
      setWorkspaceWorkflows(workflowsResult.workflows || []);

      setIsRunning(true);
      const result = await api.runWorkflow(activeWorkflowId, saved.workspaceDir, workspaceId || undefined);
      setActiveRun(result.run);
      message.success(`Workflow run started: ${result.runId}`);
    } catch (error: any) {
      console.error('Failed to run workflow:', error);
      setIsRunning(false);
      message.error(error.response?.data?.error || error.message || 'Failed to run workflow');
    } finally {
      setRunningWorkflow(false);
      releaseWorkflowOperation(operationToken);
    }
  };

  const saveStateTag = () => {
    if (nodes.length === 0 || workflowSaveState === 'empty') {
      return <Tag>Empty</Tag>;
    }
    if (workflowSaveState === 'unsaved_draft') {
      return <Tag color="orange">Unsaved Draft</Tag>;
    }
    if (workflowSaveState === 'saving_draft') {
      return <Tag color="processing">Saving Draft</Tag>;
    }
    if (workflowSaveState === 'saved_draft') {
      return (
        <Tag color="blue" title={workflowSavedAt ? `Saved at ${new Date(workflowSavedAt).toLocaleString()}` : undefined}>
          Draft Saved
        </Tag>
      );
    }
    if (workflowSaveState === 'saved_workflow') {
      return (
        <Tag color="green" title={currentWorkspaceWorkflowPath || undefined}>
          Saved Workflow
        </Tag>
      );
    }
    return (
      <Tag color="red" title={workflowDraftError || undefined}>
        Draft Error
      </Tag>
    );
  };

  const workflowStatusTag = () => {
    if (isRunning) {
      return <Tag color="processing">Running</Tag>;
    }
    return saveStateTag();
  };

  const moreMenuItems = [
    {
      key: 'import',
      icon: <UploadOutlined />,
      label: 'Import workflow',
      disabled: isRunning || Boolean(workflowOperation),
    },
    {
      key: 'export',
      icon: <DownloadOutlined />,
      label: 'Export workflow',
      disabled: isRunning || Boolean(workflowOperation) || nodes.length === 0,
    },
  ];

  return (
    <div
      className="workbench-toolbar"
    >
      <div className="workbench-toolbar-left">
        <div className="workbench-brand">
          <ProjectOutlined className="workbench-brand-icon" />
          <Text strong className="workbench-brand-text">
            Maze Workbench
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
            className="workbench-workflow-name-input"
            disabled={Boolean(workflowOperation)}
          />
        ) : (
          <button
            type="button"
            className="workbench-workflow-name"
            onClick={() => setEditingWorkflowName(true)}
            disabled={Boolean(workflowOperation)}
            title="Click to rename workflow"
          >
            <span>{workflowName}</span>
            <EditOutlined />
          </button>
        )}
      </div>

      <Segmented
        className="workbench-topbar-modes"
        value="Design"
        options={[
          {
            value: 'Design',
            label: <span className="workbench-topbar-mode-option">Design</span>,
          },
          {
            value: 'Cluster',
            label: <span className="workbench-topbar-mode-option">Cluster</span>,
          },
        ]}
        onChange={(value) => {
          if (value === 'Cluster') {
            onOpenClusterResources?.();
          }
        }}
      />

      <div className="workbench-toolbar-right">
        {workflowStatusTag()}

        <input
          ref={importInputRef}
          type="file"
          accept="application/json,.json"
          onChange={handleImportWorkflow}
          style={{ display: 'none' }}
        />

        <Button
          icon={<SaveOutlined />}
          onClick={handleSaveWorkflow}
          loading={savingWorkflow}
          disabled={isRunning || Boolean(workflowOperation) || nodes.length === 0}
        >
          Save
        </Button>

        <Button
          icon={<CheckOutlined />}
          onClick={handleValidateWorkflow}
          loading={validatingWorkflow}
          disabled={isRunning || Boolean(workflowOperation) || runningWorkflow || nodes.length === 0}
        >
          Validate
        </Button>

        <Button
          type="primary"
          icon={<PlayCircleOutlined />}
          onClick={handleRunWorkflow}
          loading={runningWorkflow}
          disabled={isRunning || Boolean(workflowOperation) || nodes.length === 0}
          className="workbench-run-button"
        >
          Run
        </Button>

        <Dropdown
          menu={{
            items: moreMenuItems,
            onClick: ({ key }) => {
              if (key === 'import') {
                importInputRef.current?.click();
              }
              if (key === 'export') {
                handleExportWorkflow();
              }
            },
          }}
          trigger={['click']}
          placement="bottomRight"
        >
          <Button
            icon={<MoreOutlined />}
            aria-label="More workflow actions"
            disabled={isRunning || Boolean(workflowOperation)}
          />
        </Dropdown>
      </div>
    </div>
  );
}
