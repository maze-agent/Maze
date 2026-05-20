import { ChangeEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Card, Divider, Drawer, Empty, Input, List, message, Modal, Radio, Space, Tag, Tooltip, Typography } from 'antd';
import {
  CodeOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  EyeOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PartitionOutlined,
  PlusOutlined,
  ReloadOutlined,
  SaveOutlined,
  ThunderboltOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import { api } from '@/api/client';
import { useWorkflowStore } from '@/stores/workflowStore';
import type { BuiltinTaskMeta, WorkflowNode, WorkspaceFileMeta, WorkspaceTaskMeta, WorkspaceWorkflowMeta } from '@/types/workflow';
import { loadLlmSettings } from '@/utils/llmSettings';

const { Text, Paragraph } = Typography;

const defaultWorkspaceTaskCode = (functionName: string) => `from maze import task

@task(resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0})
def ${functionName}(text: str = ""):
    """Process text and return a result."""
    return {"result": f"Processed: {text}"}
`;

function safeTaskFunctionName(name: string, fallback = 'workspace_task') {
  const value = name
    .trim()
    .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
    .replace(/[^a-zA-Z0-9_]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .toLowerCase();

  if (!value) return fallback;
  return /^[a-zA-Z_]/.test(value) ? value : `task_${value}`;
}

function joinWorkspacePath(base: string, name: string) {
  return [base, name].filter(Boolean).join('/');
}

function parentWorkspacePath(path: string) {
  const parts = path.split('/').filter(Boolean);
  parts.pop();
  return parts.join('/');
}

function formatFileSize(size?: number | null) {
  if (size === null || size === undefined) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
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
  const [filesLoading, setFilesLoading] = useState(false);
  const [savingWorkflow, setSavingWorkflow] = useState(false);
  const [creatingTask, setCreatingTask] = useState(false);
  const [newTaskOpen, setNewTaskOpen] = useState(false);
  const [newTaskMode, setNewTaskMode] = useState<'manual' | 'ai'>('manual');
  const [newTaskFunctionName, setNewTaskFunctionName] = useState('');
  const [newTaskRelativePath, setNewTaskRelativePath] = useState('');
  const [newTaskDescription, setNewTaskDescription] = useState('');
  const [newTaskGeneratedCode, setNewTaskGeneratedCode] = useState('');
  const [newTaskGeneratedNotes, setNewTaskGeneratedNotes] = useState('');
  const [newTaskError, setNewTaskError] = useState<string | null>(null);
  const [generatingTask, setGeneratingTask] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [workspaceInput, setWorkspaceInput] = useState(workspaceDir);
  const [workspaceErrors, setWorkspaceErrors] = useState<Array<{ relativePath: string; error: string }>>([]);
  const [workspaceFiles, setWorkspaceFiles] = useState<WorkspaceFileMeta[]>([]);
  const [workspaceFilesPath, setWorkspaceFilesPath] = useState('');
  const [previewFile, setPreviewFile] = useState<{ path: string; content: string } | null>(null);
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
  const [workspaceTaskScope, setWorkspaceTaskScope] = useState<'workflow' | 'all'>('all');
  const creatingWorkflowRef = useRef<Promise<string> | null>(null);
  const draggingWorkspaceRef = useRef(false);
  const taskImportInputRef = useRef<HTMLInputElement | null>(null);
  const taskImportTargetRef = useRef<WorkspaceTaskMeta | null>(null);
  const fileUploadInputRef = useRef<HTMLInputElement | null>(null);
  const fileUploadTargetPathRef = useRef('');

  useEffect(() => {
    loadWorkspaceTasks();
    loadWorkspaceWorkflows();
    loadWorkspaceFiles();
    if (builtinTasks.length === 0) {
      loadBuiltinTasks(false);
    }
  }, []);

  useEffect(() => {
    setWorkspaceTaskScope(currentWorkspaceWorkflowPath ? 'workflow' : 'all');
  }, [currentWorkspaceWorkflowPath]);

  const workflowWorkspaceTaskKeys = useMemo(() => new Set(
    nodes
      .filter((node) => node.data.category === 'workspace')
      .map((node) => `${node.data.taskPath || ''}:${node.data.functionName || ''}`),
  ), [nodes]);

  const displayedWorkspaceTasks = useMemo(() => {
    if (workspaceTaskScope !== 'workflow' || !currentWorkspaceWorkflowPath) {
      return workspaceTasks;
    }

    return workspaceTasks.filter((task) => workflowWorkspaceTaskKeys.has(`${task.relativePath}:${task.functionName}`));
  }, [currentWorkspaceWorkflowPath, workflowWorkspaceTaskKeys, workspaceTaskScope, workspaceTasks]);

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

  const loadWorkspaceFiles = async (dir?: string, filePath = workspaceFilesPath, showSuccess = false) => {
    setFilesLoading(true);
    try {
      const normalizedDir = dir?.trim() || undefined;
      const result = await api.getWorkspaceFiles({
        workspaceDir: normalizedDir,
        path: filePath,
      });
      setWorkspaceDir(result.workspaceDir);
      setWorkspaceInput(result.workspaceDir);
      setWorkspaceFiles(result.files || []);
      setWorkspaceFilesPath(result.path || '');

      if (showSuccess) {
        message.success('Workspace files refreshed');
      }

      return result;
    } catch (error: any) {
      console.error('Failed to load workspace files:', error);
      message.error(error.response?.data?.error || 'Failed to load workspace files');
      throw error;
    } finally {
      setFilesLoading(false);
    }
  };

  const handleChangeWorkspace = async (dir: string) => {
    try {
      const tasksResult = await loadWorkspaceTasks(dir);
      await loadWorkspaceWorkflows(tasksResult.workspaceDir);
      await loadWorkspaceFiles(tasksResult.workspaceDir, '');
      setCurrentWorkspaceWorkflowPath(null);
      message.success('Workspace loaded');
    } catch (error) {
      console.error('Failed to change workspace:', error);
    }
  };

  const startUploadWorkspaceFiles = (targetPath = workspaceFilesPath) => {
    fileUploadTargetPathRef.current = targetPath;
    if (fileUploadInputRef.current) {
      fileUploadInputRef.current.value = '';
      fileUploadInputRef.current.click();
    }
  };

  const handleUploadWorkspaceFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    event.target.value = '';

    if (files.length === 0 || !workspaceDir) {
      return;
    }

    const targetPath = fileUploadTargetPathRef.current || workspaceFilesPath;

    try {
      for (const file of files) {
        const contentBase64 = await fileToBase64(file);
        await api.uploadWorkspaceFile({
          workspaceDir,
          relativePath: joinWorkspacePath(targetPath, file.name),
          contentBase64,
        });
      }
      await loadWorkspaceFiles(workspaceDir, workspaceFilesPath);
      const targetLabel = targetPath ? ` to ${targetPath}` : '';
      message.success(files.length === 1 ? `File uploaded${targetLabel}` : `${files.length} files uploaded${targetLabel}`);
    } catch (error: any) {
      console.error('Failed to upload workspace file:', error);
      message.error(error.response?.data?.error || 'Failed to upload workspace file');
    } finally {
      fileUploadTargetPathRef.current = workspaceFilesPath;
    }
  };

  const createWorkspaceFolder = async () => {
    if (!workspaceDir) {
      return;
    }

    let folderName = '';
    Modal.confirm({
      title: 'New folder',
      content: (
        <Input
          autoFocus
          placeholder="Folder name"
          onChange={(event) => {
            folderName = event.target.value;
          }}
        />
      ),
      onOk: async () => {
        const cleanName = folderName.trim();
        if (!cleanName) {
          message.warning('Please enter a folder name');
          return Promise.reject();
        }
        await api.createWorkspaceFolder({
          workspaceDir,
          relativePath: joinWorkspacePath(workspaceFilesPath, cleanName),
        });
        await loadWorkspaceFiles(workspaceDir, workspaceFilesPath);
        message.success('Folder created');
      },
    });
  };

  const deleteWorkspaceFile = async (file: WorkspaceFileMeta) => {
    if (!workspaceDir) {
      return;
    }

    Modal.confirm({
      title: `Delete ${file.type}?`,
      content: file.relativePath,
      okText: 'Delete',
      okButtonProps: { danger: true },
      onOk: async () => {
        await api.deleteWorkspaceFile({
          workspaceDir,
          relativePath: file.relativePath,
        });
        await loadWorkspaceFiles(workspaceDir, workspaceFilesPath);
        message.success('Deleted');
      },
    });
  };

  const previewWorkspaceFile = async (file: WorkspaceFileMeta) => {
    if (!workspaceDir || file.type !== 'file') {
      return;
    }

    try {
      const result = await api.previewWorkspaceFile(workspaceDir, file.relativePath);
      setPreviewFile({ path: file.relativePath, content: result.content });
    } catch (error: any) {
      console.error('Failed to preview workspace file:', error);
      message.error(error.response?.data?.error || 'Failed to preview workspace file');
    }
  };

  const downloadWorkspaceFile = (file: WorkspaceFileMeta) => {
    if (!workspaceDir || file.type !== 'file') {
      return;
    }
    window.open(api.getWorkspaceFileDownloadUrl(workspaceDir, file.relativePath), '_blank');
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
      const reusedCount = loaded.importedTaskDefinitions?.skipped.filter((entry) => entry.reason === 'exists-same').length || 0;
      const remappedCount = loaded.importedTaskDefinitions?.remapped?.length || 0;
      const taskImportText = importedCount > 0 || reusedCount > 0 || remappedCount > 0
        ? ` Tasks added: ${importedCount}, reused: ${reusedCount}, remapped: ${remappedCount}.`
        : '';
      message.success(`Workflow loaded: ${loaded.workflow.name}.${taskImportText}`);
    } catch (error: any) {
      console.error('Failed to load workspace workflow:', error);
      message.error(error.response?.data?.error || 'Failed to load workspace workflow');
    } finally {
      setWorkflowLoading(false);
    }
  };

  const openNewWorkspaceTaskModal = () => {
    const stamp = Date.now();
    const functionName = `workspace_task_${stamp}`;
    setNewTaskMode('manual');
    setNewTaskFunctionName(functionName);
    setNewTaskRelativePath(`tasks/${functionName}.py`);
    setNewTaskDescription('');
    setNewTaskGeneratedCode('');
    setNewTaskGeneratedNotes('');
    setNewTaskError(null);
    setNewTaskOpen(true);
  };

  const closeNewWorkspaceTaskModal = () => {
    if (creatingTask || generatingTask) {
      return;
    }
    setNewTaskOpen(false);
    setNewTaskError(null);
  };

  const resolveActiveWorkspace = async () => {
    const requestedWorkspace = workspaceInput.trim() || workspaceDir;
    return requestedWorkspace
      ? (requestedWorkspace === workspaceDir ? workspaceDir : (await loadWorkspaceTasks(requestedWorkspace)).workspaceDir)
      : (await loadWorkspaceTasks()).workspaceDir;
  };

  const saveNewWorkspaceTask = async (code: string, relativePath: string) => {
    setCreatingTask(true);
    setNewTaskError(null);
    try {
      const activeWorkspace = await resolveActiveWorkspace();
      await ensureWorkflow();

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
      setNewTaskOpen(false);
    } catch (error: any) {
      console.error('Failed to create workspace task:', error);
      const errorMessage = error.response?.data?.error || 'Failed to create workspace task';
      setNewTaskError(errorMessage);
      message.error(errorMessage);
    } finally {
      setCreatingTask(false);
    }
  };

  const handleCreateManualWorkspaceTask = async () => {
    const functionName = safeTaskFunctionName(newTaskFunctionName, `workspace_task_${Date.now()}`);
    const relativePath = newTaskRelativePath.trim() || `tasks/${functionName}.py`;
    await saveNewWorkspaceTask(defaultWorkspaceTaskCode(functionName), relativePath);
  };

  const buildTaskGenerationContext = () => nodes.map((node) => {
    const workspaceTask = node.data.category === 'workspace'
      ? workspaceTasks.find((task) =>
          task.workspaceDir === node.data.workspaceDir &&
          task.relativePath === node.data.taskPath &&
          task.functionName === node.data.functionName)
      : null;
    const builtinTask = node.data.category === 'builtin'
      ? builtinTasks.find((task) => `${task.module}.${task.functionRef}` === node.data.taskRef)
      : null;

    return {
      nodeId: node.id,
      label: node.data.label,
      category: node.data.category,
      functionName: node.data.functionName || workspaceTask?.functionName || builtinTask?.functionRef,
      taskRef: node.data.taskRef,
      relativePath: node.data.taskPath || workspaceTask?.relativePath,
      description: workspaceTask?.description || builtinTask?.description || '',
      inputs: node.data.inputs,
      outputs: node.data.outputs,
      codePreview: workspaceTask?.code ? workspaceTask.code.slice(0, 1200) : undefined,
    };
  });

  const handleGenerateWorkspaceTask = async () => {
    const description = newTaskDescription.trim();
    if (!description) {
      message.warning('Please describe the task');
      return;
    }

    const settings = loadLlmSettings();
    if (!settings.model) {
      message.warning('Please configure an LLM model first');
      return;
    }

    setGeneratingTask(true);
    setNewTaskError(null);
    setNewTaskGeneratedNotes('');
    try {
      const generated = await api.generateWorkspaceTask({
        ...settings,
        description,
        taskName: newTaskFunctionName,
        relativePath: newTaskRelativePath,
        taskContext: buildTaskGenerationContext(),
      });
      setNewTaskFunctionName(generated.functionName);
      setNewTaskRelativePath(generated.relativePath);
      setNewTaskGeneratedCode(generated.code);
      setNewTaskGeneratedNotes(generated.notes || '');
      if ((generated.warnings || []).length > 0) {
        setNewTaskError(generated.warnings!.join('\n'));
      }
      message.success('Task code generated');
    } catch (error: any) {
      console.error('Failed to generate workspace task:', error);
      const errorMessage = error.response?.data?.error || 'Failed to generate workspace task';
      setNewTaskError(errorMessage);
      message.error(errorMessage);
    } finally {
      setGeneratingTask(false);
    }
  };

  const handleSaveGeneratedWorkspaceTask = async () => {
    if (!newTaskGeneratedCode.trim()) {
      message.warning('Generate or enter task code first');
      return;
    }
    await saveNewWorkspaceTask(newTaskGeneratedCode, newTaskRelativePath.trim() || `tasks/${safeTaskFunctionName(newTaskFunctionName)}.py`);
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

  const onDragStartReAct = (event: React.DragEvent) => {
    event.dataTransfer.setData('application/reactflow', JSON.stringify({
      type: 'agent-react',
      task: {
        label: 'ReAct Workflow',
      },
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
        <input
          ref={fileUploadInputRef}
          type="file"
          multiple
          onChange={handleUploadWorkspaceFile}
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

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
            <h3 style={{ margin: 0 }}>Files</h3>
            <Space size={4}>
              <Button
                type="text"
                size="small"
                icon={<ReloadOutlined />}
                onClick={() => loadWorkspaceFiles(workspaceInput || workspaceDir, workspaceFilesPath, true)}
                loading={filesLoading}
              />
              <Button
                size="small"
                icon={<FolderOpenOutlined />}
                onClick={createWorkspaceFolder}
              />
              <Button
                type="primary"
                size="small"
                icon={<UploadOutlined />}
                onClick={() => startUploadWorkspaceFiles(workspaceFilesPath)}
              >
                Upload
              </Button>
            </Space>
          </div>
          <Text type="secondary" style={{ display: 'block', fontSize: '11px', marginBottom: '8px' }}>
            workspace/files/{workspaceFilesPath || ''}
          </Text>
          {workspaceFilesPath && (
            <Button
              size="small"
              type="link"
              style={{ padding: 0, marginBottom: '6px' }}
              onClick={() => loadWorkspaceFiles(workspaceDir, parentWorkspacePath(workspaceFilesPath))}
            >
              .. parent
            </Button>
          )}
          <List
            size="small"
            loading={filesLoading}
            dataSource={workspaceFiles}
            locale={{ emptyText: <Empty description="No workspace files" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
            renderItem={(file) => (
              <List.Item
                style={{ padding: '6px 0' }}
                actions={[
                  file.type === 'file' ? (
                    <Tooltip key="preview" title="Preview">
                      <Button type="text" size="small" icon={<EyeOutlined />} onClick={() => previewWorkspaceFile(file)} />
                    </Tooltip>
                  ) : null,
                  file.type === 'file' ? (
                    <Tooltip key="download" title="Download">
                      <Button type="text" size="small" icon={<DownloadOutlined />} onClick={() => downloadWorkspaceFile(file)} />
                    </Tooltip>
                  ) : null,
                  file.type === 'directory' ? (
                    <Tooltip key="upload" title="Upload here">
                      <Button type="text" size="small" icon={<UploadOutlined />} onClick={() => startUploadWorkspaceFiles(file.relativePath)} />
                    </Tooltip>
                  ) : null,
                  <Tooltip key="delete" title="Delete">
                    <Button danger type="text" size="small" icon={<DeleteOutlined />} onClick={() => deleteWorkspaceFile(file)} />
                  </Tooltip>,
                ].filter(Boolean)}
              >
                <List.Item.Meta
                  avatar={file.type === 'directory' ? <FolderOpenOutlined /> : <FileTextOutlined />}
                  title={
                    file.type === 'directory' ? (
                      <Button
                        type="link"
                        size="small"
                        style={{ padding: 0 }}
                        onClick={() => loadWorkspaceFiles(workspaceDir, file.relativePath)}
                      >
                        {file.name}
                      </Button>
                    ) : (
                      <Text style={{ fontSize: '13px' }}>{file.name}</Text>
                    )
                  }
                  description={
                    <Text type="secondary" style={{ fontSize: '11px' }}>
                      {file.type === 'file' ? formatFileSize(file.size) : 'folder'}
                    </Text>
                  }
                />
              </List.Item>
            )}
          />

          <Divider />

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
            <h3 style={{ margin: 0 }}>
              {workspaceTaskScope === 'workflow' && currentWorkspaceWorkflowPath ? 'Workflow Tasks' : 'Workspace Tasks'}
            </h3>
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
                onClick={openNewWorkspaceTaskModal}
                loading={creatingTask}
              >
                New Task
              </Button>
            </Space>
          </div>
          {currentWorkspaceWorkflowPath && (
            <Radio.Group
              size="small"
              value={workspaceTaskScope}
              onChange={(event) => setWorkspaceTaskScope(event.target.value)}
              style={{ marginBottom: '8px' }}
            >
              <Radio.Button value="workflow">Current Workflow</Radio.Button>
              <Radio.Button value="all">All Workspace</Radio.Button>
            </Radio.Group>
          )}
          <p style={{ fontSize: '12px', color: '#999', marginBottom: '12px' }}>
            {workspaceTaskScope === 'workflow' && currentWorkspaceWorkflowPath
              ? 'Tasks referenced by the current workflow'
              : 'Click to expand or drag to canvas'}
          </p>

          {displayedWorkspaceTasks.length === 0 ? (
            <Empty
              description={workspaceTaskScope === 'workflow' && currentWorkspaceWorkflowPath
                ? 'No workspace tasks in current workflow'
                : 'No workspace tasks'}
              image={Empty.PRESENTED_IMAGE_SIMPLE}
            >
              <Button size="small" icon={<PlusOutlined />} onClick={openNewWorkspaceTaskModal} loading={creatingTask}>
                New Task
              </Button>
            </Empty>
          ) : (
            <List
              dataSource={displayedWorkspaceTasks}
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

          <div style={{ marginBottom: '16px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
              <h3 style={{ margin: 0 }}>Builtin Workflows</h3>
              <Tag color="purple" style={{ margin: 0 }}>template</Tag>
            </div>
            <p style={{ fontSize: '12px', color: '#999', marginBottom: '12px' }}>
              Drag workflow templates to canvas
            </p>
            <Card
              size="small"
              style={{ cursor: 'grab' }}
              hoverable
              draggable
              onDragStart={onDragStartReAct}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px' }}>
                <PartitionOutlined style={{ color: '#722ed1' }} />
                <strong>ReAct Workflow</strong>
              </div>
              <Space size={[4, 4]} wrap style={{ marginBottom: '6px' }}>
                <Tag color="purple">agent</Tag>
                <Tag>LLM + tools</Tag>
              </Space>
              <Text type="secondary" style={{ display: 'block', fontSize: '12px' }}>
                Drag to canvas, configure the prompt, then use the main Run button.
              </Text>
            </Card>
          </div>

          <Divider />

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
            <h3 style={{ margin: 0 }}>Builtin Tasks</h3>
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

      <Modal
        title="New Workspace Task"
        open={newTaskOpen}
        onCancel={closeNewWorkspaceTaskModal}
        width={760}
        footer={newTaskMode === 'manual' ? [
          <Button key="cancel" onClick={closeNewWorkspaceTaskModal} disabled={creatingTask}>
            Cancel
          </Button>,
          <Button key="create" type="primary" icon={<PlusOutlined />} loading={creatingTask} onClick={handleCreateManualWorkspaceTask}>
            Create
          </Button>,
        ] : [
          <Button key="cancel" onClick={closeNewWorkspaceTaskModal} disabled={creatingTask || generatingTask}>
            Cancel
          </Button>,
          <Button key="generate" icon={<ThunderboltOutlined />} loading={generatingTask} onClick={handleGenerateWorkspaceTask}>
            Generate
          </Button>,
          <Button key="save" type="primary" icon={<SaveOutlined />} loading={creatingTask} onClick={handleSaveGeneratedWorkspaceTask}>
            Save Task
          </Button>,
        ]}
      >
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Radio.Group
            value={newTaskMode}
            onChange={(event) => {
              setNewTaskMode(event.target.value);
              setNewTaskError(null);
            }}
            optionType="button"
            buttonStyle="solid"
            options={[
              { label: 'Manual', value: 'manual' },
              { label: 'Generate with AI', value: 'ai' },
            ]}
          />

          {newTaskError && (
            <Alert
              type={newTaskError.includes('Generated code') ? 'warning' : 'error'}
              showIcon
              closable
              message={newTaskError.includes('Generated code') ? 'Generation warning' : 'Task creation failed'}
              description={<pre style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{newTaskError}</pre>}
              onClose={() => setNewTaskError(null)}
            />
          )}

          <Space direction="vertical" size="small" style={{ width: '100%' }}>
            <Text strong>Function name</Text>
            <Input
              value={newTaskFunctionName}
              onChange={(event) => setNewTaskFunctionName(event.target.value)}
              placeholder="process_file"
            />
          </Space>

          <Space direction="vertical" size="small" style={{ width: '100%' }}>
            <Text strong>Task file</Text>
            <Input
              value={newTaskRelativePath}
              onChange={(event) => setNewTaskRelativePath(event.target.value)}
              placeholder="tasks/ai_generated/process_file.py"
            />
          </Space>

          {newTaskMode === 'manual' ? (
            <Alert
              type="info"
              showIcon
              message="A template task will be created and added to the canvas."
            />
          ) : (
            <>
              <Space direction="vertical" size="small" style={{ width: '100%' }}>
                <Text strong>Task description</Text>
                <Input.TextArea
                  value={newTaskDescription}
                  onChange={(event) => setNewTaskDescription(event.target.value)}
                  autoSize={{ minRows: 4, maxRows: 8 }}
                  placeholder="Read input.csv, compute missing values per column, and write reports/missing_values.json"
                />
              </Space>

              {newTaskGeneratedNotes && (
                <Alert type="info" showIcon message="Notes" description={newTaskGeneratedNotes} />
              )}

              <Alert
                type="info"
                showIcon
                message="Runtime file root"
                description='Path(".") is a task sandbox containing the staged workspace files and parent artifacts. It will not print as the physical workspace/files directory; use relative paths such as input.csv or reports/output.json.'
              />

              <Space direction="vertical" size="small" style={{ width: '100%' }}>
                <Text strong>Generated code</Text>
                <Input.TextArea
                  value={newTaskGeneratedCode}
                  onChange={(event) => setNewTaskGeneratedCode(event.target.value)}
                  autoSize={{ minRows: 14, maxRows: 24 }}
                  style={{
                    fontFamily: 'Consolas, Monaco, "Courier New", monospace',
                    fontSize: '13px',
                  }}
                />
              </Space>
            </>
          )}
        </Space>
      </Modal>

      <Modal
        title={previewFile?.path || 'Preview'}
        open={!!previewFile}
        onCancel={() => setPreviewFile(null)}
        footer={[
          <Button key="close" onClick={() => setPreviewFile(null)}>
            Close
          </Button>,
        ]}
        width={720}
      >
        <pre
          style={{
            maxHeight: '520px',
            overflow: 'auto',
            background: '#f5f5f5',
            padding: '12px',
            borderRadius: '4px',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {previewFile?.content}
        </pre>
      </Modal>
    </>
  );
}
