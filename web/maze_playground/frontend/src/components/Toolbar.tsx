import { ChangeEvent, KeyboardEvent, useEffect, useRef, useState } from 'react';
import { Button, Input, Space, Tag, Typography, message } from 'antd';
import { ClusterOutlined, DownloadOutlined, HistoryOutlined, PlayCircleOutlined, PlusOutlined, ProjectOutlined, ThunderboltOutlined, UploadOutlined } from '@ant-design/icons';
import { useWorkflowStore } from '@/stores/workflowStore';
import { api } from '@/api/client';
import type { LocalWorkspaceFileMeta, TaskDefinition, WorkflowNode } from '@/types/workflow';
import { loadLlmSettings } from '@/utils/llmSettings';
import { computeReactRunTimeout, normalizeReactTaskTimeout } from '@/utils/reactRuntime';

const { Text } = Typography;

function joinWorkspacePath(base: string, name: string) {
  return [base, name].filter(Boolean).join('/');
}

function normalizeLocalRelativePath(path: string) {
  return path
    .replace(/\\/g, '/')
    .split('/')
    .filter((part) => part && part !== '.' && part !== '..')
    .join('/');
}

async function fileToBase64(file: File) {
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
  return dataUrl.includes(',') ? dataUrl.split(',')[1] : dataUrl;
}

async function collectLocalWorkspaceFiles(
  directoryHandle: any,
  basePath = '',
): Promise<LocalWorkspaceFileMeta[]> {
  const files: LocalWorkspaceFileMeta[] = [];

  for await (const [name, handle] of directoryHandle.entries()) {
    const relativePath = normalizeLocalRelativePath(joinWorkspacePath(basePath, name));
    if (!relativePath) {
      continue;
    }
    if (handle.kind === 'directory') {
      files.push({
        name,
        relativePath,
        type: 'directory',
        size: null,
        updatedAt: null,
      });
      files.push(...await collectLocalWorkspaceFiles(handle, relativePath));
    } else if (handle.kind === 'file') {
      const file = await handle.getFile();
      files.push({
        name,
        relativePath,
        type: 'file',
        size: file.size,
        updatedAt: new Date(file.lastModified).toISOString(),
      });
    }
  }

  return files;
}

async function getFileFromLocalWorkspace(directoryHandle: any, relativePath: string): Promise<File | null> {
  const normalizedPath = normalizeLocalRelativePath(relativePath);
  if (directoryHandle?.kind === 'fileMap') {
    return directoryHandle.filesByPath?.get(normalizedPath) || null;
  }

  const parts = normalizedPath.split('/').filter(Boolean);
  if (parts.length === 0) {
    return null;
  }

  let handle = directoryHandle;
  for (let index = 0; index < parts.length; index += 1) {
    const part = parts[index];
    if (index === parts.length - 1) {
      const fileHandle = await handle.getFileHandle(part);
      return fileHandle.getFile();
    }
    handle = await handle.getDirectoryHandle(part);
  }
  return null;
}

interface ToolbarProps {
  onOpenRuns?: () => void;
  onOpenReactRunner?: () => void;
  onReactRunStarted?: (runId: string) => void;
  onOpenClusterResources?: () => void;
}

