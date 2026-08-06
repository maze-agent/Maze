import { ChangeEvent, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Empty, Input, List, message, Modal, Radio, Select, Space, Tag, Tooltip, Typography } from 'antd';
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
  WorkflowNode,
  SystemCatalogItem,
  WorkspaceFileMeta,
  WorkspaceTaskMeta,
  WorkspaceWorkflowMeta,
} from '@/types/workflow';
import { DEFAULT_LLM_SETTINGS, SILICONFLOW_MODELS, loadLlmSettings, saveLlmSettings } from '@/utils/llmSettings';

const { Text } = Typography;
const WORKFLOW_DRAFT_PATH = 'workflows/.drafts/current.workflow.json';

function normalizeResources(resources: any = {}) {
  return {
    cpu_num: Math.max(1, Number(resources.cpu_num ?? resources.cpu ?? 1) || 1),
    gpu_mem: Math.max(0, Number(resources.gpu_mem ?? 0) || 0),
    io_num: Math.max(0, Number(resources.io_num ?? 0) || 0),
  };
}

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

function formatFileSize(size?: number | null) {
  if (size === null || size === undefined) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
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
    selectedRunId,
    setSelectedRunId,
    reset,
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
  const [selectedWorkflowPath, setSelectedWorkflowPath] = useState<string | null>(null);
  const initializedWorkspaceDirRef = useRef('');
  const fileUploadInputRef = useRef<HTMLInputElement | null>(null);
  const fileUploadTargetPathRef = useRef('');
  const normalizedWorkspaceDir = workspaceDir.trim();
  const workspaceInteractionReady = Boolean(
    workspaceReady
    && workspaceId
    && normalizedWorkspaceDir
    && initializedWorkspaceDir === normalizedWorkspaceDir,
  );
  const workflowOperationBusy = Boolean(workflowOperation);
  const isRunView = Boolean(selectedRunId);

  const requireDesignInteraction = () => {
    if (!isRunView) {
      return true;
    }
    message.info('Return to Design to edit workflows');
    return false;
  };

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

  const inputFiles = workspaceFiles.filter((file) => file.type === 'file').slice(0, 5);

  const handleNewWorkflow = () => {
    if (!requireDesignInteraction()) {
      return;
    }
    if (!requireWorkspaceInteraction()) {
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
    if (!requireDesignInteraction()) {
      return;
    }
    if (!requireWorkspaceInteraction()) {
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
      setSelectedRunId(null);
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

  const handleChangeWorkspace = async (dir: string) => {
    if (!requireDesignInteraction()) {
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

  const uploadWorkspaceFiles = async (files: File[], targetPath: string) => {
    const activeWorkspace = workspaceDir || (await loadWorkspaceFiles()).workspaceDir;

    if (files.length === 0) {
      return;
    }

    const hideLoading = message.loading('Uploading files...', 0);
    try {
      let lastUploadResult: any = null;
      for (const file of files) {
        const contentBase64 = await fileToBase64(file);
        lastUploadResult = await api.uploadWorkspaceFile({
          workspaceId: workspaceId || undefined,
          workspaceDir: activeWorkspace,
          relativePath: joinWorkspacePath(targetPath, file.name),
          contentBase64,
        });
      }
      if (lastUploadResult) {
        setWorkspaceContext(lastUploadResult);
      }
      const refreshedFiles = await loadWorkspaceFiles(activeWorkspace, workspaceFilesPath);
      await loadWorkspaceTasks(activeWorkspace);
      const targetLabel = targetPath ? ` to ${targetPath}` : '';
      const label = files.length === 1
        ? `File uploaded${targetLabel}`
        : `${files.length} files uploaded${targetLabel}`;
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
    await uploadWorkspaceFiles(files, targetPath);
    fileUploadTargetPathRef.current = workspaceFilesPath;
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

  const handleSaveWorkspaceWorkflow = async () => {
    if (!requireDesignInteraction()) {
      return;
    }
    if (!requireWorkspaceInteraction()) {
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
    if (!requireDesignInteraction()) {
      return;
    }
    if (!requireWorkspaceInteraction()) {
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
      setSelectedRunId(null);
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
      message.success(`Workflow loaded: ${loaded.workflow.name}.${taskImportText}`);
    } catch (error: any) {
      console.error('Failed to load workspace workflow:', error);
      message.error(error.response?.data?.error || 'Failed to load workspace workflow');
    } finally {
      releaseWorkflowOperation(operationToken);
    }
  };

  const handleLoadSystemWorkflow = async (item: SystemCatalogItem) => {
    if (!requireDesignInteraction()) {
      return;
    }
    if (!requireWorkspaceInteraction()) {
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
      setSelectedRunId(null);
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
    if (!requireDesignInteraction()) {
      return;
    }
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
    if (!requireDesignInteraction()) {
      return;
    }
    if (!requireWorkspaceInteraction()) {
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

  const formatUpdatedAt = (value: string) => {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
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
    if (!query) return workflowItems;
    return workflowItems.filter((item) => (
      item.name.toLowerCase().includes(query)
      || item.meta.toLowerCase().includes(query)
      || item.status.toLowerCase().includes(query)
      || item.note?.toLowerCase().includes(query)
    ));
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
      });
  }, [catalogItems.workflows, workflowSearch]);

  const visibleInputs = inputFiles.length > 0
    ? inputFiles.map((file) => ({
      id: file.relativePath,
      name: file.name,
      size: formatFileSize(file.size),
      ready: true,
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

  const renderCatalogList = (type: 'workflows' | 'tasks') => {
    const items = importableCatalogItems[type] || [];

    return (
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
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
                          disabled={isRunView || !workspaceInteractionReady || workflowOperationBusy}
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
              disabled={isRunView || !workspaceInteractionReady || workflowOperationBusy}
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
                disabled={workflowOperationBusy}
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
                    : workflowLoading || workflowOperationBusy
                      ? 'busy'
                      : 'template';
                return (
                  <button
                    key={key}
                    type="button"
                    className="workbench-nav-row"
                    disabled={isRunView || !workspaceInteractionReady || workflowLoading || workflowOperationBusy}
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
                    disabled={isRunView || !workspaceInteractionReady || nodes.length === 0 || workflowOperationBusy}
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
                  disabled={isRunView || !item.source || !workspaceInteractionReady || workflowLoading || workflowOperationBusy}
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
                    disabled={isRunView || !workspaceInteractionReady || workflowOperationBusy}
                  />
                </Tooltip>
              </div>
            </div>
            <div className="workbench-nav-list">
              <button
                type="button"
                className="workbench-nav-row"
                disabled={isRunView || !workspaceInteractionReady || workflowOperationBusy}
                onClick={() => openCatalogImport('tasks')}
              >
                <AppstoreAddOutlined />
                <span className="workbench-nav-row-label">Import tasks</span>
              </button>
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
              <span>RUNS</span>
            </div>
            <button type="button" className="workbench-nav-link" onClick={onOpenRuns}>
              Open run history
            </button>
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
              disabled={isRunView || workflowOperationBusy}
              placeholder="/root/data/Maze/workspaces/default"
            />
            <Text type="secondary" style={{ display: 'block', fontSize: 12, marginTop: 6 }}>
              Service-side workspace for workflows, tasks, files, and run artifacts.
            </Text>
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
            disabled={isRunView || workflowOperationBusy}
            onClick={handleCreateManualWorkspaceTask}
          >
            Create
          </Button>,
        ] : [
          <Button key="cancel" onClick={closeNewWorkspaceTaskModal} disabled={creatingTask || generatingTask}>
            Cancel
          </Button>,
          <Button
            key="generate"
            icon={<ThunderboltOutlined />}
            loading={generatingTask}
            disabled={isRunView}
            onClick={handleGenerateWorkspaceTask}
          >
            Generate
          </Button>,
          <Button
            key="save"
            type="primary"
            icon={<SaveOutlined />}
            loading={creatingTask}
            disabled={isRunView || workflowOperationBusy}
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
    </>
  );
}
