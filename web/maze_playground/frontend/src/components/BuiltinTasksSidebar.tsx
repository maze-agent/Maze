import { ChangeEvent, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Divider, Empty, Input, List, message, Modal, Radio, Select, Space, Tag, Tooltip, Typography } from 'antd';
import {
  DownloadOutlined,
  FolderOpenOutlined,
  AppstoreAddOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PlusOutlined,
  ReloadOutlined,
  SaveOutlined,
  SettingOutlined,
  ThunderboltOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import { api } from '@/api/client';
import { createLocalWorkflowId, useWorkflowStore, type WorkflowOperationToken } from '@/stores/workflowStore';
import type {
  ModelTestResponse,
  LocalModel,
  LocalWorkspaceFileMeta,
  WorkflowNode,
  SystemCatalogItem,
  WorkspaceFileMeta,
  WorkspaceTaskMeta,
  WorkspaceWorkflowMeta,
} from '@/types/workflow';
import { DEFAULT_LLM_SETTINGS, SILICONFLOW_MODELS, loadLlmSettings, saveLlmSettings } from '@/utils/llmSettings';

const { Text } = Typography;
const WORKFLOW_DRAFT_PATH = 'workflows/.drafts/current.workflow.json';

type WorkflowExample = {
  key: string;
  name: string;
  description: string;
  tags: string[];
  color: string;
  kind: 'distributed-smoke' | 'workspace-task' | 'resource-soak';
  taskSourceId?: string;
  taskRelativePath?: string;
  workflowName?: string;
  sleepSeconds?: number;
};

function normalizeResources(resources: any = {}) {
  return {
    cpu_num: Math.max(1, Number(resources.cpu_num ?? resources.cpu ?? 1) || 1),
    gpu_mem: Math.max(0, Number(resources.gpu_mem ?? 0) || 0),
    io_num: Math.max(0, Number(resources.io_num ?? 0) || 0),
  };
}

const WORKFLOW_EXAMPLES: WorkflowExample[] = [
  {
    key: 'distributed-smoke',
    name: 'Distributed GPU Smoke',
    description: 'Runs two GPU probe tasks so Run Detail can show head/worker placement.',
    tags: ['distributed', 'GPU', 'placement'],
    color: '#0958d9',
    kind: 'distributed-smoke',
    taskSourceId: 'distributed_gpu_probe.py',
    taskRelativePath: 'tasks/examples/distributed_gpu_probe.py',
    workflowName: 'Distributed GPU Smoke',
  },
  {
    key: 'resource-soak',
    name: 'CPU + GPU Resource Soak',
    description: 'Runs long CPU and GPU tasks so cluster resources visibly change for about one minute.',
    tags: ['resource', 'CPU', 'GPU'],
    color: '#7a5af8',
    kind: 'resource-soak',
    taskSourceId: 'distributed_gpu_probe.py',
    taskRelativePath: 'tasks/examples/distributed_gpu_probe.py',
    workflowName: 'CPU + GPU Resource Soak',
    sleepSeconds: 60,
  },
  {
    key: 'file-sandbox-demo',
    name: 'File Sandbox Demo',
    description: 'Reads a workspace file and writes a report artifact without LLM setup.',
    tags: ['sandbox', 'file', 'artifact'],
    color: '#389e0d',
    kind: 'workspace-task',
    taskSourceId: 'file_sandbox_demo.py',
    taskRelativePath: 'tasks/examples/file_sandbox_demo.py',
    workflowName: 'File Sandbox Demo',
  },
];

type SidebarClusterSummary = {
  totalNodes: number;
  onlineNodes: number;
  totalGpus: number;
  availableGpus: number | null;
  queuedTasks: number;
  runningTasks: number;
  cpuUsagePercent: number | null;
  memoryUsagePercent: number | null;
};

type WorkflowNavItem = {
  id: string;
  name: string;
  meta: string;
  status: string;
  selected: boolean;
  source: WorkspaceWorkflowMeta | null;
  note?: string;
  isCurrentDraft?: boolean;
};

const defaultWorkspaceTaskCode = (functionName: string) => `from maze import task

@task(resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 0})
def ${functionName}(text: str = ""):
    """Process text and return a result."""
    return {"result": f"Processed: {text}"}
`;

const resourceSoakCpuTaskCode = `from maze import task

@task(
    data_types={
        "probe_id": "int",
        "sleep_seconds": "int",
        "placement": "dict",
    },
    resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 0},
)
def resource_soak_cpu(probe_id: int = 0, sleep_seconds: int = 60):
    """Sleep for a short period and report CPU-side placement."""
    import os
    import platform
    import socket
    import time

    delay = max(0, min(int(sleep_seconds or 0), 300))
    placement = {
        "probe_id": int(probe_id or 0),
        "hostname": socket.gethostname(),
        "platform_node": platform.node(),
        "pid": os.getpid(),
        "sleep_seconds": delay,
    }

    try:
        import ray

        placement["ray_node_id"] = ray.get_runtime_context().get_node_id()
        placement["ray_node_ip"] = ray.util.get_node_ip_address()
    except Exception as exc:
        placement["ray_error"] = str(exc)

    if delay:
        time.sleep(delay)

    return {"placement": placement}
`;

const RESOURCE_SOAK_CPU_TASK_PATH = 'tasks/examples/resource_soak_cpu.py';

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

function normalizeLocalRelativePath(path: string) {
  return path
    .replace(/\\/g, '/')
    .split('/')
    .filter((part) => part && part !== '.' && part !== '..')
    .join('/');
}

function stripTopLevelDirectory(path: string) {
  const normalized = normalizeLocalRelativePath(path);
  const parts = normalized.split('/').filter(Boolean);
  if (parts.length <= 1) return normalized;
  return parts.slice(1).join('/');
}

function describeLocalSelection(paths: string[]) {
  const roots = Array.from(new Set(
    paths
      .map((item) => normalizeLocalRelativePath(item).split('/').filter(Boolean)[0])
      .filter(Boolean),
  ));
  if (roots.length === 0) return '';
  if (roots.length === 1) return roots[0];
  return `${roots.length} local folders`;
}

function canUseDirectoryPicker() {
  return typeof window !== 'undefined' && typeof (window as any).showDirectoryPicker === 'function';
}

function localWorkspaceIdForName(name: string) {
  const safeName = String(name || 'local-workspace')
    .trim()
    .replace(/[^a-zA-Z0-9_.:-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80) || 'local-workspace';
  return `${safeName}-${Date.now().toString(36)}`;
}

function formatFileSize(size?: number | null) {
  if (size === null || size === undefined) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function percentUsed(total: number, available: number | null) {
  if (!Number.isFinite(total) || !total || available === null || !Number.isFinite(available)) {
    return null;
  }
  return Math.max(0, Math.min(100, Math.round(((total - available) / total) * 100)));
}

function formatPercent(value: number | null | undefined) {
  return value === null || value === undefined ? '-' : `${value}%`;
}

function formatGiBFromBytes(value?: number | null) {
  if (!value) return undefined;
  return `${(value / 1024 / 1024 / 1024).toFixed(1)} GiB`;
}

function formatGiBFromMiB(value?: number | null) {
  if (!value) return undefined;
  return `${(value / 1024).toFixed(1)} GiB`;
}

function localModelLabel(model: LocalModel) {
  return [
    model.name,
    model.model_type,
    model.estimated_params_label,
    model.estimated_weight_memory || formatGiBFromMiB(model.estimated_gpu_mem_mb),
  ].filter(Boolean).join(' · ');
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

type UploadCandidate = {
  file: File;
  relativePath: string;
};

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

  return files.sort((a, b) => {
    if (a.type !== b.type) return a.type === 'directory' ? -1 : 1;
    return a.relativePath.localeCompare(b.relativePath);
  });
}

type BuiltinTasksSidebarProps = {
  onOpenRuns?: () => void;
  workspaceReady?: boolean;
};

export default function BuiltinTasksSidebar({
  onOpenRuns,
  workspaceReady = false,
}: BuiltinTasksSidebarProps = {}) {
  const {
    workspaceId,
    workspaceDir,
    setWorkspaceContext,
    setWorkspaceDir,
    workspaceTasks,
    setWorkspaceTasks,
    workspaceWorkflows,
    setWorkspaceWorkflows,
    currentWorkspaceWorkflowPath,
    setCurrentWorkspaceWorkflowPath,
    localWorkspaceId,
    localWorkspaceName,
    localWorkspaceFiles,
    localWorkspaceLastSyncedAt,
    setLocalWorkspace,
    setLocalWorkspaceFiles,
    workflowId,
    setWorkflowId,
    workflowName,
    setWorkflowName,
    workflowSaveState,
    workflowOperation,
    nodes,
    edges,
    setNodes,
    setEdges,
    setWorkflowSaveState,
    addNode,
    selectNode,
    clearRunResults,
    staticRuns,
    setStaticRuns,
    upsertStaticRun,
    setStaticRunEvents,
    openRunViewer,
    reset,
    isRunning,
    acquireWorkflowOperation,
    releaseWorkflowOperation,
  } = useWorkflowStore();
  const [workspaceLoading, setWorkspaceLoading] = useState(false);
  const [workflowLoading, setWorkflowLoading] = useState(false);
  const [initializedWorkspaceDir, setInitializedWorkspaceDir] = useState('');
  const [filesLoading, setFilesLoading] = useState(false);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogItems, setCatalogItems] = useState<Record<'workflows' | 'tasks', SystemCatalogItem[]>>({
    workflows: [],
    tasks: [],
  });
  const [importingCatalogKey, setImportingCatalogKey] = useState<string | null>(null);
  const [catalogImportType, setCatalogImportType] = useState<'workflows' | 'tasks' | null>(null);
  const [workspaceWorkflowModalOpen, setWorkspaceWorkflowModalOpen] = useState(false);
  const [importingExampleKey, setImportingExampleKey] = useState<string | null>(null);
  const [advancedSettingsOpen, setAdvancedSettingsOpen] = useState(false);
  const [llmSettingsDraft, setLlmSettingsDraft] = useState(DEFAULT_LLM_SETTINGS);
  const [testingLlm, setTestingLlm] = useState(false);
  const [modelDirInput, setModelDirInput] = useState('');
  const [localModels, setLocalModels] = useState<LocalModel[]>([]);
  const [selectedLocalModelId, setSelectedLocalModelId] = useState('');
  const [modelTestResult, setModelTestResult] = useState<ModelTestResponse | null>(null);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [testingModel, setTestingModel] = useState(false);
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
  const [workflowSearch, setWorkflowSearch] = useState('');
  const [clusterSummary, setClusterSummary] = useState<SidebarClusterSummary | null>(null);
  const [syncingLocalWorkspace, setSyncingLocalWorkspace] = useState(false);
  const [selectedWorkflowPath, setSelectedWorkflowPath] = useState<string | null>(null);
  const initializedWorkspaceDirRef = useRef('');
  const fileUploadInputRef = useRef<HTMLInputElement | null>(null);
  const folderUploadInputRef = useRef<HTMLInputElement | null>(null);
  const fileUploadTargetPathRef = useRef('');
  const normalizedWorkspaceDir = workspaceDir.trim();
  const workspaceInteractionReady = Boolean(
    workspaceReady
    && workspaceId
    && normalizedWorkspaceDir
    && initializedWorkspaceDir === normalizedWorkspaceDir,
  );
  const workflowOperationBusy = Boolean(workflowOperation);
  const workflowInteractionBlocked = workflowOperationBusy || isRunning;

  const requireWorkspaceInteraction = () => {
    if (workspaceInteractionReady) {
      return true;
    }
    message.info('Workspace is still loading');
    return false;
  };

  const beginWorkflowOperation = (label: string, notifyIfBusy = true) => {
    const token = acquireWorkflowOperation(label);
    if (!token && notifyIfBusy) {
      message.info(`Please wait for ${useWorkflowStore.getState().workflowOperation?.label || 'the current workflow operation'}`);
    }
    return token;
  };

  useEffect(() => {
    loadSystemCatalog();
  }, []);

  useEffect(() => {
    const activeWorkspaceDir = workspaceDir?.trim();
    if (!activeWorkspaceDir || initializedWorkspaceDirRef.current === activeWorkspaceDir) {
      return;
    }
    initializedWorkspaceDirRef.current = activeWorkspaceDir;

    let canceled = false;
    const initializeWorkspace = async () => {
      try {
        const tasksResult = await loadWorkspaceTasks(activeWorkspaceDir);
        if (canceled) return;
        await loadWorkspaceWorkflows(tasksResult.workspaceDir);
        if (canceled) return;
        await loadWorkspaceFiles(tasksResult.workspaceDir, '');
        if (canceled) return;
        await refreshWorkspaceRuns(tasksResult.workspaceDir);
        if (canceled) return;
        const restoredDraft = await restoreWorkspaceDraft(tasksResult.workspaceDir);
        if (!canceled && !restoredDraft) {
          setCurrentWorkspaceWorkflowPath(null);
        }
      } catch (error) {
        console.error('Failed to initialize workspace:', error);
      } finally {
        if (!canceled) {
          setInitializedWorkspaceDir(activeWorkspaceDir);
        }
      }
    };

    initializeWorkspace();
    return () => {
      canceled = true;
    };
  }, [workspaceDir]);

  useEffect(() => {
    if (!folderUploadInputRef.current) {
      return;
    }
    folderUploadInputRef.current.setAttribute('webkitdirectory', '');
    folderUploadInputRef.current.setAttribute('directory', '');
  }, []);

  useEffect(() => {
    let canceled = false;

    const loadClusterSummary = async () => {
      try {
        const [resources, queues] = await Promise.all([
          api.getClusterResources(),
          api.getClusterQueues(),
        ]);
        if (canceled) return;

        const clusterNodes = resources.cluster?.nodes || [];
        const onlineNodes = clusterNodes.filter((node) => node.registered && node.alive);
        const gpuTotals = clusterNodes.reduce((acc, node) => {
          const total = Number(node.resources?.gpu?.total_count ?? node.ray_resources?.GPU ?? 0);
          const available = node.resources?.gpu?.available_count;
          return {
            total: acc.total + total,
            available: available === undefined ? acc.available : acc.available + Number(available || 0),
            hasAvailable: acc.hasAvailable || available !== undefined,
          };
        }, { total: 0, available: 0, hasAvailable: false });
        const cpuTotals = clusterNodes.reduce((acc, node) => {
          const total = Number(node.resources?.cpu?.total ?? node.ray_resources?.CPU ?? 0);
          const available = node.resources?.cpu?.available;
          return {
            total: acc.total + total,
            available: available === undefined ? acc.available : acc.available + Number(available || 0),
            hasAvailable: acc.hasAvailable || available !== undefined,
          };
        }, { total: 0, available: 0, hasAvailable: false });
        const memoryTotals = clusterNodes.reduce((acc, node) => {
          const total = Number(node.resources?.cpu_mem?.total ?? node.ray_resources?.memory ?? 0);
          const available = node.resources?.cpu_mem?.available;
          return {
            total: acc.total + total,
            available: available === undefined ? acc.available : acc.available + Number(available || 0),
            hasAvailable: acc.hasAvailable || available !== undefined,
          };
        }, { total: 0, available: 0, hasAvailable: false });

        setClusterSummary({
          totalNodes: clusterNodes.length,
          onlineNodes: onlineNodes.length,
          totalGpus: gpuTotals.total,
          availableGpus: gpuTotals.hasAvailable ? gpuTotals.available : null,
          queuedTasks: Number(queues.queues?.counts?.total_queued || 0),
          runningTasks: Number(queues.queues?.counts?.running || 0),
          cpuUsagePercent: percentUsed(cpuTotals.total, cpuTotals.hasAvailable ? cpuTotals.available : null),
          memoryUsagePercent: percentUsed(memoryTotals.total, memoryTotals.hasAvailable ? memoryTotals.available : null),
        });
      } catch (error) {
        if (!canceled) {
          console.debug('Failed to refresh sidebar cluster summary:', error);
          setClusterSummary(null);
        }
      }
    };

    loadClusterSummary();
    const timer = window.setInterval(loadClusterSummary, 5000);
    return () => {
      canceled = true;
      window.clearInterval(timer);
    };
  }, []);

  const recentRuns = staticRuns.slice(0, 3);
  const inputFiles = workspaceFiles.filter((file) => file.type === 'file').slice(0, 5);
  const clusterStatus = clusterSummary
    ? clusterSummary.runningTasks > 0
      ? 'Running'
      : clusterSummary.onlineNodes > 0
        ? 'Ready'
        : 'Offline'
    : 'Unknown';
  const taskLibrary = useMemo(() => {
    const counts = workspaceTasks.reduce((acc, task) => {
      const label = `${task.displayName || task.name} ${task.description || ''} ${task.relativePath}`.toLowerCase();
      const resources = task.resources;
      if (resources?.gpu_mem || label.includes('gpu') || label.includes('cuda') || label.includes('llm') || label.includes('model') || label.includes('inference')) {
        acc.gpu += 1;
      } else if (label.includes('file') || label.includes('io') || label.includes('input') || label.includes('artifact')) {
        acc.io += 1;
      } else if (label.includes('util') || label.includes('health') || label.includes('smoke')) {
        acc.utility += 1;
      } else {
        acc.cpu += 1;
      }
      return acc;
    }, { cpu: 0, gpu: 0, io: 0, utility: 0 });

    return [
      { id: 'cpu', name: 'CPU Operators', count: counts.cpu },
      { id: 'gpu', name: 'GPU Operators', count: counts.gpu },
      { id: 'io', name: 'I/O Operators', count: counts.io },
    ];
  }, [workspaceTasks]);

  const handleNewWorkflow = () => {
    if (!requireWorkspaceInteraction()) {
      return;
    }
    if (isRunning) {
      message.warning('Workflow is running, please create a new workflow after it finishes');
      return;
    }
    const operationToken = beginWorkflowOperation('Creating new workflow');
    if (!operationToken) {
      return;
    }
    try {
      reset();
      setSelectedWorkflowPath(null);
      message.success('New workflow ready');
    } finally {
      releaseWorkflowOperation(operationToken);
    }
  };

  const getDefaultPosition = () => {
    const offset = nodes.length * 30;
    return { x: 180 + offset, y: 120 + offset };
  };

  const loadSystemCatalog = async (showSuccess = false) => {
    setCatalogLoading(true);
    try {
      const result = await api.getSystemCatalog();
      setCatalogItems({
        workflows: result.catalog.workflows || [],
        tasks: result.catalog.tasks || [],
      });
      if (showSuccess) {
        message.success('System catalog refreshed');
      }
      return result;
    } catch (error: any) {
      console.error('Failed to load system catalog:', error);
      message.error(error.response?.data?.error || 'Failed to load system catalog');
    } finally {
      setCatalogLoading(false);
    }
  };

  const importCatalogItem = async (item: SystemCatalogItem) => {
    if (!requireWorkspaceInteraction()) {
      return;
    }
    if (isRunning && item.type === 'workflows') {
      message.warning('Workflow is running, please import after it finishes');
      return;
    }
    const operationToken = beginWorkflowOperation(`Importing ${item.type === 'workflows' ? 'workflow' : 'task'}`);
    if (!operationToken) {
      return;
    }
    const key = `${item.type}:${item.id}`;
    setImportingCatalogKey(key);
    try {
      const type = item.type as 'workflows' | 'tasks';
      const result = await api.importSystemCatalogItem({
        workspaceId: workspaceId || undefined,
        workspaceDir: workspaceDir || undefined,
        type,
        sourceId: item.id,
      });
      setWorkspaceContext(result);
      setWorkspaceDir(result.workspaceDir);
      setWorkspaceInput(result.workspaceDir);

      if (type === 'workflows') {
        await loadWorkspaceWorkflows(result.workspaceDir);
      } else if (type === 'tasks') {
        await loadWorkspaceTasks(result.workspaceDir);
      }
      message.success(`Imported ${item.name}`);
      setCatalogImportType(null);
    } catch (error: any) {
      console.error('Failed to import system catalog item:', error);
      message.error(error.response?.data?.error || 'Failed to import system catalog item');
    } finally {
      setImportingCatalogKey(null);
      releaseWorkflowOperation(operationToken);
    }
  };

  const importSystemCatalogItem = async (
    type: 'workflows' | 'tasks',
    sourceId: string,
    targetPath?: string,
  ) => {
    const result = await api.importSystemCatalogItem({
      workspaceId: workspaceId || undefined,
      workspaceDir: workspaceDir || undefined,
      type,
      sourceId,
      targetPath,
    });
    setWorkspaceContext(result);
    setWorkspaceDir(result.workspaceDir);
    setWorkspaceInput(result.workspaceDir);
    return result;
  };

  const loadWorkspaceTasks = async (dir?: string, showSuccess = false) => {
    setWorkspaceLoading(true);
    try {
      const normalizedDir = dir?.trim() || undefined;
      const result = await api.getWorkspaceTasks(normalizedDir);
      setWorkspaceContext(result);
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
      setWorkspaceContext(result);
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
      setWorkspaceContext(result);
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

  const restoreWorkspaceDraft = async (dir: string, activeToken?: WorkflowOperationToken) => {
    const operationToken = activeToken || beginWorkflowOperation('Restoring workflow draft', false);
    if (!operationToken) {
      return false;
    }
    try {
      const loaded = await api.loadWorkspaceWorkflow({
        workspaceDir: dir,
        relativePath: WORKFLOW_DRAFT_PATH,
      });

      setWorkflowId(createLocalWorkflowId());
      setWorkflowName(loaded.workflow.name);
      setNodes(loaded.workflow.nodes);
      setEdges(loaded.workflow.edges);
      selectNode(null);
      clearRunResults();
      setWorkspaceContext(loaded);
      setWorkspaceDir(loaded.workspaceDir);
      setWorkspaceInput(loaded.workspaceDir);
      setCurrentWorkspaceWorkflowPath(null);
      setSelectedWorkflowPath(null);
      setWorkflowSaveState({
        status: loaded.workflow.nodes.length > 0 ? 'saved_draft' : 'empty',
        draftPath: WORKFLOW_DRAFT_PATH,
        savedAt: new Date().toISOString(),
        error: null,
      });
      return true;
    } catch (error: any) {
      if (error?.response?.status !== 404) {
        console.debug('No restorable workspace draft found:', error);
      }
      return false;
    } finally {
      if (!activeToken) {
        releaseWorkflowOperation(operationToken);
      }
    }
  };

  const refreshWorkspaceRuns = async (dir: string) => {
    try {
      const result = await api.getRuns({
        kind: 'static',
        limit: 20,
        detail: false,
      });
      const activeWorkspaceId = String(useWorkflowStore.getState().workspaceId || '').trim();
      const normalizeWorkspaceDir = (value: unknown) => (
        String(value || '').trim().replace(/\\/g, '/').replace(/\/+$/, '')
      );
      const activeWorkspaceDir = normalizeWorkspaceDir(dir);
      const workspaceRuns = (result.runs || []).filter((run) => {
        const metadata = run.metadata || {};
        const runWorkspaceId = String(metadata.workspace_id || '').trim();
        const runWorkspaceDir = normalizeWorkspaceDir(metadata.workspace_dir);

        if (runWorkspaceId && activeWorkspaceId && runWorkspaceId !== activeWorkspaceId) {
          return false;
        }
        if (runWorkspaceDir && activeWorkspaceDir && runWorkspaceDir !== activeWorkspaceDir) {
          return false;
        }
        return true;
      });
      setStaticRuns(workspaceRuns);
    } catch (error) {
      console.debug('Failed to refresh workspace runs:', error);
      setStaticRuns([]);
    }
  };

  const openRecentRun = async (runId: string) => {
    openRunViewer(runId);
    try {
      const [runResult, eventsResult] = await Promise.all([
        api.getRun(runId),
        api.getRunEvents(runId),
      ]);
      upsertStaticRun(runResult.run);
      setStaticRunEvents(runId, eventsResult.events || []);
    } catch (error: any) {
      console.error('Failed to load run details:', error);
      message.error(error.response?.data?.error || 'Failed to load run details');
    }
  };

  const handleChangeWorkspace = async (dir: string) => {
    if (isRunning) {
      message.warning('Workflow is running, please change workspace after it finishes');
      return;
    }
    const operationToken = beginWorkflowOperation('Changing workspace');
    if (!operationToken) {
      return;
    }
    try {
      const tasksResult = await loadWorkspaceTasks(dir);
      await loadWorkspaceWorkflows(tasksResult.workspaceDir);
      await loadWorkspaceFiles(tasksResult.workspaceDir, '');
      await refreshWorkspaceRuns(tasksResult.workspaceDir);
      const restoredDraft = await restoreWorkspaceDraft(tasksResult.workspaceDir, operationToken);
      if (!restoredDraft) {
        setCurrentWorkspaceWorkflowPath(null);
      }
      message.success(restoredDraft ? 'Workspace loaded with current draft' : 'Workspace loaded');
    } catch (error) {
      console.error('Failed to change workspace:', error);
    } finally {
      releaseWorkflowOperation(operationToken);
    }
  };

  const openAdvancedSettings = () => {
    setWorkspaceInput(workspaceDir);
    setLlmSettingsDraft(loadLlmSettings());
    setAdvancedSettingsOpen(true);
    void loadModelConfig();
  };

  const loadModelConfig = async (showSuccess = false) => {
    setModelsLoading(true);
    try {
      const result = await api.getModels();
      setModelDirInput(result.model_dir || '');
      setLocalModels(result.models || []);
      setSelectedLocalModelId((current) => (
        current && result.models?.some((model) => model.id === current)
          ? current
          : result.models?.[0]?.id || ''
      ));
      setModelTestResult(null);
      if (showSuccess) {
        message.success(`Scanned ${result.models?.length || 0} local models`);
      }
      return result;
    } catch (error: any) {
      console.error('Failed to load local models:', error);
      message.error(error.response?.data?.error || 'Failed to load local models');
      return null;
    } finally {
      setModelsLoading(false);
    }
  };

  const saveModelConfig = async () => {
    if (modelDirInput.trim()) {
      setModelsLoading(true);
      try {
        const result = await api.updateModelConfig(modelDirInput.trim());
        setModelDirInput(result.model_dir || modelDirInput.trim());
        setLocalModels(result.models || []);
        setSelectedLocalModelId((current) => (
          current && result.models?.some((model) => model.id === current)
            ? current
            : result.models?.[0]?.id || ''
        ));
        setModelTestResult(null);
        return result;
      } catch (error: any) {
        console.error('Failed to save model directory:', error);
        message.error(error.response?.data?.error || 'Failed to save model directory');
        return null;
      } finally {
        setModelsLoading(false);
      }
    }
    return null;
  };

  const saveAdvancedSettings = async () => {
    saveLlmSettings(llmSettingsDraft);
    if (modelDirInput.trim()) {
      const result = await saveModelConfig();
      if (!result) return;
    }
    setAdvancedSettingsOpen(false);
    message.success('Advanced settings saved');
  };

  const testLocalModel = async () => {
    if (!selectedLocalModelId) {
      message.warning('Select a local model first');
      return;
    }
    setTestingModel(true);
    try {
      const result = await api.testModel(selectedLocalModelId);
      setModelTestResult(result);
      message.success(result.ok ? 'Local model loaded and generated' : 'Local model test failed');
    } catch (error: any) {
      console.error('Failed to test local model:', error);
      message.error(error.response?.data?.error || 'Failed to test local model');
    } finally {
      setTestingModel(false);
    }
  };

  const testLlmConnection = async () => {
    const settings = {
      ...llmSettingsDraft,
      baseUrl: llmSettingsDraft.baseUrl.trim(),
      model: llmSettingsDraft.model.trim(),
    };

    if (!settings.model) {
      message.warning('Model is required');
      return;
    }

    setTestingLlm(true);
    try {
      await api.testLlmConnection(settings);
      message.success('LLM connection works');
    } catch (error: any) {
      console.error('Failed to test LLM connection:', error);
      message.error(error.response?.data?.error || 'Failed to test LLM connection');
    } finally {
      setTestingLlm(false);
    }
  };

  const startUploadWorkspaceFiles = (targetPath = workspaceFilesPath) => {
    fileUploadTargetPathRef.current = targetPath;
    if (fileUploadInputRef.current) {
      fileUploadInputRef.current.value = '';
      fileUploadInputRef.current.click();
    }
  };

  const refreshLocalWorkspaceManifest = async (showSuccess = false) => {
    if (!localWorkspaceId || !localWorkspaceName || !useWorkflowStore.getState().localWorkspaceHandle) {
      message.warning('Select a local file cache first');
      return null;
    }

    setSyncingLocalWorkspace(true);
    try {
      const handle = useWorkflowStore.getState().localWorkspaceHandle;
      let files: LocalWorkspaceFileMeta[] = [];
      if (handle?.kind === 'fileMap') {
        files = Array.from<[string, File]>(handle.filesByPath.entries()).map(([relativePath, file]) => ({
          name: file.name,
          relativePath,
          type: 'file' as const,
          size: file.size,
          updatedAt: new Date(file.lastModified).toISOString(),
        })).filter((file) => file.relativePath);
      } else {
        files = await collectLocalWorkspaceFiles(handle);
      }

      const version = Date.now().toString();
      setLocalWorkspaceFiles(files, version);
      if (showSuccess) {
        message.success(`Local file cache refreshed: ${files.filter((file) => file.type === 'file').length} files`);
      }
      return { files, version };
    } catch (error: any) {
      console.error('Failed to refresh local file cache:', error);
      message.error(error?.message || 'Failed to refresh local file cache');
      return null;
    } finally {
      setSyncingLocalWorkspace(false);
    }
  };

  const startOpenLocalWorkspace = async () => {
    if (canUseDirectoryPicker()) {
      try {
        const handle = await (window as any).showDirectoryPicker({ mode: 'read' });
        setSyncingLocalWorkspace(true);
        const files = await collectLocalWorkspaceFiles(handle);
        const workspaceId = localWorkspaceIdForName(handle.name);
        const version = Date.now().toString();
        setLocalWorkspace({
          id: workspaceId,
          name: handle.name,
          handle,
          files,
          version,
        });
        message.success(`Local file cache set: ${handle.name}`);
      } catch (error: any) {
        if (error?.name !== 'AbortError') {
          console.error('Failed to select local file cache:', error);
          message.error(error?.message || 'Failed to select local file cache');
        }
      } finally {
        setSyncingLocalWorkspace(false);
      }
      return;
    }

    if (folderUploadInputRef.current) {
      folderUploadInputRef.current.value = '';
      folderUploadInputRef.current.click();
    }
  };

  const uploadWorkspaceCandidates = async (
    candidates: UploadCandidate[],
    options: {
      targetPath?: string;
      preserveRelativePath?: boolean;
      stripRoot?: boolean;
      refreshPath?: string;
      loadingLabel?: string;
      successLabel?: (count: number) => string;
    } = {},
  ) => {
    const activeWorkspace = workspaceDir || (await loadWorkspaceFiles()).workspaceDir;
    const targetPath = options.targetPath ?? workspaceFilesPath;
    const refreshPath = options.refreshPath ?? workspaceFilesPath;

    if (candidates.length === 0) {
      return;
    }

    const hideLoading = message.loading(options.loadingLabel || 'Uploading files...', 0);
    try {
      let lastUploadResult: any = null;
      for (const candidate of candidates) {
        const localPath = options.preserveRelativePath
          ? (options.stripRoot ? stripTopLevelDirectory(candidate.relativePath) : normalizeLocalRelativePath(candidate.relativePath))
          : candidate.file.name;
        if (!localPath) {
          continue;
        }
        const contentBase64 = await fileToBase64(candidate.file);
        lastUploadResult = await api.uploadWorkspaceFile({
          workspaceId: workspaceId || undefined,
          workspaceDir: activeWorkspace,
          relativePath: joinWorkspacePath(targetPath, localPath),
          contentBase64,
        });
      }
      if (lastUploadResult) {
        setWorkspaceContext(lastUploadResult);
      }
      const refreshedFiles = await loadWorkspaceFiles(activeWorkspace, refreshPath);
      await loadWorkspaceTasks(activeWorkspace);
      const label = options.successLabel
        ? options.successLabel(candidates.length)
        : candidates.length === 1
          ? 'File uploaded'
          : `${candidates.length} files uploaded`;
      message.success(label);
      return refreshedFiles;
    } catch (error: any) {
      console.error('Failed to upload workspace files:', error);
      message.error(error.response?.data?.error || 'Failed to upload workspace files');
      return null;
    } finally {
      hideLoading();
    }
  };

  const handleUploadWorkspaceFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    event.target.value = '';

    if (files.length === 0) {
      return;
    }

    const targetPath = fileUploadTargetPathRef.current || workspaceFilesPath;
    await uploadWorkspaceCandidates(
      files.map((file) => ({ file, relativePath: file.name })),
      {
        targetPath,
        refreshPath: workspaceFilesPath,
        successLabel: (count) => {
          const targetLabel = targetPath ? ` to ${targetPath}` : '';
          return count === 1 ? `File uploaded${targetLabel}` : `${count} files uploaded${targetLabel}`;
        },
      },
    );
    fileUploadTargetPathRef.current = workspaceFilesPath;
  };

  const handleOpenLocalWorkspace = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    event.target.value = '';

    if (files.length === 0) {
      return;
    }

    setSyncingLocalWorkspace(true);
    try {
      const rawPaths = files.map((file) => (file as any).webkitRelativePath || file.name);
      const selectionLabel = describeLocalSelection(rawPaths) || 'Local File Cache';
      const filesByPath = new Map<string, File>();
      const manifestFiles: LocalWorkspaceFileMeta[] = [];

      files.forEach((file) => {
        const rawPath = (file as any).webkitRelativePath || file.name;
        const relativePath = stripTopLevelDirectory(rawPath) || file.name;
        const normalizedPath = normalizeLocalRelativePath(relativePath);
        if (!normalizedPath) {
          return;
        }
        filesByPath.set(normalizedPath, file);
        manifestFiles.push({
          name: file.name,
          relativePath: normalizedPath,
          type: 'file',
          size: file.size,
          updatedAt: new Date(file.lastModified).toISOString(),
        });
      });

      const workspaceId = localWorkspaceIdForName(selectionLabel);
      const version = Date.now().toString();
      setLocalWorkspace({
        id: workspaceId,
        name: selectionLabel,
        handle: {
          kind: 'fileMap',
          filesByPath,
        },
        files: manifestFiles,
        version,
      });
      message.success(`Local file cache set: ${selectionLabel}`);
    } finally {
      setSyncingLocalWorkspace(false);
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
      resources: normalizeResources(task.resources),
      configured: true,
    },
  });

  const createProbeNode = (
    task: WorkspaceTaskMeta,
    id: string,
    label: string,
    probeId: number,
    sleepSeconds: number,
    position: { x: number; y: number },
  ): WorkflowNode => {
    const node = createWorkspaceNode(task, position);
    return {
      ...node,
      id,
      data: {
        ...node.data,
        label,
        inputs: node.data.inputs.map((input) => ({
          ...input,
          value: input.name === 'probe_id'
            ? String(probeId)
            : input.name === 'sleep_seconds'
              ? String(sleepSeconds)
              : input.value,
        })),
      },
    };
  };

  const handleSaveWorkspaceWorkflow = async () => {
    if (!requireWorkspaceInteraction()) {
      return;
    }
    if (isRunning) {
      message.warning('Workflow is running, please save after it finishes');
      return;
    }
    if (nodes.length === 0) {
      message.warning('Please add at least one task node before saving');
      return;
    }

    const operationToken = beginWorkflowOperation('Saving workflow');
    if (!operationToken) {
      return;
    }
    setSavingWorkflow(true);
    try {
      const activeWorkspace = workspaceInput.trim() || workspaceDir || (await loadWorkspaceTasks()).workspaceDir;
      const activeWorkflowId = workflowId || createLocalWorkflowId();
      if (!workflowId) {
        setWorkflowId(activeWorkflowId);
      }

      const saved = await api.saveWorkspaceWorkflow({
        workspaceId: workspaceId || undefined,
        workspaceDir: activeWorkspace,
        relativePath: currentWorkspaceWorkflowPath,
        name: workflowName,
        workflowId: activeWorkflowId,
        nodes,
        edges,
      });

      setWorkspaceContext(saved);
      setWorkspaceDir(saved.workspaceDir);
      setWorkspaceInput(saved.workspaceDir);
      setCurrentWorkspaceWorkflowPath(saved.relativePath);
      setNodes(saved.workflow.nodes);
      setEdges(saved.workflow.edges);
      setWorkflowSaveState({
        status: 'saved_workflow',
        draftPath: 'workflows/.drafts/current.workflow.json',
        savedAt: new Date().toISOString(),
        error: null,
      });
      await loadWorkspaceWorkflows(saved.workspaceDir);
      message.success(`Workflow saved to ${saved.relativePath}`);
    } catch (error: any) {
      console.error('Failed to save workspace workflow:', error);
      message.error(error.response?.data?.error || 'Failed to save workspace workflow');
    } finally {
      setSavingWorkflow(false);
      releaseWorkflowOperation(operationToken);
    }
  };

  const handleLoadWorkspaceWorkflow = async (item: WorkspaceWorkflowMeta) => {
    if (!requireWorkspaceInteraction()) {
      return;
    }
    if (isRunning) {
      message.warning('Workflow is running, please load another workflow after it finishes');
      return;
    }
    const operationToken = beginWorkflowOperation('Loading workspace workflow');
    if (!operationToken) {
      return;
    }
    try {
      const activeWorkspace = workspaceInput.trim() || workspaceDir;
      const loaded = await api.loadWorkspaceWorkflow({
        workspaceId: workspaceId || undefined,
        workspaceDir: activeWorkspace,
        relativePath: item.relativePath,
      });

      setWorkflowId(createLocalWorkflowId());
      setWorkflowName(loaded.workflow.name);
      setNodes(loaded.workflow.nodes);
      setEdges(loaded.workflow.edges);
      selectNode(null);
      clearRunResults();
      setWorkspaceContext(loaded);
      setWorkspaceDir(loaded.workspaceDir);
      setWorkspaceInput(loaded.workspaceDir);
      setCurrentWorkspaceWorkflowPath(loaded.relativePath);
      setSelectedWorkflowPath(loaded.relativePath);
      setWorkflowSaveState({
        status: 'saved_workflow',
        draftPath: 'workflows/.drafts/current.workflow.json',
        savedAt: new Date().toISOString(),
        error: null,
      });
      const importedCount = loaded.importedTaskDefinitions?.imported.length || 0;
      const reusedCount = loaded.importedTaskDefinitions?.skipped.filter((entry) => entry.reason === 'exists-same').length || 0;
      const remappedCount = loaded.importedTaskDefinitions?.remapped?.length || 0;
      if (importedCount > 0 || remappedCount > 0) {
        await loadWorkspaceTasks(loaded.workspaceDir);
      }
      const taskImportText = importedCount > 0 || reusedCount > 0 || remappedCount > 0
        ? ` Tasks added: ${importedCount}, reused: ${reusedCount}, remapped: ${remappedCount}.`
        : '';
      setWorkspaceWorkflowModalOpen(false);
      message.success(`Workflow loaded: ${loaded.workflow.name}.${taskImportText}`);
    } catch (error: any) {
      console.error('Failed to load workspace workflow:', error);
      message.error(error.response?.data?.error || 'Failed to load workspace workflow');
    } finally {
      releaseWorkflowOperation(operationToken);
    }
  };

  const handleLoadSystemWorkflow = async (item: SystemCatalogItem) => {
    if (!requireWorkspaceInteraction()) {
      return;
    }
    if (isRunning) {
      message.warning('Workflow is running, please load another workflow after it finishes');
      return;
    }
    const operationToken = beginWorkflowOperation('Loading system workflow');
    if (!operationToken) {
      return;
    }
    const key = `${item.type}:${item.id}`;
    setImportingCatalogKey(key);
    try {
      const activeWorkspace = workspaceInput.trim() || workspaceDir;
      const loaded = await api.loadSystemWorkflow({
        workspaceId,
        workspaceDir: activeWorkspace,
        sourceId: item.id,
      });

      setWorkflowId(createLocalWorkflowId());
      setWorkflowName(loaded.workflow.name);
      setNodes(loaded.workflow.nodes);
      setEdges(loaded.workflow.edges);
      selectNode(null);
      clearRunResults();
      setWorkspaceContext(loaded);
      setWorkspaceDir(loaded.workspaceDir);
      setWorkspaceInput(loaded.workspaceDir);
      setCurrentWorkspaceWorkflowPath(null);
      setSelectedWorkflowPath(null);
      setWorkflowSaveState({
        status: 'unsaved_draft',
        draftPath: WORKFLOW_DRAFT_PATH,
        savedAt: new Date().toISOString(),
        error: null,
      });
      setWorkspaceWorkflowModalOpen(false);

      const importedCount = loaded.importedTaskDefinitions?.imported.length || 0;
      const reusedCount = loaded.importedTaskDefinitions?.skipped.filter((entry) => entry.reason === 'exists-same').length || 0;
      const remappedCount = loaded.importedTaskDefinitions?.remapped?.length || 0;
      if (importedCount > 0 || remappedCount > 0) {
        await loadWorkspaceTasks(loaded.workspaceDir).catch((refreshError) => {
          console.error('Failed to refresh workspace tasks after loading system workflow:', refreshError);
        });
      }
      const taskImportText = importedCount > 0 || reusedCount > 0 || remappedCount > 0
        ? ` Tasks added: ${importedCount}, reused: ${reusedCount}, remapped: ${remappedCount}.`
        : '';
      message.success(`System workflow loaded: ${loaded.workflow.name}.${taskImportText}`);
    } catch (error: any) {
      console.error('Failed to load system workflow:', error);
      message.error(error.response?.data?.error || 'Failed to load system workflow');
    } finally {
      setImportingCatalogKey(null);
      releaseWorkflowOperation(operationToken);
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
    if (!requireWorkspaceInteraction()) {
      return;
    }
    if (isRunning) {
      message.warning('Workflow is running, please create a task after it finishes');
      return;
    }
    const operationToken = beginWorkflowOperation('Creating workspace task');
    if (!operationToken) {
      return;
    }
    setCreatingTask(true);
    setNewTaskError(null);
    try {
      const activeWorkspace = await resolveActiveWorkspace();

      const saved = await api.saveWorkspaceTask({
        workspaceId: workspaceId || undefined,
        workspaceDir: activeWorkspace,
        relativePath,
        code,
        parse: true,
      });
      setWorkspaceContext(saved);

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
      releaseWorkflowOperation(operationToken);
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
    return {
      nodeId: node.id,
      label: node.data.label,
      category: node.data.category,
      functionName: node.data.functionName || workspaceTask?.functionName,
      taskRef: node.data.taskRef,
      relativePath: node.data.taskPath || workspaceTask?.relativePath,
      description: workspaceTask?.description || '',
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

  const addDistributedSmokeTemplateToCanvas = async (template: WorkflowExample) => {
    setWorkflowName(template.workflowName || template.name);
    const task = await importWorkspaceExampleTask(template);
    const position = getDefaultPosition();
    const baseId = Date.now();
    const smokeNodes = Array.from({ length: 2 }, (_, index) => createProbeNode(
      task,
      `node-${baseId}-${index + 1}`,
      `GPU Probe ${index + 1}`,
      index + 1,
      1,
      {
        x: position.x + (index % 2) * 260,
        y: position.y + Math.floor(index / 2) * 180,
      },
    ));

    smokeNodes.forEach((node) => addNode(node));
    selectNode(smokeNodes[0]);
    setCatalogImportType(null);
    message.success(`${template.name} added`);
  };

  const ensureResourceSoakCpuTask = async () => {
    const activeWorkspace = workspaceInput.trim() || workspaceDir || (await loadWorkspaceTasks()).workspaceDir;
    const saved = await api.saveWorkspaceTask({
      workspaceId: workspaceId || undefined,
      workspaceDir: activeWorkspace,
      relativePath: RESOURCE_SOAK_CPU_TASK_PATH,
      code: resourceSoakCpuTaskCode,
      parse: true,
    });
    setWorkspaceContext(saved);
    const tasksResult = await loadWorkspaceTasks(saved.workspaceDir || activeWorkspace);
    const task = (tasksResult.tasks || []).find((item) => item.relativePath === RESOURCE_SOAK_CPU_TASK_PATH)
      || saved.task;
    if (!task) {
      throw new Error(`Resource soak CPU task was not parsed: ${RESOURCE_SOAK_CPU_TASK_PATH}`);
    }
    return task;
  };

  const addResourceSoakTemplateToCanvas = async (template: WorkflowExample) => {
    setWorkflowName(template.workflowName || template.name);
    const sleepSeconds = template.sleepSeconds || 60;
    const cpuTask = await ensureResourceSoakCpuTask();
    const gpuTask = await importWorkspaceExampleTask(template);
    const position = getDefaultPosition();
    const baseId = Date.now();
    const soakNodes: WorkflowNode[] = [
      createProbeNode(
        cpuTask,
        `node-${baseId}-cpu-1`,
        'CPU Soak 1',
        1,
        sleepSeconds,
        { x: position.x, y: position.y },
      ),
      createProbeNode(
        cpuTask,
        `node-${baseId}-cpu-2`,
        'CPU Soak 2',
        2,
        sleepSeconds,
        { x: position.x, y: position.y + 170 },
      ),
      createProbeNode(
        gpuTask,
        `node-${baseId}-gpu-1`,
        'GPU Soak 1',
        101,
        sleepSeconds,
        { x: position.x + 280, y: position.y },
      ),
      createProbeNode(
        gpuTask,
        `node-${baseId}-gpu-2`,
        'GPU Soak 2',
        102,
        sleepSeconds,
        { x: position.x + 280, y: position.y + 170 },
      ),
    ];

    soakNodes.forEach((node) => addNode(node));
    selectNode(soakNodes[0]);
    setCatalogImportType(null);
    message.success(`${template.name} added (${sleepSeconds}s tasks)`);
  };

  const importWorkspaceExampleTask = async (template: WorkflowExample) => {
    if (!template.taskSourceId) {
      throw new Error('Example task source is missing');
    }

    const imported = await importSystemCatalogItem(
      'tasks',
      template.taskSourceId,
      template.taskRelativePath || template.taskSourceId,
    );
    const tasksResult = await loadWorkspaceTasks(imported.workspaceDir);
    const targetPath = imported.import?.targetPath || template.taskRelativePath || `tasks/${template.taskSourceId}`;
    const task = (tasksResult.tasks || []).find((item) => item.relativePath === targetPath)
      || (tasksResult.tasks || []).find((item) => item.relativePath.endsWith(template.taskSourceId || ''));
    if (!task) {
      throw new Error(`Imported task was not parsed: ${targetPath}`);
    }

    return task;
  };

  const addWorkspaceExampleTaskToCanvas = async (template: WorkflowExample) => {
    setWorkflowName(template.workflowName || template.name);
    const task = await importWorkspaceExampleTask(template);
    const newNode = createWorkspaceNode(task);
    addNode(newNode);
    selectNode(newNode);
    setCatalogImportType(null);
    message.success(`${template.name} added`);
  };

  const addWorkflowExampleToCanvas = async (template: WorkflowExample) => {
    if (!requireWorkspaceInteraction()) {
      return;
    }
    if (isRunning) {
      message.warning('Workflow is running, please update the canvas after it finishes');
      return;
    }
    const operationToken = beginWorkflowOperation('Adding workflow template');
    if (!operationToken) {
      return;
    }
    setImportingExampleKey(template.key);
    try {
      if (template.kind === 'distributed-smoke') {
        await addDistributedSmokeTemplateToCanvas(template);
      } else if (template.kind === 'resource-soak') {
        await addResourceSoakTemplateToCanvas(template);
      } else {
        await addWorkspaceExampleTaskToCanvas(template);
      }
    } catch (error: any) {
      console.error('Failed to add workflow example:', error);
      message.error(error.response?.data?.error || error.message || 'Failed to add workflow example');
    } finally {
      setImportingExampleKey(null);
      releaseWorkflowOperation(operationToken);
    }
  };

  const formatUpdatedAt = (value: string) => {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
  };

  const formatRunTime = (value?: number | null) => {
    if (!value) return '';
    const milliseconds = value > 1_000_000_000_000 ? value : value * 1000;
    const date = new Date(milliseconds);
    return Number.isNaN(date.getTime())
      ? ''
      : date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  };

  const statusClassName = (status?: string) => {
    const normalized = (status || '').toLowerCase();
    if (['succeeded', 'completed', 'validated', 'healthy', 'ready', 'selected'].includes(normalized)) return 'is-success';
    if (['running', 'processing'].includes(normalized)) return 'is-running';
    if (['failed', 'timed_out', 'interrupted'].includes(normalized)) return 'is-failed';
    if (['queued', 'draft', 'created'].includes(normalized) || normalized.includes('draft')) return 'is-warning';
    return 'is-idle';
  };

  const workflowItems = useMemo<WorkflowNavItem[]>(() => {
    const currentPathIsSavedWorkflow = Boolean(currentWorkspaceWorkflowPath)
      && workspaceWorkflows.some((item) => item.relativePath === currentWorkspaceWorkflowPath);
    const hasCurrentCanvas = nodes.length > 0 || (workflowName && workflowName !== 'Untitled Workflow');
    const currentDraftItem: WorkflowNavItem | null = hasCurrentCanvas && !currentPathIsSavedWorkflow
      ? {
        id: workflowId || 'current-draft',
        name: workflowName || 'Untitled Workflow',
        status: workflowSaveState === 'empty' ? 'draft' : workflowSaveState.replace('_', ' '),
        meta: `${nodes.length} tasks`,
        selected: true,
        source: null,
        note: workflowSaveState === 'saved_draft'
          ? `Current draft restored from ${WORKFLOW_DRAFT_PATH}`
          : 'Current draft',
        isCurrentDraft: true,
      }
      : null;
    const savedWorkflowItems = workspaceWorkflows.map((item) => ({
        id: item.relativePath,
        name: item.name,
        meta: `${item.nodeCount} tasks`,
        status: selectedWorkflowPath === item.relativePath || currentWorkspaceWorkflowPath === item.relativePath
          ? 'selected'
          : 'draft',
        selected: selectedWorkflowPath === item.relativePath || currentWorkspaceWorkflowPath === item.relativePath,
        source: item,
      }));

    return currentDraftItem ? [currentDraftItem, ...savedWorkflowItems] : savedWorkflowItems;
  }, [currentWorkspaceWorkflowPath, nodes.length, selectedWorkflowPath, workflowId, workflowName, workflowSaveState, workspaceWorkflows]);

  const visibleWorkflowItems = useMemo(() => {
    const query = workflowSearch.trim().toLowerCase();
    if (!query) return workflowItems.slice(0, 4);
    return workflowItems.filter((item) => (
      item.name.toLowerCase().includes(query)
      || item.meta.toLowerCase().includes(query)
      || item.status.toLowerCase().includes(query)
      || item.note?.toLowerCase().includes(query)
    )).slice(0, 4);
  }, [workflowItems, workflowSearch]);

  const visibleSystemWorkflowItems = useMemo(() => {
    const query = workflowSearch.trim().toLowerCase();
    return (catalogItems.workflows || [])
      .filter((item) => item.kind === 'file' && item.path.toLowerCase().endsWith('.json'))
      .filter((item) => {
        if (!query) return true;
        return [item.name, item.description, ...(item.tags || [])]
          .filter(Boolean)
          .some((value) => String(value).toLowerCase().includes(query));
      })
      .slice(0, 4);
  }, [catalogItems.workflows, workflowSearch]);

  const visibleInputs = inputFiles.length > 0
    ? inputFiles.map((file) => ({
      id: file.relativePath,
      name: file.name,
      size: formatFileSize(file.size),
      ready: true,
    }))
    : [];

  const visibleRuns = recentRuns.length > 0
    ? recentRuns.map((run) => ({
      id: run.run_id,
      label: run.workflow_name || `Run ${run.run_id.slice(0, 8)}`,
      startedAt: formatRunTime(run.created_time),
      status: run.status,
    }))
    : [];
  const importableCatalogItems = useMemo(() => {
    const workflows = (catalogItems.workflows || []).filter((item) => item.kind === 'file');
    const tasks = (catalogItems.tasks || []).filter((item) => (
      item.kind === 'file'
      && item.name.endsWith('.py')
      && !item.name.startsWith('__')
      && !item.path.includes('__pycache__')
    ));
    return { workflows, tasks };
  }, [catalogItems]);

  const renderWorkflowExamples = () => {
    return (
      <div>
        <Space align="center" size={6} style={{ marginBottom: 8 }}>
          <Text strong>Examples</Text>
          <Tag color="purple" style={{ margin: 0 }}>canvas</Tag>
        </Space>
        <List
          size="small"
          dataSource={WORKFLOW_EXAMPLES}
          renderItem={(template) => (
            <List.Item
              className="workspace-file-row"
              actions={[
                <Button
                  key="add"
                  type="primary"
                  size="small"
                  icon={<PlusOutlined />}
                  loading={importingExampleKey === template.key}
                  disabled={!workspaceInteractionReady || workflowInteractionBlocked}
                  onClick={() => addWorkflowExampleToCanvas(template)}
                >
                  Add
                </Button>,
              ]}
            >
              <List.Item.Meta
                avatar={<AppstoreAddOutlined style={{ color: template.color }} />}
                title={<Text strong>{template.name}</Text>}
                description={
                  <Space direction="vertical" size={4}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {template.description}
                    </Text>
                    <Space size={4} wrap>
                      {template.tags.map((tag) => (
                        <Tag key={tag} style={{ margin: 0 }}>{tag}</Tag>
                      ))}
                    </Space>
                  </Space>
                }
              />
            </List.Item>
          )}
        />
      </div>
    );
  };

  const renderCatalogList = (type: 'workflows' | 'tasks') => {
    const items = importableCatalogItems[type] || [];

    return (
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {type === 'workflows' && renderWorkflowExamples()}
        <div>
          <Space align="center" size={6} style={{ marginBottom: 8 }}>
            <Text strong>System Catalog</Text>
            <Tag color="geekblue" style={{ margin: 0 }}>system</Tag>
            <Tag color="green" style={{ margin: 0 }}>import to workspace</Tag>
          </Space>
          {items.length === 0 ? (
            <Empty description={`No ${type} in library`} image={Empty.PRESENTED_IMAGE_SIMPLE} />
          ) : (
            <List
              size="small"
              dataSource={items}
              renderItem={(item) => {
                const key = `${item.type}:${item.id}`;
                return (
                  <List.Item
                    className="workspace-file-row"
                    actions={[
                      <Tooltip key="import" title="Import">
                        <Button
                          type="text"
                          size="small"
                          icon={<DownloadOutlined />}
                          loading={importingCatalogKey === key}
                          disabled={!workspaceInteractionReady || workflowOperationBusy || (isRunning && item.type === 'workflows')}
                          onClick={() => importCatalogItem(item)}
                        />
                      </Tooltip>,
                    ]}
                  >
                    <List.Item.Meta
                      avatar={<AppstoreAddOutlined style={{ color: '#1677ff' }} />}
                      title={
                        <Text style={{ fontSize: '13px', maxWidth: '190px' }} ellipsis={{ tooltip: item.id }}>
                          {item.name}
                        </Text>
                      }
                      description={
                        <Space direction="vertical" size={4}>
                          {item.description && (
                            <Text type="secondary" style={{ fontSize: 12 }}>
                              {item.description}
                            </Text>
                          )}
                          <Space size={4} wrap>
                            <Tag color="geekblue" style={{ margin: 0 }}>system</Tag>
                            <Tag style={{ margin: 0 }}>{item.kind}</Tag>
                            {(item.tags || []).map((tag) => (
                              <Tag key={tag} style={{ margin: 0 }}>{tag}</Tag>
                            ))}
                            {item.updatedAt && (
                              <Text type="secondary" style={{ fontSize: '11px' }}>
                                {formatUpdatedAt(item.updatedAt)}
                              </Text>
                            )}
                          </Space>
                        </Space>
                      }
                    />
                  </List.Item>
                );
              }}
            />
          )}
        </div>
      </Space>
    );
  };

  const renderWorkspaceWorkflowList = () => {
    const systemWorkflows = importableCatalogItems.workflows || [];

    return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <div>
        <Space align="center" size={6} style={{ marginBottom: 8 }}>
          <Text strong>System Workflows</Text>
          <Tag color="geekblue" style={{ margin: 0 }}>template</Tag>
        </Space>
        {systemWorkflows.length === 0 ? (
          <Empty description="No system workflows" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <List
            size="small"
            loading={catalogLoading}
            dataSource={systemWorkflows}
            renderItem={(item) => {
              const key = `${item.type}:${item.id}`;

              return (
                <List.Item
                  className="workspace-file-row"
                  actions={[
                    <Button
                      key="open"
                      type="primary"
                      size="small"
                      loading={importingCatalogKey === key}
                      disabled={!workspaceInteractionReady || workflowLoading || workflowInteractionBlocked}
                      onClick={() => void handleLoadSystemWorkflow(item)}
                    >
                      Open
                    </Button>,
                  ]}
                >
                  <List.Item.Meta
                    avatar={<AppstoreAddOutlined style={{ color: '#722ed1' }} />}
                    title={<Text strong>{item.name.replace(/\.json$/i, '')}</Text>}
                    description={(
                      <Space direction="vertical" size={4}>
                        {item.description && (
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            {item.description}
                          </Text>
                        )}
                        <Space size={6} wrap>
                          <Tag color="geekblue" style={{ margin: 0 }}>system</Tag>
                          {(item.tags || []).map((tag) => (
                            <Tag key={tag} style={{ margin: 0 }}>{tag}</Tag>
                          ))}
                          {item.updatedAt && (
                            <Text type="secondary" style={{ fontSize: 12 }}>
                              {formatUpdatedAt(item.updatedAt)}
                            </Text>
                          )}
                        </Space>
                      </Space>
                    )}
                  />
                </List.Item>
              );
            }}
          />
        )}
      </div>
      <Divider style={{ margin: '4px 0' }} />
      <div>
        <Space align="center" size={6} style={{ marginBottom: 8 }}>
          <Text strong>Workspace Workflows</Text>
          <Tag color="blue" style={{ margin: 0 }}>saved</Tag>
        </Space>
        {workspaceWorkflows.length === 0 ? (
          <Empty description="No saved workflows in this workspace" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <List
            size="small"
            loading={workflowLoading}
            dataSource={workspaceWorkflows}
            renderItem={(item) => (
              <List.Item
                className="workspace-file-row"
                actions={[
                  <Button
                    key="open"
                    type="primary"
                    size="small"
                    disabled={!workspaceInteractionReady || workflowLoading || workflowInteractionBlocked}
                    onClick={() => void handleLoadWorkspaceWorkflow(item)}
                  >
                    Open
                  </Button>,
                ]}
              >
                <List.Item.Meta
                  avatar={<AppstoreAddOutlined style={{ color: '#1677ff' }} />}
                  title={<Text strong>{item.name}</Text>}
                  description={(
                    <Space size={6} wrap>
                      <Tag style={{ margin: 0 }}>{item.nodeCount} tasks</Tag>
                      <Tag style={{ margin: 0 }}>{item.edgeCount} edges</Tag>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {item.relativePath}
                      </Text>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {formatUpdatedAt(item.updatedAt)}
                      </Text>
                    </Space>
                  )}
                />
              </List.Item>
            )}
          />
        )}
      </div>
    </Space>
    );
  };

  const openCatalogImport = (type: 'workflows' | 'tasks') => {
    setCatalogImportType(type);
    void loadSystemCatalog(false);
  };

  const refreshWorkspaceWorkflowList = (showSuccess = true) => {
    if (!requireWorkspaceInteraction()) {
      return;
    }
    void loadWorkspaceWorkflows(workspaceInput || workspaceDir, showSuccess).catch(() => undefined);
  };

  const openWorkflowLibrary = () => {
    if (!requireWorkspaceInteraction()) {
      return;
    }
    setWorkspaceWorkflowModalOpen(true);
    void loadWorkspaceWorkflows(workspaceInput || workspaceDir).catch(() => undefined);
    void loadSystemCatalog(false);
  };

  const refreshWorkflowLibrary = () => {
    if (!requireWorkspaceInteraction()) {
      return;
    }
    void loadWorkspaceWorkflows(workspaceInput || workspaceDir, true).catch(() => undefined);
    void loadSystemCatalog(true);
  };

  const catalogImportTitle = catalogImportType
    ? `Import ${catalogImportType === 'tasks' ? 'Tasks' : 'Workflows'}`
    : 'Import';

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
        data-sidebar-collapsed={collapsed ? 'true' : 'false'}
        style={{
          width: collapsed ? '56px' : '100%',
          height: '100%',
          boxSizing: 'border-box',
          flexShrink: 0,
          borderRight: 0,
          background: '#fff',
          overflowY: collapsed ? 'hidden' : 'auto',
          overflowX: 'hidden',
          transition: 'width 180ms ease',
        }}
      >
        <input
          ref={fileUploadInputRef}
          type="file"
          multiple
          onChange={handleUploadWorkspaceFile}
          style={{ display: 'none' }}
        />
        <input
          ref={folderUploadInputRef}
          type="file"
          multiple
          onChange={handleOpenLocalWorkspace}
          style={{ display: 'none' }}
        />
      {collapsed ? (
        <div style={{ display: 'flex', justifyContent: 'center', paddingTop: '12px' }}>
          {toggleButton}
        </div>
      ) : (
        <div style={{ padding: '12px' }}>
          <div className="workbench-sidebar-primary-actions">
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={handleNewWorkflow}
              disabled={!workspaceInteractionReady || workflowInteractionBlocked}
              className="workbench-sidebar-new-workflow"
            >
              New Workflow
            </Button>
            <Tooltip title="Advanced workspace and LLM settings">
              <Button
                size="small"
                className="workspace-sidebar-icon-button"
                icon={<SettingOutlined />}
                aria-label="Advanced workspace settings"
                onClick={openAdvancedSettings}
                disabled={workflowInteractionBlocked}
              />
            </Tooltip>
            {toggleButton}
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

          <Input.Search
            allowClear
            size="small"
            placeholder="Search workflows..."
            aria-label="Search system and workspace workflows"
            value={workflowSearch}
            onChange={(event) => setWorkflowSearch(event.target.value)}
            className="workbench-nav-search"
          />

          <section className="workbench-nav-section">
            <div className="workbench-nav-section-header">
              <span>SYSTEM WORKFLOWS</span>
              <div className="workbench-nav-section-actions">
                <Tooltip title="Refresh system workflows">
                  <Button
                    type="text"
                    size="small"
                    className="workspace-sidebar-icon-button"
                    icon={<ReloadOutlined />}
                    aria-label="Refresh system workflows"
                    onClick={() => loadSystemCatalog(true)}
                    loading={catalogLoading}
                  />
                </Tooltip>
              </div>
            </div>
            <div className="workbench-nav-list">
              {visibleSystemWorkflowItems.map((item) => {
                const key = `${item.type}:${item.id}`;
                const loading = importingCatalogKey === key;
                const status = loading
                  ? 'loading'
                  : !workspaceInteractionReady
                    ? 'waiting'
                    : workflowLoading || workflowInteractionBlocked
                      ? 'busy'
                      : 'template';
                return (
                  <button
                    key={key}
                    type="button"
                    className="workbench-nav-row"
                    disabled={!workspaceInteractionReady || workflowLoading || workflowInteractionBlocked}
                    aria-busy={loading}
                    title={!workspaceInteractionReady ? 'Workspace is still loading' : undefined}
                    onClick={() => void handleLoadSystemWorkflow(item)}
                  >
                    <AppstoreAddOutlined style={{ color: '#1677ff', flexShrink: 0 }} />
                    <span className="workbench-nav-row-main">
                      <span className="workbench-nav-row-label">{item.name.replace(/\.json$/i, '')}</span>
                      {item.description && (
                        <span className="workbench-nav-row-note">{item.description}</span>
                      )}
                    </span>
                    <span className="workbench-nav-row-meta">{status}</span>
                  </button>
                );
              })}
              {visibleSystemWorkflowItems.length === 0 && (
                <div className="workbench-nav-empty-row">
                  {catalogLoading
                    ? 'Loading workflows...'
                    : workflowSearch.trim()
                      ? 'No matching system workflows'
                      : 'No system workflows'}
                </div>
              )}
            </div>
          </section>

          <section className="workbench-nav-section">
            <div className="workbench-nav-section-header">
              <span>WORKSPACE WORKFLOWS</span>
              <div className="workbench-nav-section-actions">
                <Tooltip title="Refresh workspace workflows">
                  <Button
                    type="text"
                    size="small"
                    className="workspace-sidebar-icon-button"
                    icon={<ReloadOutlined />}
                    aria-label="Refresh workspace workflows"
                    onClick={() => refreshWorkspaceWorkflowList(true)}
                    loading={workflowLoading}
                    disabled={!workspaceInteractionReady}
                  />
                </Tooltip>
                <Tooltip title="Save current workflow">
                  <Button
                    type="text"
                    size="small"
                    className="workspace-sidebar-icon-button"
                    icon={<SaveOutlined />}
                    aria-label="Save current workflow"
                    onClick={handleSaveWorkspaceWorkflow}
                    loading={savingWorkflow}
                    disabled={!workspaceInteractionReady || nodes.length === 0 || workflowInteractionBlocked}
                  />
                </Tooltip>
              </div>
            </div>
            <div className="workbench-nav-list">
              {visibleWorkflowItems.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className={`workbench-nav-row${item.selected ? ' is-selected' : ''}${item.isCurrentDraft ? ' is-current-draft' : ''}`}
                  disabled={!item.source || !workspaceInteractionReady || workflowLoading || workflowInteractionBlocked}
                  onClick={() => {
                    if (item.source) {
                      void handleLoadWorkspaceWorkflow(item.source);
                    }
                  }}
                >
	                  <span className={`workbench-status-dot ${statusClassName(item.status)}`} />
	                  <span className="workbench-nav-row-main">
	                    <span className="workbench-nav-row-label">{item.name}</span>
	                    {item.note && (
	                      <span className="workbench-nav-row-note">{item.note}</span>
	                    )}
	                  </span>
	                  <span className="workbench-nav-row-meta">{item.meta}</span>
	                </button>
	              ))}
              {visibleWorkflowItems.length === 0 && (
	                <div className="workbench-nav-empty-row">
	                  {workflowSearch.trim() ? 'No matching workspace workflows' : 'No workspace workflows'}
	                </div>
	              )}
	            </div>
            <button
              type="button"
              className="workbench-nav-link"
              disabled={!workspaceInteractionReady || workflowLoading}
              onClick={openWorkflowLibrary}
            >
              Open workflow library
            </button>
          </section>

          <section className="workbench-nav-section">
            <div className="workbench-nav-section-header">
              <span>TASK LIBRARY</span>
              <div className="workbench-nav-section-actions">
                <Tooltip title="Refresh tasks">
                  <Button
                    type="text"
                    size="small"
                    className="workspace-sidebar-icon-button"
                    icon={<ReloadOutlined />}
                    aria-label="Refresh tasks"
                    onClick={() => loadWorkspaceTasks(workspaceInput || workspaceDir, true)}
                    loading={workspaceLoading}
                  />
                </Tooltip>
                <Tooltip title="New workspace task">
                  <Button
                    type="text"
                    size="small"
                    className="workspace-sidebar-icon-button"
                    icon={<PlusOutlined />}
                    aria-label="New workspace task"
                    onClick={openNewWorkspaceTaskModal}
                    loading={creatingTask}
                  />
                </Tooltip>
              </div>
            </div>
            <div className="workbench-nav-list">
              {taskLibrary.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className={`workbench-nav-row workbench-task-kind-${item.id}`}
                  onClick={() => openCatalogImport('tasks')}
                >
                  <span className="workbench-task-kind-icon" />
                  <span className="workbench-nav-row-label">{item.name}</span>
                  <span className="workbench-nav-row-meta">{item.count}</span>
                </button>
              ))}
            </div>
          </section>

          <section className="workbench-nav-section">
            <div className="workbench-nav-section-header">
              <span>INPUTS</span>
              <div className="workbench-nav-section-actions">
                <Tooltip title="Refresh inputs">
                  <Button
                    type="text"
                    size="small"
                    className="workspace-sidebar-icon-button"
                    icon={<ReloadOutlined />}
                    aria-label="Refresh inputs"
                    onClick={() => loadWorkspaceFiles(workspaceInput || workspaceDir, workspaceFilesPath, true)}
                    loading={filesLoading}
                  />
                </Tooltip>
                <Tooltip title="Upload input files">
                  <Button
                    type="text"
                    size="small"
                    className="workspace-sidebar-icon-button"
                    icon={<UploadOutlined />}
                    aria-label="Upload input files"
                    onClick={() => startUploadWorkspaceFiles(workspaceFilesPath)}
                  />
                </Tooltip>
              </div>
            </div>
            <div className="workbench-nav-list">
              {visibleInputs.map((input) => (
                <div key={input.id} className="workbench-nav-row">
	                  <span className={`workbench-status-dot ${input.ready ? 'is-success' : 'is-idle'}`} />
	                  <span className="workbench-nav-row-label">{input.name}</span>
	                  <span className="workbench-nav-row-meta">{input.size}</span>
	                </div>
	              ))}
	              {visibleInputs.length === 0 && (
	                <div className="workbench-nav-empty-row">No inputs yet</div>
	              )}
	            </div>
	            <button type="button" className="workbench-nav-link" onClick={() => startUploadWorkspaceFiles(workspaceFilesPath)}>
	              Upload inputs
	            </button>
	          </section>

	          <section className="workbench-nav-section">
	            <div className="workbench-nav-section-header">
	              <span>RECENT RUNS</span>
	              <span className="workbench-nav-count">{staticRuns.length}</span>
	            </div>
	            <div className="workbench-nav-list">
	              {visibleRuns.map((run) => (
	                <button
	                  key={run.id}
	                  type="button"
	                  className="workbench-nav-row"
	                  onClick={() => openRecentRun(run.id)}
	                >
	                  <span className={`workbench-status-dot ${statusClassName(run.status)}`} />
	                  <span className="workbench-nav-row-label">{run.label}</span>
	                  <span className={`workbench-run-status ${statusClassName(run.status)}`}>{run.status}</span>
	                </button>
	              ))}
	              {visibleRuns.length === 0 && (
	                <div className="workbench-nav-empty-row">No runs yet</div>
	              )}
	            </div>
            {staticRuns.length > 0 && (
              <button type="button" className="workbench-nav-link" onClick={onOpenRuns}>
                View all runs
              </button>
            )}
	          </section>

          <section className="workbench-nav-section">
            <div className="workbench-nav-section-header">
              <span>CLUSTER SUMMARY</span>
              <span className={`workbench-run-status ${statusClassName(clusterStatus)}`}>{clusterStatus}</span>
	            </div>
	            <div className="workbench-cluster-summary">
	              <span>Status</span>
	              <strong>{clusterSummary ? `${clusterSummary.onlineNodes}/${clusterSummary.totalNodes} online` : '-'}</strong>
	              <span>Queued</span>
	              <strong>{clusterSummary ? clusterSummary.queuedTasks : '-'}</strong>
	              <span>Running</span>
	              <strong>{clusterSummary ? clusterSummary.runningTasks : '-'}</strong>
	              <span>GPUs</span>
	              <strong>
	                {clusterSummary
	                  ? clusterSummary.availableGpus === null
	                    ? `${clusterSummary.totalGpus} total`
	                    : `${clusterSummary.availableGpus}/${clusterSummary.totalGpus} available`
	                  : '-'}
	              </strong>
	              <span>CPU Usage</span>
	              <strong>{formatPercent(clusterSummary?.cpuUsagePercent)}</strong>
	              <span>Memory Usage</span>
	              <strong>{formatPercent(clusterSummary?.memoryUsagePercent)}</strong>
	            </div>
	          </section>

        </div>
      )}
      </div>

      <Modal
        title={
          <Space>
            <SettingOutlined />
            <span>Advanced Setting</span>
          </Space>
        }
        open={advancedSettingsOpen}
        onCancel={() => setAdvancedSettingsOpen(false)}
        width={680}
        footer={[
          <Button key="test" loading={testingLlm} onClick={testLlmConnection}>
            Test Connection
          </Button>,
          <Button key="cancel" onClick={() => setAdvancedSettingsOpen(false)}>
            Cancel
          </Button>,
          <Button key="save" type="primary" onClick={saveAdvancedSettings}>
            Save
          </Button>,
        ]}
      >
        <Space direction="vertical" size="large" style={{ width: '100%' }}>
          <div>
            <Space size={6} style={{ marginBottom: 8 }}>
              <FolderOpenOutlined style={{ color: '#666' }} />
              <Text strong>Runtime Root</Text>
            </Space>
            <Input.Search
              value={workspaceInput}
              onChange={(event) => setWorkspaceInput(event.target.value)}
              onSearch={handleChangeWorkspace}
              enterButton="Change"
              loading={workspaceLoading || workflowLoading}
              disabled={workflowInteractionBlocked}
              placeholder="/root/data/Maze/workspaces/default"
            />
            <Text type="secondary" style={{ display: 'block', fontSize: 12, marginTop: 6 }}>
              Service-side workspace for workflows, tasks, files, and run artifacts.
            </Text>
          </div>

          <div>
            <Space size={6} style={{ marginBottom: 8 }}>
              <FolderOpenOutlined style={{ color: '#1677ff' }} />
              <Text strong>Local File Cache</Text>
            </Space>
            <Space size={8} wrap>
              <Tooltip title="Optional browser-local file cache. Missing files are copied into Workspace Files before runs.">
                <Button
                  icon={<FolderOpenOutlined />}
                  onClick={startOpenLocalWorkspace}
                  loading={syncingLocalWorkspace}
                >
                  Select Folder
                </Button>
              </Tooltip>
              {localWorkspaceName && (
                <Button
                  onClick={() => refreshLocalWorkspaceManifest(true)}
                  loading={syncingLocalWorkspace}
                >
                  Refresh Cache
                </Button>
              )}
            </Space>
            {localWorkspaceName ? (
              <div className="local-workspace-summary" style={{ marginTop: 10 }}>
                <Space size={[4, 4]} wrap>
                  <Tag color="green" style={{ margin: 0 }}>cache</Tag>
                  <Text strong ellipsis={{ tooltip: localWorkspaceName }} style={{ maxWidth: 280 }}>
                    {localWorkspaceName}
                  </Text>
                  <Tag style={{ margin: 0 }}>
                    {localWorkspaceFiles.filter((file) => file.type === 'file').length} files
                  </Tag>
                  {localWorkspaceLastSyncedAt && (
                    <Text type="secondary" style={{ fontSize: 11 }}>
                      {formatUpdatedAt(localWorkspaceLastSyncedAt)}
                    </Text>
                  )}
                </Space>
              </div>
            ) : (
              <Text type="secondary" style={{ display: 'block', fontSize: 12, marginTop: 6 }}>
                No local file cache selected.
              </Text>
            )}
          </div>

          <div>
            <Space size={6} style={{ marginBottom: 8 }}>
              <FolderOpenOutlined style={{ color: '#389e0d' }} />
              <Text strong>Local Models</Text>
              <Tag style={{ margin: 0 }}>head server</Tag>
            </Space>
            <Input
              value={modelDirInput}
              onChange={(event) => setModelDirInput(event.target.value)}
              placeholder="/root/data/Maze/model_cache"
            />
            <Space size={8} wrap style={{ marginTop: 8 }}>
              <Button
                size="small"
                icon={<ReloadOutlined />}
                loading={modelsLoading}
                onClick={async () => {
                  const result = await saveModelConfig();
                  if (result) {
                    message.success(`Scanned ${result.models?.length || 0} local models`);
                  }
                }}
              >
                Scan Path
              </Button>
              <Tag style={{ margin: 0 }}>{localModels.length} models</Tag>
            </Space>
            {localModels.length > 0 && (
              <Space.Compact style={{ width: '100%', marginTop: 8 }}>
                <Select
                  showSearch
                  value={selectedLocalModelId || undefined}
                  onChange={(value) => {
                    setSelectedLocalModelId(value);
                    setModelTestResult(null);
                  }}
                  options={localModels.map((model) => ({
                    label: localModelLabel(model),
                    value: model.id,
                  }))}
                  style={{ width: '100%' }}
                />
                <Button loading={testingModel} onClick={testLocalModel}>
                  Test Model
                </Button>
              </Space.Compact>
            )}
            {modelTestResult && (
              <Alert
                type={modelTestResult.ok ? 'success' : 'error'}
                showIcon
                style={{ marginTop: 8 }}
                message={modelTestResult.message}
                description={(
                  <Space size={[4, 4]} wrap>
                    {modelTestResult.run_id && (
                      <Tag color="blue" style={{ margin: 0 }}>run: {modelTestResult.run_id.slice(0, 8)}</Tag>
                    )}
                    {modelTestResult.task_id && (
                      <Tag color="geekblue" style={{ margin: 0 }}>task: {modelTestResult.task_id.slice(0, 8)}</Tag>
                    )}
                    {modelTestResult.runtime?.peak_cuda_reserved_bytes !== undefined && (
                      <Tag color="purple" style={{ margin: 0 }}>
                        peak: {formatGiBFromBytes(modelTestResult.runtime.peak_cuda_reserved_bytes)}
                      </Tag>
                    )}
                    {modelTestResult.checks.map((check) => (
                      <Tag key={check.name} color={check.ok ? 'green' : 'red'} style={{ margin: 0 }}>
                        {check.name}: {check.message}
                      </Tag>
                    ))}
                  </Space>
                )}
              />
            )}
            <Text type="secondary" style={{ display: 'block', fontSize: 12, marginTop: 6 }}>
              Absolute path on the head server. Browser folder selection is only for Local File Cache.
            </Text>
          </div>

          <div>
            <Space size={6} style={{ marginBottom: 8 }}>
              <ThunderboltOutlined style={{ color: '#fa8c16' }} />
              <Text strong>LLM</Text>
            </Space>
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <div>
                <Text type="secondary" style={{ display: 'block', fontSize: 12, marginBottom: 6 }}>
                  Base URL
                </Text>
                <Input
                  value={llmSettingsDraft.baseUrl}
                  onChange={(event) => setLlmSettingsDraft((current) => ({ ...current, baseUrl: event.target.value }))}
                  placeholder="https://api.siliconflow.cn/v1"
                />
              </div>
              <div>
                <Text type="secondary" style={{ display: 'block', fontSize: 12, marginBottom: 6 }}>
                  API Key
                </Text>
                <Input.Password
                  value={llmSettingsDraft.apiKey}
                  onChange={(event) => setLlmSettingsDraft((current) => ({ ...current, apiKey: event.target.value }))}
                  placeholder="sk-..."
                />
              </div>
              <div>
                <Text type="secondary" style={{ display: 'block', fontSize: 12, marginBottom: 6 }}>
                  Model
                </Text>
                <Select
                  showSearch
                  value={llmSettingsDraft.model}
                  onChange={(model) => setLlmSettingsDraft((current) => ({ ...current, model }))}
                  options={SILICONFLOW_MODELS.map((model) => ({ label: model, value: model }))}
                  style={{ width: '100%' }}
                />
              </div>
            </Space>
          </div>
        </Space>
      </Modal>

      <Modal
        title="New Workspace Task"
        open={newTaskOpen}
        onCancel={closeNewWorkspaceTaskModal}
        width={760}
        footer={newTaskMode === 'manual' ? [
          <Button key="cancel" onClick={closeNewWorkspaceTaskModal} disabled={creatingTask}>
            Cancel
          </Button>,
          <Button
            key="create"
            type="primary"
            icon={<PlusOutlined />}
            loading={creatingTask}
            disabled={workflowInteractionBlocked}
            onClick={handleCreateManualWorkspaceTask}
          >
            Create
          </Button>,
        ] : [
          <Button key="cancel" onClick={closeNewWorkspaceTaskModal} disabled={creatingTask || generatingTask}>
            Cancel
          </Button>,
          <Button key="generate" icon={<ThunderboltOutlined />} loading={generatingTask} onClick={handleGenerateWorkspaceTask}>
            Generate
          </Button>,
          <Button
            key="save"
            type="primary"
            icon={<SaveOutlined />}
            loading={creatingTask}
            disabled={workflowInteractionBlocked}
            onClick={handleSaveGeneratedWorkspaceTask}
          >
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
        title={catalogImportTitle}
        open={!!catalogImportType}
        onCancel={() => setCatalogImportType(null)}
        width={680}
        footer={[
          <Button key="close" onClick={() => setCatalogImportType(null)}>
            Close
          </Button>,
          <Button key="refresh" icon={<ReloadOutlined />} onClick={() => loadSystemCatalog(true)} loading={catalogLoading}>
            Refresh Library
          </Button>,
        ]}
      >
        {catalogImportType && renderCatalogList(catalogImportType)}
      </Modal>

      <Modal
        title="Workflow Library"
        open={workspaceWorkflowModalOpen}
        onCancel={() => setWorkspaceWorkflowModalOpen(false)}
        width={760}
        footer={[
          <Button key="close" onClick={() => setWorkspaceWorkflowModalOpen(false)}>
            Close
          </Button>,
          <Button
            key="refresh"
            icon={<ReloadOutlined />}
            onClick={refreshWorkflowLibrary}
            loading={workflowLoading || catalogLoading}
            disabled={!workspaceInteractionReady}
          >
            Refresh Workflows
          </Button>,
        ]}
      >
        {renderWorkspaceWorkflowList()}
      </Modal>
    </>
  );
}