export default function Toolbar({ onOpenRuns, onOpenReactRunner, onReactRunStarted, onOpenClusterResources }: ToolbarProps) {
  const importInputRef = useRef<HTMLInputElement | null>(null);
  const { 
    workflowId, 
    workflowName, 
    workspaceId,
    workspaceDir,
    workspaceTasks,
    currentWorkspaceWorkflowPath,
    localWorkspaceId,
    localWorkspaceName,
    localWorkspaceHandle,
    localWorkspaceFiles,
    nodes, 
    edges, 
    isRunning,
    workflowSaveState,
    workflowDraftError,
    workflowSavedAt,
    setWorkflowId,
    setWorkflowName,
    setNodes,
    setEdges,
    setWorkspaceDir,
    setWorkspaceContext,
    setWorkspaceTasks,
    setLocalWorkspaceFiles,
    selectNode,
    setCurrentWorkspaceWorkflowPath,
    setWorkspaceWorkflows,
    setIsRunning,
    setActiveRun,
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

  const refreshLocalWorkspaceManifest = async () => {
    if (!localWorkspaceId || !localWorkspaceName || !localWorkspaceHandle) {
      return localWorkspaceFiles;
    }

    let files = localWorkspaceFiles;
    if (localWorkspaceHandle.kind === 'fileMap') {
      files = Array.from<[string, File]>(localWorkspaceHandle.filesByPath.entries()).map(([relativePath, file]) => ({
        name: file.name,
        relativePath,
        type: 'file' as const,
        size: file.size,
        updatedAt: new Date(file.lastModified).toISOString(),
      })).filter((file) => file.relativePath);
    } else {
      files = await collectLocalWorkspaceFiles(localWorkspaceHandle);
    }

    const version = Date.now().toString();
    await api.updateLocalWorkspaceManifest(localWorkspaceId, {
      displayName: localWorkspaceName,
      version,
      files,
    });
    setLocalWorkspaceFiles(files, version);
    return files;
  };

  const hydrateMissingLocalWorkspaceFiles = async () => {
    if (!localWorkspaceId || !localWorkspaceName || !localWorkspaceHandle) {
      return;
    }

    const files = await refreshLocalWorkspaceManifest();
    const localFilePaths = files
      .filter((file) => file.type === 'file')
      .map((file) => file.relativePath);
    if (localFilePaths.length === 0) {
      return;
    }

    const missingResult = await api.getMissingWorkspaceFiles({
      workspaceId: workspaceId || undefined,
      workspaceDir: workspaceDir || undefined,
      paths: localFilePaths,
    });
    if (!missingResult.missing.length) {
      message.info('Local file cache is already available in Workspace Files');
      return;
    }

    const hideLoading = message.loading(`Hydrating ${missingResult.missing.length} local file${missingResult.missing.length === 1 ? '' : 's'} into file sandbox...`, 0);
    try {
      for (const relativePath of missingResult.missing) {
        const file = await getFileFromLocalWorkspace(localWorkspaceHandle, relativePath);
        if (!file) {
          throw new Error(`Local file is no longer available: ${relativePath}`);
        }
        await api.uploadWorkspaceFile({
          workspaceId: workspaceId || undefined,
          workspaceDir: missingResult.workspaceDir,
          relativePath,
          contentBase64: await fileToBase64(file),
        });
      }
      message.success(`Hydrated ${missingResult.missing.length} local file${missingResult.missing.length === 1 ? '' : 's'} into file sandbox`);
    } finally {
      hideLoading();
    }
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
    }
  };

  const handleRunWorkflow = async () => {
    if (nodes.length === 0) {
      message.warning('Please add at least one task node');
      return;
    }

    let activeWorkflowId = workflowId;
    if (!activeWorkflowId) {
      try {
        const created = await api.createWorkflow(workflowName);
        activeWorkflowId = created.workflowId;
        setWorkflowId(created.workflowId);
      } catch (error) {
        console.error('Failed to create workflow before run:', error);
        message.error('Failed to create workflow before run');
        return;
      }
    }

    const agentNodes = nodes.filter((node) => node.data.category === 'agent');
    if (agentNodes.length > 0) {
      if (agentNodes.length > 1 || nodes.length > 1 || edges.length > 0) {
        message.warning('Run one ReAct workflow node at a time. Mixed static and agent workflows are not supported yet.');
        return;
      }

      const agentNode = agentNodes[0];
      if (agentNode.data.agentKind !== 'react') {
        message.warning('Unsupported agent workflow');
        return;
      }

      const prompt = (agentNode.data.prompt || '').trim();
      if (!prompt) {
        message.warning('Please configure a ReAct prompt');
        return;
      }

      const mode = agentNode.data.reactMode || 'local';
      const settings = loadLlmSettings();
      if (mode === 'online' && (!settings.baseUrl.trim() || !settings.model.trim() || !settings.apiKey.trim())) {
        message.warning('Please configure online LLM settings first');
        return;
      }

      setIsRunning(true);
      try {
        await hydrateMissingLocalWorkspaceFiles();
        const maxSteps = agentNode.data.maxSteps || 4;
        const taskTimeout = normalizeReactTaskTimeout(agentNode.data.taskTimeout, mode);
        const started = await api.startReactRun({
          mode,
          prompt,
          workspaceId: workspaceId || undefined,
          workspaceDir: workspaceDir || undefined,
          maxSteps,
          maxTokens: agentNode.data.maxTokens || 2048,
          timeoutSeconds: computeReactRunTimeout(maxSteps, taskTimeout),
          taskTimeout,
          skills: agentNode.data.skills || [],
          execBackend: agentNode.data.execBackend || 'workspace_sandbox',
          llm: mode === 'online' ? settings : undefined,
        });
        message.success(`ReAct run started: ${started.runId.slice(0, 8)}...`);
        onReactRunStarted?.(started.runId);
      } catch (error: any) {
        console.error('Failed to run ReAct workflow:', error);
        message.error(error.response?.data?.error || 'Failed to run ReAct workflow');
      } finally {
        setIsRunning(false);
      }
      return;
    }

    const unconfiguredNodes = nodes.filter(n => !n.data.configured);
    if (unconfiguredNodes.length > 0) {
      message.warning('Some nodes are not configured, please configure all nodes first');
      return;
    }

    try {
      setIsRunning(true);
      await api.saveWorkflow(activeWorkflowId, { name: workflowName, nodes, edges });
      await hydrateMissingLocalWorkspaceFiles();
      clearRunResults();
      const started = await api.runWorkflow(activeWorkflowId, workspaceDir || undefined, workspaceId || undefined);
      setActiveRun(started.run);
      message.info('Workflow started running');
      
    } catch (error) {
      console.error('Failed to run workflow:', error);
      message.error('Failed to run workflow');
      setIsRunning(false);
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
        {saveStateTag()}
      </Space>

      <Space>
        <Button
          icon={<ThunderboltOutlined />}
          onClick={onOpenReactRunner}
        >
          ReAct
        </Button>

        <Button
          icon={<HistoryOutlined />}
          onClick={onOpenRuns}
        >
          Runs
        </Button>

        <Button
          icon={<ClusterOutlined />}
          onClick={onOpenClusterResources}
        >
          Cluster
        </Button>

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
          disabled={nodes.length === 0 || isRunning}
          loading={isRunning}
          style={{ background: '#52c41a', borderColor: '#52c41a' }}
        >
          {isRunning ? 'Running...' : 'Run'}
        </Button>
      </Space>
    </div>
  );
}
