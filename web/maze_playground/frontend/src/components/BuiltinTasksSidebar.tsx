import { ChangeEvent, KeyboardEvent, useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Divider, Drawer, Empty, Input, List, message, Modal, Space, Tag, Tooltip, Typography } from 'antd';
import {
  CodeOutlined,
  DownloadOutlined,
  EditOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PlusOutlined,
  ReloadOutlined,
  SaveOutlined,
  ThunderboltOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import { api } from '@/api/client';
import { useWorkflowStore } from '@/stores/workflowStore';
import type { BuiltinTaskMeta, WorkflowNode, WorkspaceTaskMeta, WorkspaceWorkflowMeta } from '@/types/workflow';

const { Text, Paragraph } = Typography;

const defaultWorkspaceTaskCode = (functionName: string) => `from maze import task

@task(
    inputs=["text"],
    outputs=["result"],
    resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0}
)
def ${functionName}(params):
    """Process text and return a result."""
    text = params.get("text", "")
    return {"result": f"Processed: {text}"}
`;

export default function BuiltinTasksSidebar() {
  const {
    builtinTasks,
    setBuiltinTasks,
    workspaceDir,
    setWorkspaceDir,
    workspaceTasks,
    setWorkspaceTasks,
    workspaceWorkflows,
    setWorkspaceWorkflows,
    currentWorkspaceWorkflowPath,
    setCurrentWorkspaceWorkflowPath,
    workflowId,
    setWorkflowId,
    workflowName,
    setWorkflowName,
    nodes,
    edges,
    setNodes,
    setEdges,
    addNode,
    selectNode,
    clearRunResults,
  } = useWorkflowStore();

  const [builtinLoading, setBuiltinLoading] = useState(false);
  const [workspaceLoading, setWorkspaceLoading] = useState(false);
  const [workflowLoading, setWorkflowLoading] = useState(false);
  const [savingWorkflow, setSavingWorkflow] = useState(false);
  const [creatingTask, setCreatingTask] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [workspaceInput, setWorkspaceInput] = useState(workspaceDir);
  const [workspaceErrors, setWorkspaceErrors] = useState<Array<{ relativePath: string; error: string }>>([]);
  const [expandedTaskKey, setExpandedTaskKey] = useState<string | null>(null);
  const [selectedWorkspaceTaskKey, setSelectedWorkspaceTaskKey] = useState<string | null>(null);
  const [selectedWorkflowPath, setSelectedWorkflowPath] = useState<string | null>(null);
  const [editingWorkflowPath, setEditingWorkflowPath] = useState<string | null>(null);
  const [workflowNameDraft, setWorkflowNameDraft] = useState('');
  const [editingTaskKey, setEditingTaskKey] = useState<string | null>(null);
  const [taskNameDraft, setTaskNameDraft] = useState('');
  const [importingTaskKey, setImportingTaskKey] = useState<string | null>(null);
  const [workspaceTaskEditor, setWorkspaceTaskEditor] = useState<WorkspaceTaskMeta | null>(null);
  const [workspaceTaskEditorCode, setWorkspaceTaskEditorCode] = useState('');
  const [workspaceTaskEditorError, setWorkspaceTaskEditorError] = useState<string | null>(null);
  const [savingWorkspaceTaskEditor, setSavingWorkspaceTaskEditor] = useState(false);
  const creatingWorkflowRef = useRef<Promise<string> | null>(null);
  const draggingWorkspaceRef = useRef(false);
  const taskImportInputRef = useRef<HTMLInputElement | null>(null);
  const taskImportTargetRef = useRef<WorkspaceTaskMeta | null>(null);

  useEffect(() => {
    loadWorkspaceTasks();
    loadWorkspaceWorkflows();
    if (builtinTasks.length === 0) {
      loadBuiltinTasks(false);
    }
  }, []);

  const ensureWorkflow = async () => {
    if (workflowId) {
      return workflowId;
    }

    if (!creatingWorkflowRef.current) {
      const hideLoading = message.loading('Creating workflow...', 0);
      creatingWorkflowRef.current = api.createWorkflow()
        .then(({ workflowId: newWorkflowId }) => {
          setWorkflowId(newWorkflowId);
          message.success('Workflow created automatically');
          return newWorkflowId;
        })
        .catch((error) => {
          console.error('Failed to auto-create workflow:', error);
          message.error('Failed to create workflow');
          throw error;
        })
        .finally(() => {
          hideLoading();
          creatingWorkflowRef.current = null;
        });
    }

    return creatingWorkflowRef.current;
  };

  const getDefaultPosition = () => {
    const offset = nodes.length * 30;
    return { x: 180 + offset, y: 120 + offset };
  };

  const loadBuiltinTasks = async (showSuccess = true) => {
    setBuiltinLoading(true);
    try {
      const tasks = await api.getBuiltinTasks();
      setBuiltinTasks(tasks);
      if (showSuccess) {
        message.success('Builtin tasks refreshed');
      }
    } catch (error) {
      console.error('Failed to load builtin tasks:', error);
      message.error('Failed to load builtin tasks');
    } finally {
      setBuiltinLoading(false);
    }
  };

  const loadWorkspaceTasks = async (dir?: string, showSuccess = false) => {
    setWorkspaceLoading(true);
    try {
      const normalizedDir = dir?.trim() || undefined;
      const result = await api.getWorkspaceTasks(normalizedDir);
      setWorkspaceDir(result.workspaceDir);
      setWorkspaceInput(result.workspaceDir);
      setWorkspaceTasks(result.tasks || []);
      setWorkspaceErrors(result.errors || []);

      if ((result.errors || []).length > 0) {
        message.warning('Some workspace task files could not be parsed');
      } else if (showSuccess) {
        message.success('Workspace tasks refreshed');
      }

      return result;
    } catch (error: any) {
      console.error('Failed to load workspace tasks:', error);
      message.error(error.response?.data?.error || 'Failed to load workspace tasks');
      throw error;
    } finally {
      setWorkspaceLoading(false);
    }
  };

  const loadWorkspaceWorkflows = async (dir?: string, showSuccess = false) => {
    setWorkflowLoading(true);
    try {
      const normalizedDir = dir?.trim() || undefined;
      const result = await api.getWorkspaceWorkflows(normalizedDir);
      setWorkspaceDir(result.workspaceDir);
      setWorkspaceInput(result.workspaceDir);
      setWorkspaceWorkflows(result.workflows || []);

      if ((result.errors || []).length > 0) {
        message.warning('Some workspace workflow files could not be parsed');
      } else if (showSuccess) {
        message.success('Workspace workflows refreshed');
      }

      return result;
    } catch (error: any) {
      console.error('Failed to load workspace workflows:', error);
      message.error(error.response?.data?.error || 'Failed to load workspace workflows');
      throw error;
    } finally {
      setWorkflowLoading(false);
    }
  };

  const handleChangeWorkspace = async (dir: string) => {
    try {
      const tasksResult = await loadWorkspaceTasks(dir);
      await loadWorkspaceWorkflows(tasksResult.workspaceDir);
      setCurrentWorkspaceWorkflowPath(null);
      message.success('Workspace loaded');
    } catch (error) {
      console.error('Failed to change workspace:', error);
    }
  };

  const createWorkspaceNode = (task: WorkspaceTaskMeta, position = getDefaultPosition()): WorkflowNode => ({
    id: `node-${Date.now()}`,
    type: 'taskNode',
    position,
    data: {
      category: 'workspace',
      nodeType: 'task',
      label: task.displayName || task.name,
      customCode: task.code,
      workspaceDir: task.workspaceDir,
      taskPath: task.relativePath,
      functionName: task.functionName,
      inputs: task.inputs.map((input) => ({
        name: input.name,
        dataType: input.dataType,
        source: 'user',
        value: '',
      })),
      outputs: task.outputs,
      resources: task.resources,
      configured: true,
    },
  });

  const getWorkspaceTaskKey = (task: WorkspaceTaskMeta) => `workspace:${task.workspaceDir}:${task.relativePath}:${task.functionName}`;
  const getBuiltinTaskKey = (task: BuiltinTaskMeta) => `builtin:${task.module}.${task.functionRef}`;

  const preserveInputConfig = (task: WorkspaceTaskMeta, node: WorkflowNode) => task.inputs.map((input) => {
    const existingInput = node.data.inputs.find((item) => item.name === input.name);

    if (existingInput) {
      return {
        ...existingInput,
        dataType: input.dataType,
      };
    }

    return {
      name: input.name,
      dataType: input.dataType,
      source: 'user' as const,
      value: '',
    };
  });

  const toggleTask = (taskKey: string) => {
    setExpandedTaskKey((current) => (current === taskKey ? null : taskKey));
  };

  const handleUpdateWorkspaceTask = (task: WorkspaceTaskMeta) => {
    setWorkspaceTaskEditor(task);
    setWorkspaceTaskEditorCode(task.code || '');
    setWorkspaceTaskEditorError(null);
  };

  const closeWorkspaceTaskEditor = () => {
    setWorkspaceTaskEditor(null);
    setWorkspaceTaskEditorCode('');
    setWorkspaceTaskEditorError(null);
  };

  const saveWorkspaceTaskEditor = async () => {
    if (!workspaceTaskEditor) {
      return;
    }

    if (!workspaceTaskEditorCode.trim()) {
      message.warning('Please enter task code');
      return;
    }

    setSavingWorkspaceTaskEditor(true);
    setWorkspaceTaskEditorError(null);

    try {
      const saved = await api.saveWorkspaceTask({
        workspaceDir: workspaceTaskEditor.workspaceDir,
        relativePath: workspaceTaskEditor.relativePath,
        code: workspaceTaskEditorCode,
        parse: true,
      });
      const updatedTask = saved.task as WorkspaceTaskMeta;
      const oldTaskKey = getWorkspaceTaskKey(workspaceTaskEditor);
      const updatedTaskKey = getWorkspaceTaskKey(updatedTask);

      updateCanvasNodesForWorkspaceTask(workspaceTaskEditor, updatedTask);

      if (expandedTaskKey === oldTaskKey) {
        setExpandedTaskKey(updatedTaskKey);
      }
      if (selectedWorkspaceTaskKey === oldTaskKey) {
        setSelectedWorkspaceTaskKey(updatedTaskKey);
      }

      await loadWorkspaceTasks(updatedTask.workspaceDir);
      message.success('Workspace task updated');
      closeWorkspaceTaskEditor();
    } catch (error: any) {
      console.error('Failed to update workspace task:', error);
      const errorMessage = error.response?.data?.error || 'Failed to update workspace task';
      setWorkspaceTaskEditorError(errorMessage);
      message.error(errorMessage);
    } finally {
      setSavingWorkspaceTaskEditor(false);
    }
  };

  const updateCanvasNodesForWorkspaceTask = (previousTask: WorkspaceTaskMeta, updatedTask: WorkspaceTaskMeta) => {
    setNodes(nodes.map((node) => {
      const isSameWorkspaceTask =
        node.data.category === 'workspace' &&
        node.data.workspaceDir === previousTask.workspaceDir &&
        node.data.taskPath === previousTask.relativePath &&
        node.data.functionName === previousTask.functionName;

      if (!isSameWorkspaceTask) {
        return node;
      }

      return {
        ...node,
        data: {
          ...node.data,
          label: updatedTask.displayName || updatedTask.name,
          customCode: updatedTask.code,
          workspaceDir: updatedTask.workspaceDir,
          taskPath: updatedTask.relativePath,
          functionName: updatedTask.functionName,
          inputs: preserveInputConfig(updatedTask, node),
          outputs: updatedTask.outputs,
          resources: updatedTask.resources,
          configured: true,
        },
      };
    }));
  };

  const exportWorkspaceTask = (task: WorkspaceTaskMeta) => {
    const fileName = task.relativePath.split('/').pop() || `${task.functionName}.py`;
    const blob = new Blob([task.code || ''], { type: 'text/x-python' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = fileName;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
    message.success('Workspace task exported');
  };

  const startImportWorkspaceTask = (task: WorkspaceTaskMeta) => {
    taskImportTargetRef.current = task;
    if (taskImportInputRef.current) {
      taskImportInputRef.current.value = '';
      taskImportInputRef.current.click();
    }
  };

  const handleImportWorkspaceTask = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';

    if (!file || !taskImportTargetRef.current) {
      return;
    }

    const targetTask = taskImportTargetRef.current;
    const targetTaskKey = getWorkspaceTaskKey(targetTask);
    setImportingTaskKey(targetTaskKey);

    try {
      const code = await file.text();
      const saved = await api.saveWorkspaceTask({
        workspaceDir: targetTask.workspaceDir,
        relativePath: targetTask.relativePath,
        code,
        parse: true,
      });
      const updatedTask = saved.task as WorkspaceTaskMeta;
      const updatedTaskKey = getWorkspaceTaskKey(updatedTask);

      updateCanvasNodesForWorkspaceTask(targetTask, updatedTask);

      if (expandedTaskKey === targetTaskKey) {
        setExpandedTaskKey(updatedTaskKey);
      }
      if (selectedWorkspaceTaskKey === targetTaskKey) {
        setSelectedWorkspaceTaskKey(updatedTaskKey);
      }

      await loadWorkspaceTasks(updatedTask.workspaceDir);
      message.success('Workspace task imported');
    } catch (error: any) {
      console.error('Failed to import workspace task:', error);
      message.error(error.response?.data?.error || 'Failed to import workspace task');
    } finally {
      setImportingTaskKey(null);
      taskImportTargetRef.current = null;
    }
  };

  const confirmDeleteWorkspaceTask = (task: WorkspaceTaskMeta) => {
    const taskKey = getWorkspaceTaskKey(task);

    Modal.confirm({
      title: 'Delete workspace task?',
      content: `This will delete ${task.relativePath} from the workspace.`,
      okText: 'Delete',
      okButtonProps: { danger: true },
      cancelText: 'Cancel',
      onOk: async () => {
        try {
          await api.deleteWorkspaceTask({
            workspaceDir: task.workspaceDir,
            relativePath: task.relativePath,
          });

          const deletedNodeIds = nodes
            .filter((node) =>
              node.data.category === 'workspace' &&
              node.data.workspaceDir === task.workspaceDir &&
              node.data.taskPath === task.relativePath
            )
            .map((node) => node.id);

          if (deletedNodeIds.length > 0) {
            setNodes(nodes.filter((node) => !deletedNodeIds.includes(node.id)));
            setEdges(edges.filter((edge) => !deletedNodeIds.includes(edge.source) && !deletedNodeIds.includes(edge.target)));
            selectNode(null);
          }

          if (expandedTaskKey === taskKey) {
            setExpandedTaskKey(null);
          }
          if (selectedWorkspaceTaskKey === taskKey) {
            setSelectedWorkspaceTaskKey(null);
          }

          await loadWorkspaceTasks(task.workspaceDir);
          message.success('Workspace task deleted');
        } catch (error: any) {
          console.error('Failed to delete workspace task:', error);
          message.error(error.response?.data?.error || 'Failed to delete workspace task');
        }
      },
    });
  };

  const commitWorkspaceTaskRename = async (task: WorkspaceTaskMeta) => {
    const taskKey = getWorkspaceTaskKey(task);
    const nextName = taskNameDraft.trim();
    setEditingTaskKey(null);

    if (!nextName || nextName === task.displayName || nextName === task.functionName) {
      setTaskNameDraft('');
      return;
    }

    try {
      const renamed = await api.renameWorkspaceTask({
        workspaceDir: task.workspaceDir,
        relativePath: task.relativePath,
        oldFunctionName: task.functionName,
        newName: nextName,
      });
      const updatedTask = renamed.task as WorkspaceTaskMeta;
      const updatedTaskKey = getWorkspaceTaskKey(updatedTask);

      updateCanvasNodesForWorkspaceTask(task, updatedTask);

      if (expandedTaskKey === taskKey) {
        setExpandedTaskKey(updatedTaskKey);
      }
      if (selectedWorkspaceTaskKey === taskKey) {
        setSelectedWorkspaceTaskKey(updatedTaskKey);
      }

      await loadWorkspaceTasks(updatedTask.workspaceDir);
      message.success(`Task renamed to ${updatedTask.displayName}`);
    } catch (error: any) {
      console.error('Failed to rename workspace task:', error);
      message.error(error.response?.data?.error || 'Failed to rename workspace task');
    } finally {
      setTaskNameDraft('');
    }
  };

  const handleTaskRenameKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter') {
      event.currentTarget.blur();
    }
    if (event.key === 'Escape') {
      event.stopPropagation();
      setTaskNameDraft('');
      setEditingTaskKey(null);
    }
    if (event.key === 'Delete' || event.key === 'Backspace') {
      event.stopPropagation();
    }
  };

  const confirmDeleteWorkspaceWorkflow = (item: WorkspaceWorkflowMeta) => {
    Modal.confirm({
      title: 'Delete workspace workflow?',
      content: `This will delete ${item.relativePath} from the workspace.`,
      okText: 'Delete',
      okButtonProps: { danger: true },
      cancelText: 'Cancel',
      onOk: async () => {
        try {
          const activeWorkspace = workspaceInput.trim() || workspaceDir;
          await api.deleteWorkspaceWorkflow({
            workspaceDir: activeWorkspace,
            relativePath: item.relativePath,
          });

          if (selectedWorkflowPath === item.relativePath) {
            setSelectedWorkflowPath(null);
          }
          if (currentWorkspaceWorkflowPath === item.relativePath) {
            setCurrentWorkspaceWorkflowPath(null);
          }

          await loadWorkspaceWorkflows(activeWorkspace);
          message.success('Workspace workflow deleted');
        } catch (error: any) {
          console.error('Failed to delete workspace workflow:', error);
          message.error(error.response?.data?.error || 'Failed to delete workspace workflow');
        }
      },
    });
  };

  const commitWorkspaceWorkflowRename = async (item: WorkspaceWorkflowMeta) => {
    const nextName = workflowNameDraft.trim();
    setEditingWorkflowPath(null);

    if (!nextName || nextName === item.name) {
      setWorkflowNameDraft('');
      return;
    }

    try {
      const activeWorkspace = workspaceInput.trim() || workspaceDir;
      await api.renameWorkspaceWorkflow({
        workspaceDir: activeWorkspace,
        relativePath: item.relativePath,
        name: nextName,
      });

      if (currentWorkspaceWorkflowPath === item.relativePath) {
        setWorkflowName(nextName);
        if (workflowId) {
          await api.saveWorkflow(workflowId, { name: nextName, nodes, edges });
        }
      }

      await loadWorkspaceWorkflows(activeWorkspace);
      message.success('Workflow renamed');
    } catch (error: any) {
      console.error('Failed to rename workspace workflow:', error);
      message.error(error.response?.data?.error || 'Failed to rename workspace workflow');
    } finally {
      setWorkflowNameDraft('');
    }
  };

  const handleWorkflowRenameKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter') {
      event.currentTarget.blur();
    }
    if (event.key === 'Escape') {
      event.stopPropagation();
      setWorkflowNameDraft('');
      setEditingWorkflowPath(null);
    }
    if (event.key === 'Delete' || event.key === 'Backspace') {
      event.stopPropagation();
    }
  };

  const handleSaveWorkspaceWorkflow = async () => {
    if (nodes.length === 0) {
      message.warning('Please add at least one task node before saving');
      return;
    }

    setSavingWorkflow(true);
    try {
      const activeWorkspace = workspaceInput.trim() || workspaceDir || (await loadWorkspaceTasks()).workspaceDir;
      let activeWorkflowId = workflowId;

      if (!activeWorkflowId) {
        const created = await api.createWorkflow(workflowName);
        activeWorkflowId = created.workflowId;
        setWorkflowId(created.workflowId);
      }

      const saved = await api.saveWorkspaceWorkflow({
        workspaceDir: activeWorkspace,
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

      setWorkspaceDir(saved.workspaceDir);
      setWorkspaceInput(saved.workspaceDir);
      setCurrentWorkspaceWorkflowPath(saved.relativePath);
      setNodes(saved.workflow.nodes);
      setEdges(saved.workflow.edges);
      await loadWorkspaceWorkflows(saved.workspaceDir);
      message.success(`Workflow saved to ${saved.relativePath}`);
    } catch (error: any) {
      console.error('Failed to save workspace workflow:', error);
      message.error(error.response?.data?.error || 'Failed to save workspace workflow');
    } finally {
      setSavingWorkflow(false);
    }
  };

  const handleLoadWorkspaceWorkflow = async (item: WorkspaceWorkflowMeta) => {
    setWorkflowLoading(true);
    try {
      const activeWorkspace = workspaceInput.trim() || workspaceDir;
      const loaded = await api.loadWorkspaceWorkflow({
        workspaceDir: activeWorkspace,
        relativePath: item.relativePath,
      });
      const created = await api.createWorkflow(loaded.workflow.name);

      await api.saveWorkflow(created.workflowId, {
        name: loaded.workflow.name,
        nodes: loaded.workflow.nodes,
        edges: loaded.workflow.edges,
      });

      setWorkflowId(created.workflowId);
      setWorkflowName(loaded.workflow.name);
      setNodes(loaded.workflow.nodes);
      setEdges(loaded.workflow.edges);
      selectNode(null);
      clearRunResults();
      setWorkspaceDir(loaded.workspaceDir);
      setWorkspaceInput(loaded.workspaceDir);
      setCurrentWorkspaceWorkflowPath(loaded.relativePath);
      await loadWorkspaceTasks(loaded.workspaceDir);
      const importedCount = loaded.importedTaskDefinitions?.imported.length || 0;
      const skippedCount = loaded.importedTaskDefinitions?.skipped.filter((entry) => entry.reason === 'exists').length || 0;
      const taskImportText = importedCount > 0 || skippedCount > 0
        ? ` Tasks added: ${importedCount}, skipped existing: ${skippedCount}.`
        : '';
      message.success(`Workflow loaded: ${loaded.workflow.name}.${taskImportText}`);
    } catch (error: any) {
      console.error('Failed to load workspace workflow:', error);
      message.error(error.response?.data?.error || 'Failed to load workspace workflow');
    } finally {
      setWorkflowLoading(false);
    }
  };

  const handleCreateWorkspaceTask = async () => {
    setCreatingTask(true);
    try {
      const requestedWorkspace = workspaceInput.trim() || workspaceDir;
      const activeWorkspace = requestedWorkspace
        ? (requestedWorkspace === workspaceDir ? workspaceDir : (await loadWorkspaceTasks(requestedWorkspace)).workspaceDir)
        : (await loadWorkspaceTasks()).workspaceDir;
      await ensureWorkflow();

      const stamp = Date.now();
      const functionName = `workspace_task_${stamp}`;
      const relativePath = `tasks/${functionName}.py`;
      const code = defaultWorkspaceTaskCode(functionName);
      const saved = await api.saveWorkspaceTask({
        workspaceDir: activeWorkspace,
        relativePath,
        code,
        parse: true,
      });

      await loadWorkspaceTasks(activeWorkspace);
      const newNode = createWorkspaceNode(saved.task);
      addNode(newNode);
      selectNode(newNode);
      message.success('Workspace task created');
    } catch (error: any) {
      console.error('Failed to create workspace task:', error);
      message.error(error.response?.data?.error || 'Failed to create workspace task');
    } finally {
      setCreatingTask(false);
    }
  };

  const onDragStartBuiltin = (event: React.DragEvent, task: BuiltinTaskMeta) => {
    event.dataTransfer.setData('application/reactflow', JSON.stringify({
      type: 'builtin',
      task,
    }));
    event.dataTransfer.effectAllowed = 'move';
  };

  const onDragStartWorkspace = (event: React.DragEvent, task: WorkspaceTaskMeta) => {
    draggingWorkspaceRef.current = true;
    event.dataTransfer.setData('application/reactflow', JSON.stringify({
      type: 'workspace',
      task,
    }));
    event.dataTransfer.effectAllowed = 'move';
  };

  const onDragEndWorkspace = () => {
    window.setTimeout(() => {
      draggingWorkspaceRef.current = false;
    }, 0);
  };

  const formatUpdatedAt = (value: string) => {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
  };

  const renderWorkspaceTaskSummary = (task: WorkspaceTaskMeta) => (
    <>
      <Space size={[4, 4]} wrap style={{ marginBottom: '6px' }}>
        <Tag color="purple">workspace</Tag>
      </Space>
      <Paragraph
        type="secondary"
        ellipsis={{ rows: 2, tooltip: task.description }}
        style={{ fontSize: '12px', marginBottom: '6px' }}
      >
        {task.description || 'No description provided.'}
      </Paragraph>
    </>
  );

  const renderBuiltinTaskSummary = (task: BuiltinTaskMeta) => (
    <>
      <Space size={[4, 4]} wrap style={{ marginBottom: '6px' }}>
        <Tag color="blue">builtin</Tag>
        <Tag>{task.module}</Tag>
      </Space>
      <Paragraph
        type="secondary"
        ellipsis={{ rows: 2, tooltip: task.description }}
        style={{ fontSize: '12px', marginBottom: '6px' }}
      >
        {task.description || 'No description provided.'}
      </Paragraph>
    </>
  );

  const renderTaskDetails = (task: BuiltinTaskMeta | WorkspaceTaskMeta) => (
    <>
      <Paragraph
        type="secondary"
        ellipsis={{ rows: 2, tooltip: task.description }}
        style={{ fontSize: '12px', marginBottom: '8px' }}
      >
        {task.description || 'No description provided.'}
      </Paragraph>
      <Space size={[4, 4]} wrap style={{ marginBottom: '8px' }}>
        <Tag color="geekblue">Inputs {task.inputs.length}</Tag>
        <Tag color="cyan">Outputs {task.outputs.length}</Tag>
      </Space>
      <div style={{ fontSize: '12px', color: '#666' }}>
        {'relativePath' in task && (
          <div style={{ marginBottom: '4px' }}>
            <Text type="secondary">File: </Text>
            <Tag>{task.relativePath}</Tag>
          </div>
        )}
        <div>
          <Text type="secondary">In: </Text>
          {task.inputs.map((input) => (
            <Tag key={input.name} style={{ marginBottom: '4px' }}>
              {input.name}:{input.dataType}
            </Tag>
          ))}
        </div>
        <div>
          <Text type="secondary">Out: </Text>
          {task.outputs.map((output) => (
            <Tag key={output.name} style={{ marginBottom: '4px' }}>
              {output.name}:{output.dataType}
            </Tag>
          ))}
        </div>
        {task.resources && (
          <Text type="secondary" style={{ fontSize: '11px' }}>
            CPU {task.resources.cpu}, GPU {task.resources.gpu}, Mem {task.resources.cpu_mem}MB, VRAM {task.resources.gpu_mem}MB
          </Text>
        )}
      </div>
    </>
  );

  const toggleButton = (
    <Tooltip title={collapsed ? 'Open sidebar' : 'Close sidebar'} placement="right">
      <Button
        type="text"
        icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
        onClick={() => setCollapsed(!collapsed)}
        aria-label={collapsed ? 'Open task sidebar' : 'Close task sidebar'}
        style={{
          width: '36px',
          height: '36px',
          borderRadius: '12px',
          background: '#f2f3f5',
          color: '#666',
          boxShadow: '0 1px 2px rgba(15, 23, 42, 0.08)',
        }}
      />
    </Tooltip>
  );

  return (
    <>
      <div
        style={{
          width: collapsed ? '56px' : '340px',
          flexShrink: 0,
          borderRight: '1px solid #f0f0f0',
          background: '#fafafa',
          overflowY: collapsed ? 'hidden' : 'auto',
          overflowX: 'hidden',
          transition: 'width 180ms ease',
        }}
      >
        <input
          ref={taskImportInputRef}
          type="file"
          accept=".py,text/x-python,text/plain"
          onChange={handleImportWorkspaceTask}
          style={{ display: 'none' }}
        />
      {collapsed ? (
        <div style={{ display: 'flex', justifyContent: 'center', paddingTop: '12px' }}>
          {toggleButton}
        </div>
      ) : (
        <div style={{ padding: '16px' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
            <h3 style={{ margin: 0 }}>Workspace</h3>
            {toggleButton}
          </div>

          <div style={{ marginBottom: '16px' }}>
            <Space size={6} style={{ width: '100%', marginBottom: '8px' }}>
              <FolderOpenOutlined style={{ color: '#666' }} />
              <Text strong>Directory</Text>
            </Space>
            <Input.Search
              size="small"
              value={workspaceInput}
              onChange={(event) => setWorkspaceInput(event.target.value)}
              onSearch={handleChangeWorkspace}
              enterButton="Change"
              loading={workspaceLoading || workflowLoading}
              placeholder="/home/user/project"
            />
            <Text type="secondary" style={{ display: 'block', fontSize: '11px', marginTop: '6px' }}>
              Scans tasks/**/*.py and workflows/**/*.json
            </Text>
          </div>

          {workspaceErrors.length > 0 && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: '12px' }}
              message={`${workspaceErrors.length} task file${workspaceErrors.length > 1 ? 's' : ''} failed to load`}
              description={workspaceErrors.slice(0, 2).map((item) => `${item.relativePath}: ${item.error}`).join('\n')}
            />
          )}

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
            <h3 style={{ margin: 0 }}>Workflows</h3>
            <Space size={4}>
              <Button
                type="text"
                size="small"
                icon={<ReloadOutlined />}
                onClick={() => loadWorkspaceWorkflows(workspaceInput || workspaceDir, true)}
                loading={workflowLoading}
              />
              <Button
                type="primary"
                size="small"
                icon={<SaveOutlined />}
                onClick={handleSaveWorkspaceWorkflow}
                loading={savingWorkflow}
                disabled={nodes.length === 0}
              >
                Save
              </Button>
            </Space>
          </div>
          {currentWorkspaceWorkflowPath && (
            <Text type="secondary" style={{ display: 'block', fontSize: '11px', marginBottom: '8px' }}>
              Current: {currentWorkspaceWorkflowPath}
            </Text>
          )}
          {workspaceWorkflows.length === 0 ? (
            <Empty description="No workspace workflows" image={Empty.PRESENTED_IMAGE_SIMPLE}>
              <Button size="small" icon={<SaveOutlined />} onClick={handleSaveWorkspaceWorkflow} loading={savingWorkflow} disabled={nodes.length === 0}>
                Save Current
              </Button>
            </Empty>
          ) : (
            <List
              dataSource={workspaceWorkflows}
              renderItem={(item) => (
                <Card
                  size="small"
                  style={{
                    marginBottom: '8px',
                    cursor: 'pointer',
                    borderColor: selectedWorkflowPath === item.relativePath || currentWorkspaceWorkflowPath === item.relativePath ? '#1677ff' : undefined,
                  }}
                  hoverable
                  tabIndex={0}
                  onClick={(event) => {
                    event.currentTarget.focus();
                    setSelectedWorkflowPath(item.relativePath);
                    handleLoadWorkspaceWorkflow(item);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === 'Delete' || event.key === 'Backspace') {
                      event.preventDefault();
                      event.stopPropagation();
                      confirmDeleteWorkspaceWorkflow(item);
                    }
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
                    <FileTextOutlined style={{ color: '#1677ff' }} />
                    {editingWorkflowPath === item.relativePath ? (
                      <Input
                        autoFocus
                        size="small"
                        value={workflowNameDraft}
                        onChange={(event) => setWorkflowNameDraft(event.target.value)}
                        onBlur={() => commitWorkspaceWorkflowRename(item)}
                        onKeyDown={(event) => handleWorkflowRenameKeyDown(event)}
                        onClick={(event) => event.stopPropagation()}
                        onMouseDown={(event) => event.stopPropagation()}
                        style={{ width: '210px' }}
                      />
                    ) : (
                      <strong
                        title="Click to rename workflow"
                        onClick={(event) => {
                          event.stopPropagation();
                          setSelectedWorkflowPath(item.relativePath);
                          setEditingWorkflowPath(item.relativePath);
                          setWorkflowNameDraft(item.name);
                        }}
                        onMouseDown={(event) => event.stopPropagation()}
                        style={{
                          cursor: 'text',
                          maxWidth: '220px',
                          whiteSpace: 'nowrap',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                        }}
                      >
                        {item.name}
                      </strong>
                    )}
                  </div>
                  <Space size={[4, 4]} wrap style={{ marginBottom: '6px' }}>
                    <Tag color="blue">workflow</Tag>
                    <Tag>{item.nodeCount} nodes</Tag>
                    <Tag>{item.edgeCount} edges</Tag>
                  </Space>
                  <Text type="secondary" style={{ display: 'block', fontSize: '11px' }}>
                    {item.relativePath}
                  </Text>
                  <Text type="secondary" style={{ display: 'block', fontSize: '11px' }}>
                    {formatUpdatedAt(item.updatedAt)}
                  </Text>
                </Card>
              )}
            />
          )}

          <Divider />

          <div style={{ marginBottom: '12px' }}>
            <h3 style={{ margin: 0 }}>Tasks</h3>
          </div>

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
            <h4 style={{ margin: 0, fontSize: '14px' }}>Workspace Tasks</h4>
            <Space size={4}>
              <Button
                type="text"
                size="small"
                icon={<ReloadOutlined />}
                onClick={() => loadWorkspaceTasks(workspaceInput || workspaceDir, true)}
                loading={workspaceLoading}
              />
              <Button
                type="primary"
                size="small"
                icon={<PlusOutlined />}
                onClick={handleCreateWorkspaceTask}
                loading={creatingTask}
              >
                New Task
              </Button>
            </Space>
          </div>
          <p style={{ fontSize: '12px', color: '#999', marginBottom: '12px' }}>
            Click to expand or drag to canvas
          </p>

          {workspaceTasks.length === 0 ? (
            <Empty description="No workspace tasks" image={Empty.PRESENTED_IMAGE_SIMPLE}>
              <Button size="small" icon={<PlusOutlined />} onClick={handleCreateWorkspaceTask} loading={creatingTask}>
                New Task
              </Button>
            </Empty>
          ) : (
            <List
              dataSource={workspaceTasks}
              renderItem={(task) => {
                const taskKey = getWorkspaceTaskKey(task);
                const expanded = expandedTaskKey === taskKey;

                return (
                  <Card
                    size="small"
                    style={{
                      marginBottom: '8px',
                      cursor: 'pointer',
                      borderColor: selectedWorkspaceTaskKey === taskKey ? '#1677ff' : undefined,
                    }}
                    hoverable
                    draggable
                    tabIndex={0}
                    onClick={(event) => {
                      event.currentTarget.focus();
                      setSelectedWorkspaceTaskKey(taskKey);
                      toggleTask(taskKey);
                    }}
                    onKeyDown={(event) => {
                      if (event.key === 'Delete' || event.key === 'Backspace') {
                        event.preventDefault();
                        event.stopPropagation();
                        confirmDeleteWorkspaceTask(task);
                      }
                    }}
                    onDragStart={(event) => onDragStartWorkspace(event, task)}
                    onDragEnd={onDragEndWorkspace}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px' }}>
                      <CodeOutlined style={{ color: '#722ed1' }} />
                      {editingTaskKey === taskKey ? (
                        <Input
                          autoFocus
                          size="small"
                          value={taskNameDraft}
                          onChange={(event) => setTaskNameDraft(event.target.value)}
                          onBlur={() => commitWorkspaceTaskRename(task)}
                          onKeyDown={handleTaskRenameKeyDown}
                          onClick={(event) => event.stopPropagation()}
                          onMouseDown={(event) => event.stopPropagation()}
                          style={{ width: '210px' }}
                        />
                      ) : (
                        <strong
                          title="Click to rename task"
                          onClick={(event) => {
                            event.stopPropagation();
                            setSelectedWorkspaceTaskKey(taskKey);
                            setEditingTaskKey(taskKey);
                            setTaskNameDraft(task.displayName);
                          }}
                          onMouseDown={(event) => event.stopPropagation()}
                          style={{
                            cursor: 'text',
                            maxWidth: '220px',
                            whiteSpace: 'nowrap',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                          }}
                        >
                          {task.displayName}
                        </strong>
                      )}
                    </div>
                    {renderWorkspaceTaskSummary(task)}
                    {expanded && (
                      <>
                        <Divider style={{ margin: '10px 0' }} />
                        {renderTaskDetails(task)}
                        <Space size={8} style={{ marginTop: '10px' }}>
                          <Button
                            size="small"
                            icon={<EditOutlined />}
                            onClick={(event) => {
                              event.stopPropagation();
                              handleUpdateWorkspaceTask(task);
                            }}
                          >
                            Update
                          </Button>
                          <Button
                            size="small"
                            icon={<UploadOutlined />}
                            loading={importingTaskKey === taskKey}
                            onClick={(event) => {
                              event.stopPropagation();
                              startImportWorkspaceTask(task);
                            }}
                          >
                            Import
                          </Button>
                          <Button
                            size="small"
                            icon={<DownloadOutlined />}
                            onClick={(event) => {
                              event.stopPropagation();
                              exportWorkspaceTask(task);
                            }}
                          >
                            Export
                          </Button>
                        </Space>
                      </>
                    )}
                  </Card>
                );
              }}
            />
          )}

          <Divider />

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
            <h4 style={{ margin: 0, fontSize: '14px' }}>Builtin Tasks</h4>
            <Button
              type="text"
              size="small"
              icon={<ReloadOutlined />}
              onClick={() => loadBuiltinTasks(true)}
              loading={builtinLoading}
            >
              Refresh
            </Button>
          </div>
          <p style={{ fontSize: '12px', color: '#999', marginBottom: '12px' }}>
            Click to expand or drag to canvas
          </p>
          {builtinTasks.length === 0 ? (
            <Empty description="No builtin tasks" image={Empty.PRESENTED_IMAGE_SIMPLE}>
              <Button type="primary" size="small" icon={<ReloadOutlined />} onClick={() => loadBuiltinTasks(true)}>
                Load Builtin Tasks
              </Button>
            </Empty>
          ) : (
            <List
              dataSource={builtinTasks}
              renderItem={(task) => {
                const taskKey = getBuiltinTaskKey(task);
                const expanded = expandedTaskKey === taskKey;

                return (
                  <Card
                    size="small"
                    style={{ marginBottom: '8px', cursor: 'pointer' }}
                    hoverable
                    draggable
                    onClick={() => {
                      setSelectedWorkspaceTaskKey(null);
                      toggleTask(taskKey);
                    }}
                    onDragStart={(event) => onDragStartBuiltin(event, task)}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px' }}>
                      <ThunderboltOutlined style={{ color: '#1890ff' }} />
                      <strong>{task.displayName}</strong>
                    </div>
                    {renderBuiltinTaskSummary(task)}
                    {expanded && (
                      <>
                        <Divider style={{ margin: '10px 0' }} />
                        {renderTaskDetails(task)}
                      </>
                    )}
                  </Card>
                );
              }}
            />
          )}
        </div>
      )}
      </div>

      <Drawer
        title={
          <Space>
            <EditOutlined />
            <span>Update Workspace Task</span>
          </Space>
        }
        placement="right"
        open={!!workspaceTaskEditor}
        onClose={closeWorkspaceTaskEditor}
        width={760}
        extra={
          <Space>
            <Button onClick={closeWorkspaceTaskEditor}>
              Cancel
            </Button>
            <Button type="primary" icon={<SaveOutlined />} loading={savingWorkspaceTaskEditor} onClick={saveWorkspaceTaskEditor}>
              Save
            </Button>
          </Space>
        }
      >
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          {workspaceTaskEditor && (
            <Alert
              type="info"
              showIcon
              message={workspaceTaskEditor.relativePath}
              description={workspaceTaskEditor.workspaceDir}
            />
          )}
          {workspaceTaskEditorError && (
            <Alert
              type="error"
              showIcon
              closable
              message="Update failed"
              description={<pre style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{workspaceTaskEditorError}</pre>}
              onClose={() => setWorkspaceTaskEditorError(null)}
            />
          )}
          <Input.TextArea
            value={workspaceTaskEditorCode}
            onChange={(event) => setWorkspaceTaskEditorCode(event.target.value)}
            autoSize={{ minRows: 22, maxRows: 34 }}
            style={{
              fontFamily: 'Consolas, Monaco, "Courier New", monospace',
              fontSize: '13px',
            }}
          />
        </Space>
      </Drawer>
    </>
  );
}
