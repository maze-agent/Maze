import { ChangeEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Card, Divider, Drawer, Empty, Input, List, message, Modal, Radio, Select, Space, Tag, Tooltip, Typography } from 'antd';
import {
  CodeOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  EyeOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  InboxOutlined,
  AppstoreAddOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PartitionOutlined,
  PlusOutlined,
  ReloadOutlined,
  SaveOutlined,
  SettingOutlined,
  ThunderboltOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import { api } from '@/api/client';
import { useWorkflowStore } from '@/stores/workflowStore';
import { defaultReactTaskTimeout } from '@/utils/reactRuntime';
import type {
  BuiltinTaskMeta,
  LocalWorkspaceFileMeta,
  WorkflowNode,
  SystemCatalogItem,
  WorkspaceFileMeta,
  WorkspaceSkillMeta,
  WorkspaceTaskMeta,
  WorkspaceWorkflowMeta,
} from '@/types/workflow';
import { DEFAULT_LLM_SETTINGS, SILICONFLOW_MODELS, loadLlmSettings, saveLlmSettings } from '@/utils/llmSettings';

const { Text, Paragraph } = Typography;

type BuiltinWorkflowExample = {
  key: string;
  name: string;
  description: string;
  tags: string[];
  color: string;
  kind: 'react' | 'distributed-smoke' | 'workspace-task';
  recommendedSkills?: string[];
  reactMode?: 'local' | 'online';
  prompt?: string;
  maxSteps?: number;
  maxTokens?: number;
  taskSourceId?: string;
  taskRelativePath?: string;
  workflowName?: string;
};

const BUILTIN_WORKFLOW_EXAMPLES: BuiltinWorkflowExample[] = [
  {
    key: 'json-canvas-agent',
    name: 'JSON Canvas Agent',
    description: 'Creates an Obsidian JSON Canvas map and saves a .canvas artifact.',
    tags: ['agent', '.canvas', 'artifact'],
    color: '#13a8a8',
    kind: 'react',
    reactMode: 'online',
    recommendedSkills: ['json-canvas'],
    maxSteps: 5,
    maxTokens: 4096,
    prompt: [
      'Create a JSON Canvas file about "Maze workflow workspace design".',
      'Use the json-canvas skill guidance.',
      'Call write_file to save it as examples/maze-workspace.canvas.',
      'The file must be valid JSON Canvas with at least 5 text nodes and 4 edges.',
      'Finish with the artifact path and a short summary.',
    ].join(' '),
  },
  {
    key: 'debugging-agent',
    name: 'Debugging Agent',
    description: 'Turns a failure log into a root cause report using systematic debugging.',
    tags: ['agent', 'debug', 'report'],
    color: '#d46b08',
    kind: 'react',
    reactMode: 'online',
    recommendedSkills: ['systematic-debugging'],
    maxSteps: 5,
    maxTokens: 4096,
    prompt: [
      'Use the systematic-debugging skill to analyze this failing test log:',
      '"TypeError: Cannot read properties of undefined (reading tasks) at WorkflowCanvas.tsx:412 after importing a workflow template."',
      'Create a concise root cause report with evidence, likely source, and next diagnostic action.',
      'Call write_file to save the report as reports/root-cause.md, then finish with the path.',
    ].join(' '),
  },
  {
    key: 'slack-gif-agent',
    name: 'Slack GIF Agent',
    description: 'Builds a Slack-ready animated GIF artifact from a short idea.',
    tags: ['agent', 'GIF', 'creative'],
    color: '#eb2f96',
    kind: 'react',
    reactMode: 'online',
    recommendedSkills: ['slack-gif-creator'],
    maxSteps: 6,
    maxTokens: 4096,
    prompt: [
      'Create a polished 128x128 Slack emoji GIF of a tiny rocket drawing a check mark.',
      'Use the slack-gif-creator skill constraints.',
      'Call exec_code with Python/PIL code that writes examples/rocket-check.gif.',
      'Finish with the artifact path and validation notes.',
    ].join(' '),
  },
  {
    key: 'distributed-smoke',
    name: 'Distributed GPU Smoke',
    description: 'Runs two GPU probe tasks so Run Detail can show head/worker placement.',
    tags: ['distributed', 'GPU', 'placement'],
    color: '#0958d9',
    kind: 'distributed-smoke',
    workflowName: 'Distributed GPU Smoke',
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

const SKILL_PACKS = [
  {
    key: 'engineering',
    name: 'Engineering Pack',
    description: 'Debugging, tests, web app checks, and MCP building.',
    skills: ['systematic-debugging', 'test-driven-development', 'webapp-testing', 'mcp-builder'],
  },
  {
    key: 'creative',
    name: 'Creative Pack',
    description: 'Algorithmic visuals and Slack GIF creation.',
    skills: ['algorithmic-art', 'slack-gif-creator'],
  },
  {
    key: 'knowledge',
    name: 'Knowledge Pack',
    description: 'Obsidian-friendly canvas and markdown workflows.',
    skills: ['json-canvas', 'obsidian-markdown'],
  },
];

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

function compactSkillDescription(skill: WorkspaceSkillMeta) {
  const value = String(skill.description || '')
    .replace(/\s+/g, ' ')
    .trim();
  if (!value) return 'No summary available';
  return value.length > 180 ? `${value.slice(0, 177).trim()}...` : value;
}

function shortSkillPath(pathValue?: string) {
  if (!pathValue) return '';
  const parts = pathValue.replace(/\\/g, '/').split('/').filter(Boolean);
  if (parts.length <= 2) return pathValue;
  return parts.slice(-2).join('/');
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

export default function BuiltinTasksSidebar() {
  const {
    builtinTasks,
    setBuiltinTasks,
    workspaceId,
    workspaceDir,
    workspaceManifestVersion,
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
    workflowSavedAt,
    workflowDraftError,
    nodes,
    edges,
    setNodes,
    setEdges,
    setWorkflowSaveState,
    addNode,
    selectNode,
    clearRunResults,
  } = useWorkflowStore();

  const [builtinLoading, setBuiltinLoading] = useState(false);
  const [workspaceLoading, setWorkspaceLoading] = useState(false);
  const [workflowLoading, setWorkflowLoading] = useState(false);
  const [filesLoading, setFilesLoading] = useState(false);
  const [skillsLoading, setSkillsLoading] = useState(false);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogItems, setCatalogItems] = useState<Record<'workflows' | 'tasks' | 'skills', SystemCatalogItem[]>>({
    workflows: [],
    tasks: [],
    skills: [],
  });
  const [importingCatalogKey, setImportingCatalogKey] = useState<string | null>(null);
  const [catalogImportType, setCatalogImportType] = useState<'workflows' | 'tasks' | 'skills' | null>(null);
  const [importingSkillPackKey, setImportingSkillPackKey] = useState<string | null>(null);
  const [importingExampleKey, setImportingExampleKey] = useState<string | null>(null);
  const [advancedSettingsOpen, setAdvancedSettingsOpen] = useState(false);
  const [llmSettingsDraft, setLlmSettingsDraft] = useState(DEFAULT_LLM_SETTINGS);
  const [testingLlm, setTestingLlm] = useState(false);
  const [savingWorkflow, setSavingWorkflow] = useState(false);
  const [creatingTask, setCreatingTask] = useState(false);
  const [newTaskOpen, setNewTaskOpen] = useState(false);
  const [newTaskMode, setNewTaskMode] = useState<'manual' | 'ai' | 'template'>('manual');
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
  const [workspaceSkills, setWorkspaceSkills] = useState<WorkspaceSkillMeta[]>([]);
  const [workspaceSkillErrors, setWorkspaceSkillErrors] = useState<Array<{ error: string; traceback?: string }>>([]);
  const [workspaceFilesPath, setWorkspaceFilesPath] = useState('');
  const [previewFile, setPreviewFile] = useState<{ path: string; content: string } | null>(null);
  const [previewSkill, setPreviewSkill] = useState<WorkspaceSkillMeta | null>(null);
  const [fileDropActive, setFileDropActive] = useState(false);
  const [syncingLocalWorkspace, setSyncingLocalWorkspace] = useState(false);
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
  const initializedWorkspaceDirRef = useRef('');
  const taskImportInputRef = useRef<HTMLInputElement | null>(null);
  const taskImportTargetRef = useRef<WorkspaceTaskMeta | null>(null);
  const fileUploadInputRef = useRef<HTMLInputElement | null>(null);
  const folderUploadInputRef = useRef<HTMLInputElement | null>(null);
  const fileUploadTargetPathRef = useRef('');

  useEffect(() => {
    loadSystemCatalog();
    if (builtinTasks.length === 0) {
      loadBuiltinTasks(false);
    }
  }, []);

  useEffect(() => {
    const activeWorkspaceDir = workspaceDir?.trim();
    if (!activeWorkspaceDir || initializedWorkspaceDirRef.current === activeWorkspaceDir) {
      return;
    }
    initializedWorkspaceDirRef.current = activeWorkspaceDir;
    loadWorkspaceTasks(activeWorkspaceDir);
    loadWorkspaceWorkflows(activeWorkspaceDir);
    loadWorkspaceFiles(activeWorkspaceDir, '');
    loadWorkspaceSkills(activeWorkspaceDir);
  }, [workspaceDir]);

  useEffect(() => {
    if (!folderUploadInputRef.current) {
      return;
    }
    folderUploadInputRef.current.setAttribute('webkitdirectory', '');
    folderUploadInputRef.current.setAttribute('directory', '');
  }, []);

  const workflowTaskNodes = useMemo(
    () => nodes.filter((node) => node.data.nodeType === 'task'),
    [nodes],
  );

  const hasWorkflowTaskNodes = workflowTaskNodes.length > 0;
  const hasWorkflowContext = hasWorkflowTaskNodes || Boolean(currentWorkspaceWorkflowPath);
  const showingWorkflowTasks = workspaceTaskScope === 'workflow' && hasWorkflowContext;

  const renderWorkflowSaveState = () => {
    if (nodes.length === 0 || workflowSaveState === 'empty') {
      return <Tag style={{ margin: 0 }}>empty</Tag>;
    }
    if (workflowSaveState === 'unsaved_draft') {
      return <Tag color="orange" style={{ margin: 0 }}>Unsaved Draft</Tag>;
    }
    if (workflowSaveState === 'saving_draft') {
      return <Tag color="processing" style={{ margin: 0 }}>Saving Draft</Tag>;
    }
    if (workflowSaveState === 'saved_draft') {
      return (
        <Tag color="blue" style={{ margin: 0 }} title={workflowSavedAt ? `Saved at ${new Date(workflowSavedAt).toLocaleString()}` : undefined}>
          Draft Saved
        </Tag>
      );
    }
    if (workflowSaveState === 'saved_workflow') {
      return <Tag color="green" style={{ margin: 0 }}>Saved Workflow</Tag>;
    }
    return (
      <Tag color="red" style={{ margin: 0 }} title={workflowDraftError || undefined}>
        Draft Error
      </Tag>
    );
  };

  useEffect(() => {
    setWorkspaceTaskScope(hasWorkflowContext ? 'workflow' : 'all');
  }, [hasWorkflowContext]);

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

  const loadSystemCatalog = async (showSuccess = false) => {
    setCatalogLoading(true);
    try {
      const result = await api.getSystemCatalog();
      setCatalogItems({
        workflows: result.catalog.workflows || [],
        tasks: result.catalog.tasks || [],
        skills: result.catalog.skills || [],
      });
      if (showSuccess) {
        message.success('System catalog refreshed');
      }
      return result;
    } catch (error: any) {
      console.error('Failed to load system catalog:', error);
      message.error(error.response?.data?.error || 'Failed to load system catalog');
      throw error;
    } finally {
      setCatalogLoading(false);
    }
  };

  const importCatalogItem = async (item: SystemCatalogItem) => {
    const key = `${item.type}:${item.id}`;
    setImportingCatalogKey(key);
    try {
      const type = item.type as 'workflows' | 'tasks' | 'skills';
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
        const recommendedSkills = item.recommendedSkills || [];
        const missingRecommended = recommendedSkills.filter((skillName) => !workspaceSkillNames().has(skillName));
        if (missingRecommended.length > 0) {
          Modal.confirm({
            title: 'Import recommended skills?',
            content: `${item.name} recommends: ${missingRecommended.join(', ')}`,
            okText: 'Import Skills',
            cancelText: 'Skip',
            onOk: async () => {
              await importRecommendedSkills(missingRecommended);
              message.success('Recommended skills imported');
            },
          });
        }
      } else if (type === 'tasks') {
        await loadWorkspaceTasks(result.workspaceDir);
      } else {
        await loadWorkspaceSkills(result.workspaceDir);
      }
      message.success(`Imported ${item.name}`);
      setCatalogImportType(null);
    } catch (error: any) {
      console.error('Failed to import system catalog item:', error);
      message.error(error.response?.data?.error || 'Failed to import system catalog item');
    } finally {
      setImportingCatalogKey(null);
    }
  };

  const importSystemCatalogItem = async (
    type: 'workflows' | 'tasks' | 'skills',
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

  const workspaceSkillNames = () => new Set(workspaceSkills.map((skill) => skill.name));

  const importRecommendedSkills = async (skillNames: string[] = []) => {
    const uniqueSkills = Array.from(new Set(skillNames.filter(Boolean)));
    if (uniqueSkills.length === 0) {
      return [];
    }

    let knownSkills = workspaceSkillNames();
    const missing = uniqueSkills.filter((skillName) => !knownSkills.has(skillName));
    if (missing.length === 0) {
      return [];
    }

    let catalogSkills = catalogItems.skills || [];
    if (catalogSkills.length === 0) {
      const catalog = await loadSystemCatalog(false);
      catalogSkills = catalog.catalog.skills || [];
    }
    const availableCatalogSkills = new Set(catalogSkills.map((item) => item.id));
    const unknown = missing.filter((skillName) => !availableCatalogSkills.has(skillName));
    const importable = missing.filter((skillName) => availableCatalogSkills.has(skillName));

    if (unknown.length > 0) {
      message.warning(`Missing built-in skill(s): ${unknown.join(', ')}`);
    }
    if (importable.length === 0) {
      return [];
    }

    const imported: string[] = [];
    for (const skillName of importable) {
      await importSystemCatalogItem('skills', skillName, skillName);
      imported.push(skillName);
    }
    const refreshed = await loadWorkspaceSkills();
    knownSkills = new Set((refreshed.skills || []).map((skill) => skill.name));
    return imported.filter((skillName) => knownSkills.has(skillName));
  };

  const importSkillPack = async (pack: typeof SKILL_PACKS[number]) => {
    setImportingSkillPackKey(pack.key);
    try {
      const imported = await importRecommendedSkills(pack.skills);
      if (imported.length > 0) {
        message.success(`Imported ${pack.name}`);
      } else {
        message.info(`${pack.name} is already available`);
      }
    } catch (error: any) {
      console.error('Failed to import skill pack:', error);
      message.error(error.response?.data?.error || 'Failed to import skill pack');
    } finally {
      setImportingSkillPackKey(null);
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

  const loadWorkspaceSkills = async (dir?: string, showSuccess = false) => {
    setSkillsLoading(true);
    try {
      const normalizedDir = dir?.trim() || undefined;
      const result = await api.getWorkspaceSkills(normalizedDir);
      setWorkspaceContext(result);
      setWorkspaceDir(result.workspaceDir);
      setWorkspaceInput(result.workspaceDir);
      setWorkspaceSkills(result.skills || []);
      setWorkspaceSkillErrors(result.errors || []);

      if ((result.errors || []).length > 0) {
        message.warning('Some skills could not be loaded');
      } else if (showSuccess) {
        message.success('Skills refreshed');
      }

      return result;
    } catch (error: any) {
      console.error('Failed to load skills:', error);
      message.error(error.response?.data?.error || 'Failed to load skills');
      throw error;
    } finally {
      setSkillsLoading(false);
    }
  };

  const handleChangeWorkspace = async (dir: string) => {
    try {
      const tasksResult = await loadWorkspaceTasks(dir);
      await loadWorkspaceWorkflows(tasksResult.workspaceDir);
      await loadWorkspaceFiles(tasksResult.workspaceDir, '');
      await loadWorkspaceSkills(tasksResult.workspaceDir);
      setCurrentWorkspaceWorkflowPath(null);
      message.success('Workspace loaded');
    } catch (error) {
      console.error('Failed to change workspace:', error);
    }
  };

  const openAdvancedSettings = () => {
    setWorkspaceInput(workspaceDir);
    setLlmSettingsDraft(loadLlmSettings());
    setAdvancedSettingsOpen(true);
  };

  const saveAdvancedSettings = () => {
    saveLlmSettings(llmSettingsDraft);
    setAdvancedSettingsOpen(false);
    message.success('Advanced settings saved');
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

  const publishLocalWorkspaceManifest = async (
    workspaceId: string,
    displayName: string,
    files: LocalWorkspaceFileMeta[],
    version = Date.now().toString(),
  ) => {
    await api.updateLocalWorkspaceManifest(workspaceId, {
      displayName,
      version,
      files,
    });
    return version;
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

      const version = await publishLocalWorkspaceManifest(localWorkspaceId, localWorkspaceName, files);
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
        const version = await publishLocalWorkspaceManifest(workspaceId, handle.name, files);
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
      const version = await publishLocalWorkspaceManifest(workspaceId, selectionLabel, manifestFiles);
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

  const readDroppedFileEntry = (entry: any, prefix = ''): Promise<UploadCandidate[]> => {
    if (!entry) {
      return Promise.resolve([]);
    }
    if (entry.isFile) {
      return new Promise((resolve) => {
        entry.file((file: File) => {
          resolve([{ file, relativePath: normalizeLocalRelativePath(`${prefix}${file.name}`) }]);
        }, () => resolve([]));
      });
    }
    if (entry.isDirectory) {
      const reader = entry.createReader();
      const nextPrefix = `${prefix}${entry.name}/`;
      return new Promise((resolve) => {
        const entries: any[] = [];
        const readBatch = () => {
          reader.readEntries(async (batch: any[]) => {
            if (!batch.length) {
              const nested = await Promise.all(entries.map((item) => readDroppedFileEntry(item, nextPrefix)));
              resolve(nested.flat());
              return;
            }
            entries.push(...batch);
            readBatch();
          }, () => resolve([]));
        };
        readBatch();
      });
    }
    return Promise.resolve([]);
  };

  const collectDroppedFiles = async (dataTransfer: DataTransfer): Promise<UploadCandidate[]> => {
    const items = Array.from(dataTransfer.items || []);
    const entryItems = items
      .map((item) => (item as any).webkitGetAsEntry?.())
      .filter(Boolean);

    if (entryItems.length > 0) {
      const nested = await Promise.all(entryItems.map((entry) => readDroppedFileEntry(entry)));
      return nested.flat();
    }

    return Array.from(dataTransfer.files || []).map((file) => ({
      file,
      relativePath: (file as any).webkitRelativePath || file.name,
    }));
  };

  const handleFilesDrop = async (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.stopPropagation();
    setFileDropActive(false);

    if (draggingWorkspaceRef.current) {
      return;
    }

    const candidates = await collectDroppedFiles(event.dataTransfer);
    await uploadWorkspaceCandidates(candidates, {
      targetPath: workspaceFilesPath,
      refreshPath: workspaceFilesPath,
      preserveRelativePath: true,
      stripRoot: false,
      loadingLabel: 'Uploading dropped files...',
      successLabel: (count) => `${count} dropped file${count === 1 ? '' : 's'} uploaded`,
    });
  };

  const handleFilesDragOver = (event: React.DragEvent<HTMLDivElement>) => {
    if (draggingWorkspaceRef.current) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = 'copy';
    setFileDropActive(true);
  };

  const handleFilesDragLeave = (event: React.DragEvent<HTMLDivElement>) => {
    const relatedTarget = event.relatedTarget;
    if (!relatedTarget || !(relatedTarget instanceof Node) || !event.currentTarget.contains(relatedTarget)) {
      setFileDropActive(false);
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
        const created = await api.createWorkspaceFolder({
          workspaceId: workspaceId || undefined,
          workspaceDir,
          relativePath: joinWorkspacePath(workspaceFilesPath, cleanName),
        });
        setWorkspaceContext(created);
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
        const deleted = await api.deleteWorkspaceFile({
          workspaceId: workspaceId || undefined,
          workspaceDir,
          relativePath: file.relativePath,
        });
        setWorkspaceContext(deleted);
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
        workspaceId: workspaceId || undefined,
        workspaceDir: workspaceTaskEditor.workspaceDir,
        relativePath: workspaceTaskEditor.relativePath,
        code: workspaceTaskEditorCode,
        parse: true,
      });
      const updatedTask = saved.task as WorkspaceTaskMeta;
      setWorkspaceContext(saved);
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
        workspaceId: workspaceId || undefined,
        workspaceDir: targetTask.workspaceDir,
        relativePath: targetTask.relativePath,
        code,
        parse: true,
      });
      const updatedTask = saved.task as WorkspaceTaskMeta;
      setWorkspaceContext(saved);
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
          const deleted = await api.deleteWorkspaceTask({
            workspaceId: workspaceId || undefined,
            workspaceDir: task.workspaceDir,
            relativePath: task.relativePath,
          });
          setWorkspaceContext(deleted);

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
        workspaceId: workspaceId || undefined,
        workspaceDir: task.workspaceDir,
        relativePath: task.relativePath,
        oldFunctionName: task.functionName,
        newName: nextName,
      });
      const updatedTask = renamed.task as WorkspaceTaskMeta;
      setWorkspaceContext(renamed);
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
          const deleted = await api.deleteWorkspaceWorkflow({
            workspaceId: workspaceId || undefined,
            workspaceDir: activeWorkspace,
            relativePath: item.relativePath,
          });
          setWorkspaceContext(deleted);

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
      const renamed = await api.renameWorkspaceWorkflow({
        workspaceId: workspaceId || undefined,
        workspaceDir: activeWorkspace,
        relativePath: item.relativePath,
        name: nextName,
      });
      setWorkspaceContext(renamed);

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
        workspaceId: workspaceId || undefined,
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
    }
  };

  const handleLoadWorkspaceWorkflow = async (item: WorkspaceWorkflowMeta) => {
    setWorkflowLoading(true);
    try {
      const activeWorkspace = workspaceInput.trim() || workspaceDir;
      const loaded = await api.loadWorkspaceWorkflow({
        workspaceId: workspaceId || undefined,
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
      setWorkspaceContext(loaded);
      setWorkspaceDir(loaded.workspaceDir);
      setWorkspaceInput(loaded.workspaceDir);
      setCurrentWorkspaceWorkflowPath(loaded.relativePath);
      setWorkflowSaveState({
        status: 'saved_workflow',
        draftPath: 'workflows/.drafts/current.workflow.json',
        savedAt: new Date().toISOString(),
        error: null,
      });
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

  const onDragStartReAct = (event: React.DragEvent, template?: BuiltinWorkflowExample) => {
    event.dataTransfer.setData('application/reactflow', JSON.stringify({
      type: 'agent-react',
      task: {
        label: template?.name || 'ReAct Workflow',
        prompt: template?.prompt,
        reactMode: template?.reactMode || 'local',
        maxSteps: template?.maxSteps,
        maxTokens: template?.maxTokens,
        taskTimeout: defaultReactTaskTimeout(template?.reactMode || 'local'),
        skills: template?.recommendedSkills || [],
        recommendedSkills: template?.recommendedSkills || [],
      },
    }));
    event.dataTransfer.effectAllowed = 'move';
  };

  const onDragStartDistributedSmoke = (event: React.DragEvent) => {
    event.dataTransfer.setData('application/reactflow', JSON.stringify({
      type: 'workflow-distributed-smoke',
      task: {
        label: 'Distributed Smoke Workflow',
      },
    }));
    event.dataTransfer.effectAllowed = 'move';
  };

  const onDragStartWorkflowExample = (event: React.DragEvent, template: BuiltinWorkflowExample) => {
    if (template.kind === 'react') {
      onDragStartReAct(event, template);
    } else if (template.kind === 'distributed-smoke') {
      onDragStartDistributedSmoke(event);
    } else {
      event.dataTransfer.setData('application/reactflow', JSON.stringify({
        type: 'workspace-example-task',
        task: {
          label: template.name,
          taskSourceId: template.taskSourceId,
          taskRelativePath: template.taskRelativePath,
        },
      }));
      event.dataTransfer.effectAllowed = 'move';
    }
  };

  const addBuiltinTaskToCanvas = async (task: BuiltinTaskMeta, closeAfterAdd = false) => {
    await ensureWorkflow();
    const newNode: WorkflowNode = {
      id: `node-${Date.now()}`,
      type: 'taskNode',
      position: getDefaultPosition(),
      data: {
        category: 'builtin',
        nodeType: 'task',
        label: task.displayName,
        taskRef: `${task.module}.${task.functionRef}`,
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
    };

    addNode(newNode);
    selectNode(newNode);
    if (closeAfterAdd) {
      setNewTaskOpen(false);
    }
    message.success(`${task.displayName} added to canvas`);
  };

  const addReactTemplateToCanvas = async (template: BuiltinWorkflowExample = BUILTIN_WORKFLOW_EXAMPLES[0]) => {
    await ensureWorkflow();
    const importedSkills = await importRecommendedSkills(template.recommendedSkills || []);
    if (template.workflowName || template.name) {
      setWorkflowName(template.workflowName || template.name);
    }
    const newNode: WorkflowNode = {
      id: `node-${Date.now()}`,
      type: 'taskNode',
      position: getDefaultPosition(),
      data: {
        category: 'agent',
        nodeType: 'task',
        label: template.name || 'ReAct Workflow',
        agentKind: 'react',
        reactMode: template.reactMode || 'local',
        prompt: template.prompt || 'Use the calculator to compute 18 * 7, then give the final answer.',
        maxSteps: template.maxSteps || 4,
        maxTokens: template.maxTokens || 2048,
        taskTimeout: defaultReactTaskTimeout(template.reactMode || 'local'),
        skills: template.recommendedSkills || [],
        recommendedSkills: template.recommendedSkills || [],
        inputs: [],
        outputs: [{ name: 'answer', dataType: 'any' }],
        configured: true,
      },
    };

    addNode(newNode);
    selectNode(newNode);
    setCatalogImportType(null);
    message.success(importedSkills.length > 0
      ? `${template.name} added with ${importedSkills.length} skill(s)`
      : `${template.name} added`);
  };

  const addDistributedSmokeTemplateToCanvas = async (template?: BuiltinWorkflowExample) => {
    await ensureWorkflow();
    setWorkflowName(template?.workflowName || 'Distributed GPU Smoke');
    const position = getDefaultPosition();
    const baseId = Date.now();
    const smokeNodes: WorkflowNode[] = Array.from({ length: 2 }, (_, index) => ({
      id: `node-${baseId}-${index + 1}`,
      type: 'taskNode',
      position: {
        x: position.x + (index % 2) * 260,
        y: position.y + Math.floor(index / 2) * 180,
      },
      data: {
        category: 'builtin',
        nodeType: 'task',
        label: `GPU Probe ${index + 1}`,
        taskRef: 'distributedSmoke.distributed_gpu_probe',
        inputs: [
          {
            name: 'probe_id',
            dataType: 'int',
            source: 'user',
            value: String(index + 1),
          },
          {
            name: 'sleep_seconds',
            dataType: 'int',
            source: 'user',
            value: '1',
          },
        ],
        outputs: [{ name: 'placement', dataType: 'dict' }],
        resources: { cpu: 1, cpu_mem: 128, gpu: 1, gpu_mem: 0 },
        configured: true,
      },
    }));

    smokeNodes.forEach((node) => addNode(node));
    selectNode(smokeNodes[0]);
    setCatalogImportType(null);
    message.success(`${template?.name || 'Distributed smoke workflow'} added`);
  };

  const importWorkspaceExampleTask = async (template: BuiltinWorkflowExample) => {
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

  const addWorkspaceExampleTaskToCanvas = async (template: BuiltinWorkflowExample) => {
    await ensureWorkflow();
    setWorkflowName(template.workflowName || template.name);
    const task = await importWorkspaceExampleTask(template);
    const newNode = createWorkspaceNode(task);
    addNode(newNode);
    selectNode(newNode);
    setCatalogImportType(null);
    message.success(`${template.name} added`);
  };

  const addWorkflowExampleToCanvas = async (template: BuiltinWorkflowExample) => {
    setImportingExampleKey(template.key);
    try {
      if (template.kind === 'react') {
        await addReactTemplateToCanvas(template);
      } else if (template.kind === 'distributed-smoke') {
        await addDistributedSmokeTemplateToCanvas(template);
      } else {
        await addWorkspaceExampleTaskToCanvas(template);
      }
    } catch (error: any) {
      console.error('Failed to add workflow example:', error);
      message.error(error.response?.data?.error || error.message || 'Failed to add workflow example');
    } finally {
      setImportingExampleKey(null);
    }
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

  const renderWorkflowNodeDetails = (node: WorkflowNode) => (
    <div style={{ fontSize: '12px', color: '#666' }}>
      {node.data.taskRef && (
        <div style={{ marginBottom: '6px' }}>
          <Text type="secondary">Task ref: </Text>
          <Tag>{node.data.taskRef}</Tag>
        </div>
      )}
      {node.data.taskPath && (
        <div style={{ marginBottom: '6px' }}>
          <Text type="secondary">File: </Text>
          <Tag>{node.data.taskPath}</Tag>
        </div>
      )}
      {node.data.prompt && (
        <Paragraph
          type="secondary"
          ellipsis={{ rows: 3, tooltip: node.data.prompt }}
          style={{ fontSize: '12px', marginBottom: '8px' }}
        >
          {node.data.prompt}
        </Paragraph>
      )}
      <Space size={[4, 4]} wrap style={{ marginBottom: '8px' }}>
        <Tag color="geekblue">Inputs {node.data.inputs.length}</Tag>
        <Tag color="cyan">Outputs {node.data.outputs.length}</Tag>
        {node.data.reactMode && <Tag color="purple">{node.data.reactMode}</Tag>}
      </Space>
      <div>
        <Text type="secondary">In: </Text>
        {node.data.inputs.length === 0 ? (
          <Tag>none</Tag>
        ) : node.data.inputs.map((input) => (
          <Tag key={input.name} style={{ marginBottom: '4px' }}>
            {input.name}:{input.dataType}
          </Tag>
        ))}
      </div>
      <div>
        <Text type="secondary">Out: </Text>
        {node.data.outputs.length === 0 ? (
          <Tag>none</Tag>
        ) : node.data.outputs.map((output) => (
          <Tag key={output.name} style={{ marginBottom: '4px' }}>
            {output.name}:{output.dataType}
          </Tag>
        ))}
      </div>
      {node.data.resources && (
        <Text type="secondary" style={{ display: 'block', fontSize: '11px', marginTop: '6px' }}>
          CPU {node.data.resources.cpu}, GPU {node.data.resources.gpu}, Mem {node.data.resources.cpu_mem}MB, VRAM {node.data.resources.gpu_mem}MB
        </Text>
      )}
    </div>
  );

  const renderWorkflowTaskNodeCard = (node: WorkflowNode) => {
    const nodeKey = `workflow-node:${node.id}`;
    const expanded = expandedTaskKey === nodeKey;
    const workspaceTask = node.data.category === 'workspace'
      ? workspaceTasks.find((task) =>
          task.workspaceDir === node.data.workspaceDir &&
          task.relativePath === node.data.taskPath &&
          task.functionName === node.data.functionName)
      : null;
    const builtinTask = node.data.category === 'builtin'
      ? builtinTasks.find((task) => `${task.module}.${task.functionRef}` === node.data.taskRef)
      : null;
    const matchedTask = workspaceTask || builtinTask;
    const icon = node.data.category === 'agent'
      ? <PartitionOutlined style={{ color: '#722ed1' }} />
      : node.data.category === 'workspace'
        ? <CodeOutlined style={{ color: '#722ed1' }} />
        : <ThunderboltOutlined style={{ color: '#1677ff' }} />;
    const categoryColor = node.data.category === 'agent'
      ? 'purple'
      : node.data.category === 'workspace'
        ? 'geekblue'
        : 'blue';

    return (
      <Card
        key={node.id}
        size="small"
        style={{
          marginBottom: '8px',
          cursor: 'pointer',
          borderColor: selectedWorkspaceTaskKey === nodeKey ? '#1677ff' : undefined,
        }}
        hoverable
        tabIndex={0}
        onClick={(event) => {
          event.currentTarget.focus();
          setSelectedWorkspaceTaskKey(nodeKey);
          selectNode(node);
          toggleTask(nodeKey);
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px' }}>
          {icon}
          <strong
            style={{
              maxWidth: '230px',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {node.data.label}
          </strong>
        </div>
        <Space size={[4, 4]} wrap style={{ marginBottom: '6px' }}>
          <Tag color={categoryColor}>{node.data.category}</Tag>
          <Tag>canvas</Tag>
          {node.data.agentKind && <Tag>{node.data.agentKind}</Tag>}
        </Space>
        {matchedTask ? (
          node.data.category === 'workspace'
            ? renderWorkspaceTaskSummary(matchedTask as WorkspaceTaskMeta)
            : renderBuiltinTaskSummary(matchedTask as BuiltinTaskMeta)
        ) : (
          <Paragraph
            type="secondary"
            ellipsis={{ rows: 2, tooltip: node.data.prompt || node.data.taskRef || node.data.taskPath || node.id }}
            style={{ fontSize: '12px', marginBottom: '6px' }}
          >
            {node.data.prompt || node.data.taskRef || node.data.taskPath || 'Canvas task node'}
          </Paragraph>
        )}
        {expanded && (
          <>
            <Divider style={{ margin: '10px 0' }} />
            {matchedTask ? renderTaskDetails(matchedTask) : renderWorkflowNodeDetails(node)}
          </>
        )}
      </Card>
    );
  };

  const renderBuiltinWorkflowTemplates = () => {
    return (
      <div>
        <Space align="center" size={6} style={{ marginBottom: 8 }}>
          <Text strong>Examples</Text>
          <Tag color="purple" style={{ margin: 0 }}>canvas</Tag>
        </Space>
        <List
          size="small"
          dataSource={BUILTIN_WORKFLOW_EXAMPLES}
          renderItem={(template) => (
            <List.Item
              className="workspace-file-row"
              draggable
              onDragStart={(event) => onDragStartWorkflowExample(event, template)}
              style={{ cursor: 'grab' }}
              actions={[
                <Button
                  key="add"
                  type="primary"
                  size="small"
                  icon={<PlusOutlined />}
                  loading={importingExampleKey === template.key}
                  onClick={() => addWorkflowExampleToCanvas(template)}
                >
                  Add
                </Button>,
              ]}
            >
              <List.Item.Meta
                avatar={<PartitionOutlined style={{ color: template.color }} />}
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
                      {(template.recommendedSkills || []).map((skillName) => (
                        <Tag key={skillName} color="green" style={{ margin: 0 }}>
                          skill:{skillName}
                        </Tag>
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

  const renderBuiltinTaskTemplates = (closeAfterAdd = false) => (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, marginBottom: 8 }}>
        <Space align="center" size={6}>
          <Text strong>Built-in Templates</Text>
          <Tag color="blue" style={{ margin: 0 }}>canvas</Tag>
        </Space>
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
      <List
        size="small"
        loading={builtinLoading}
        dataSource={builtinTasks}
        locale={{
          emptyText: (
            <Empty description="No built-in tasks" image={Empty.PRESENTED_IMAGE_SIMPLE}>
              <Button size="small" icon={<ReloadOutlined />} onClick={() => loadBuiltinTasks(true)}>
                Load Templates
              </Button>
            </Empty>
          ),
        }}
        renderItem={(task) => {
          const taskKey = getBuiltinTaskKey(task);
          const expanded = expandedTaskKey === taskKey;

          return (
            <List.Item
              className="workspace-file-row"
              draggable
              onDragStart={(event) => onDragStartBuiltin(event, task)}
              onClick={() => {
                setSelectedWorkspaceTaskKey(null);
                toggleTask(taskKey);
              }}
              style={{ cursor: 'grab' }}
              actions={[
                <Button
                  key="add"
                  size="small"
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={(event) => {
                    event.stopPropagation();
                    addBuiltinTaskToCanvas(task, closeAfterAdd);
                  }}
                >
                  Add
                </Button>,
              ]}
            >
              <List.Item.Meta
                avatar={<ThunderboltOutlined style={{ color: '#1677ff' }} />}
                title={
                  <Text style={{ fontSize: 13, maxWidth: 230 }} ellipsis={{ tooltip: task.displayName }}>
                    {task.displayName}
                  </Text>
                }
                description={
                  <div>
                    {renderBuiltinTaskSummary(task)}
                    {expanded && (
                      <>
                        <Divider style={{ margin: '8px 0' }} />
                        {renderTaskDetails(task)}
                      </>
                    )}
                  </div>
                }
              />
            </List.Item>
          );
        }}
      />
    </div>
  );

  const renderSkillPacks = () => (
    <div>
      <Space align="center" size={6} style={{ marginBottom: 8 }}>
        <Text strong>Skill Packs</Text>
        <Tag color="green" style={{ margin: 0 }}>one-click import</Tag>
      </Space>
      <List
        size="small"
        dataSource={SKILL_PACKS}
        renderItem={(pack) => {
          const availableCount = pack.skills.filter((skillName) => workspaceSkillNames().has(skillName)).length;
          return (
            <List.Item
              className="workspace-file-row"
              actions={[
                <Button
                  key="import"
                  type="primary"
                  size="small"
                  icon={<DownloadOutlined />}
                  loading={importingSkillPackKey === pack.key}
                  onClick={() => importSkillPack(pack)}
                >
                  Import
                </Button>,
              ]}
            >
              <List.Item.Meta
                avatar={<AppstoreAddOutlined style={{ color: '#389e0d' }} />}
                title={<Text strong>{pack.name}</Text>}
                description={
                  <Space direction="vertical" size={4}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {pack.description}
                    </Text>
                    <Space size={4} wrap>
                      <Tag color={availableCount === pack.skills.length ? 'green' : 'default'} style={{ margin: 0 }}>
                        {availableCount}/{pack.skills.length} available
                      </Tag>
                      {pack.skills.map((skillName) => (
                        <Tag key={skillName} style={{ margin: 0 }}>{skillName}</Tag>
                      ))}
                    </Space>
                  </Space>
                }
              />
            </List.Item>
          );
        }}
      />
    </div>
  );

  const renderCatalogList = (type: 'workflows' | 'tasks' | 'skills') => {
    const items = catalogItems[type] || [];

    return (
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {type === 'workflows' && renderBuiltinWorkflowTemplates()}
        {type === 'tasks' && renderBuiltinTaskTemplates()}
        {type === 'skills' && renderSkillPacks()}

        <div>
          <Space align="center" size={6} style={{ marginBottom: 8 }}>
            <Text strong>Built-in Library</Text>
            <Tag color="geekblue" style={{ margin: 0 }}>built-in</Tag>
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
                            <Tag color="geekblue" style={{ margin: 0 }}>built-in</Tag>
                            <Tag style={{ margin: 0 }}>{item.kind}</Tag>
                            {(item.tags || []).map((tag) => (
                              <Tag key={tag} style={{ margin: 0 }}>{tag}</Tag>
                            ))}
                            {(item.recommendedSkills || []).map((skillName) => (
                              <Tag key={skillName} color="green" style={{ margin: 0 }}>
                                skill:{skillName}
                              </Tag>
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

  const openCatalogImport = (type: 'workflows' | 'tasks' | 'skills') => {
    setCatalogImportType(type);
    void loadSystemCatalog(false);
  };

  const catalogImportTitle = catalogImportType
    ? `Import ${catalogImportType === 'skills' ? 'Skills' : catalogImportType === 'tasks' ? 'Tasks' : 'Workflows'}`
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
        <div style={{ padding: '16px' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
            <Space size={8}>
              <h3 style={{ margin: 0 }}>Workspace</h3>
              <Button
                size="small"
                icon={<SettingOutlined />}
                onClick={openAdvancedSettings}
              >
                Advanced Setting
              </Button>
            </Space>
            {toggleButton}
          </div>

          {workspaceId && (
            <Space size={[4, 4]} wrap style={{ marginBottom: '10px' }}>
              <Tag color="blue" style={{ margin: 0 }}>{workspaceId}</Tag>
              {workspaceManifestVersion !== null && workspaceManifestVersion !== undefined && (
                <Tag style={{ margin: 0 }}>v{workspaceManifestVersion}</Tag>
              )}
            </Space>
          )}

          {workspaceErrors.length > 0 && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: '12px' }}
              message={`${workspaceErrors.length} task file${workspaceErrors.length > 1 ? 's' : ''} failed to load`}
              description={workspaceErrors.slice(0, 2).map((item) => `${item.relativePath}: ${item.error}`).join('\n')}
            />
          )}

          <div className="workspace-workflows-header">
            <div className="workspace-workflows-title">
              <h3 style={{ margin: 0 }}>Workflows</h3>
              <Tag color={showingWorkflowTasks ? 'purple' : 'blue'} style={{ margin: 0 }}>
                {showingWorkflowTasks ? 'canvas' : 'service'}
              </Tag>
            </div>
            <div className="workspace-workflows-actions">
              <Tooltip title="Refresh workflows">
                <Button
                  type="text"
                  size="small"
                  className="workspace-sidebar-icon-button"
                  icon={<ReloadOutlined />}
                  aria-label="Refresh workflows"
                  onClick={() => loadWorkspaceWorkflows(workspaceInput || workspaceDir, true)}
                  loading={workflowLoading}
                />
              </Tooltip>
              <Tooltip title="Open workflow library">
                <Button
                  size="small"
                  className="workspace-sidebar-icon-button"
                  icon={<DownloadOutlined />}
                  aria-label="Open workflow library"
                  onClick={() => openCatalogImport('workflows')}
                />
              </Tooltip>
              <Tooltip title="Save current workflow">
              <Button
                type="primary"
                size="small"
                className="workspace-sidebar-icon-button"
                icon={<SaveOutlined />}
                aria-label="Save current workflow"
                onClick={handleSaveWorkspaceWorkflow}
                loading={savingWorkflow}
                disabled={nodes.length === 0}
              />
              </Tooltip>
            </div>
          </div>
          <Space size={4} wrap style={{ marginBottom: 8 }}>
            {renderWorkflowSaveState()}
            {currentWorkspaceWorkflowPath ? (
              <Text type="secondary" style={{ fontSize: '11px' }}>
                Current: {currentWorkspaceWorkflowPath}
              </Text>
            ) : nodes.length > 0 ? (
              <Text type="secondary" style={{ fontSize: '11px' }}>
                Current: service-side draft
              </Text>
            ) : null}
          </Space>
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

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
            <Space size={6}>
              <h3 style={{ margin: 0 }}>
                {showingWorkflowTasks ? 'Workflow Tasks' : 'Tasks'}
              </h3>
              <Tag color="blue" style={{ margin: 0 }}>service</Tag>
            </Space>
            <Space size={4}>
              <Button
                type="text"
                size="small"
                icon={<ReloadOutlined />}
                onClick={() => loadWorkspaceTasks(workspaceInput || workspaceDir, true)}
                loading={workspaceLoading}
              />
              <Button
                size="small"
                icon={<DownloadOutlined />}
                onClick={() => openCatalogImport('tasks')}
              >
                Library
              </Button>
            </Space>
          </div>
          {hasWorkflowContext && (
            <Radio.Group
              size="small"
              value={workspaceTaskScope}
              onChange={(event) => setWorkspaceTaskScope(event.target.value)}
              style={{ marginBottom: '8px' }}
            >
              <Radio.Button value="workflow">Current Workflow</Radio.Button>
              <Radio.Button value="all">All Tasks</Radio.Button>
            </Radio.Group>
          )}
          <p style={{ fontSize: '12px', color: '#999', marginBottom: '12px' }}>
            {showingWorkflowTasks
              ? 'Canvas tasks in the current workflow'
              : 'Workspace task files. Click to expand or drag to canvas'}
          </p>

          {showingWorkflowTasks ? (
            workflowTaskNodes.length === 0 ? (
              <Empty description="No tasks in current workflow" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
              <List
                dataSource={workflowTaskNodes}
                renderItem={(node) => renderWorkflowTaskNodeCard(node)}
              />
            )
          ) : workspaceTasks.length === 0 ? (
            <Empty
              description="No tasks"
              image={Empty.PRESENTED_IMAGE_SIMPLE}
            >
              <Button size="small" icon={<PlusOutlined />} onClick={openNewWorkspaceTaskModal} loading={creatingTask}>
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

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
            <Space size={6}>
              <h3 style={{ margin: 0 }}>Files</h3>
              <Tag color="blue" style={{ margin: 0 }}>service</Tag>
            </Space>
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
          <div
            className={`workspace-file-dropzone${fileDropActive ? ' is-active' : ''}`}
            onDrop={handleFilesDrop}
            onDragOver={handleFilesDragOver}
            onDragLeave={handleFilesDragLeave}
          >
            <div className="workspace-file-dropzone-header">
              <Space size={8}>
                <InboxOutlined />
                <Text strong>files/{workspaceFilesPath || ''}</Text>
              </Space>
              {workspaceFilesPath && (
                <Button
                  size="small"
                  type="link"
                  style={{ padding: 0 }}
                  onClick={() => loadWorkspaceFiles(workspaceDir, parentWorkspacePath(workspaceFilesPath))}
                >
                  Parent
                </Button>
              )}
            </div>
            <Text type="secondary" style={{ display: 'block', fontSize: '11px', marginBottom: '8px' }}>
              Workspace Files are available to runs when explicitly selected or materialized.
            </Text>
            <List
              size="small"
              loading={filesLoading}
              dataSource={workspaceFiles}
              locale={{ emptyText: <Empty description="No workspace files" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
              renderItem={(file) => (
                <List.Item
                  className="workspace-file-row"
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
                    avatar={
                      <span className={`workspace-file-icon workspace-file-icon-${file.type}`}>
                        {file.type === 'directory' ? <FolderOpenOutlined /> : <FileTextOutlined />}
                      </span>
                    }
                    title={
                      file.type === 'directory' ? (
                        <Button
                          type="link"
                          size="small"
                          style={{ padding: 0, maxWidth: '150px', overflow: 'hidden', textOverflow: 'ellipsis' }}
                          onClick={() => loadWorkspaceFiles(workspaceDir, file.relativePath)}
                        >
                          {file.name}
                        </Button>
                      ) : (
                        <Text style={{ fontSize: '13px', maxWidth: '150px' }} ellipsis={{ tooltip: file.relativePath }}>
                          {file.name}
                        </Text>
                      )
                    }
                    description={
                      <Space size={4} wrap>
                        <Tag color={file.type === 'directory' ? 'geekblue' : 'default'} style={{ margin: 0 }}>
                          {file.type === 'file' ? formatFileSize(file.size) : 'folder'}
                        </Tag>
                        {file.updatedAt && (
                          <Text type="secondary" style={{ fontSize: '11px' }}>
                            {formatUpdatedAt(file.updatedAt)}
                          </Text>
                        )}
                      </Space>
                    }
                  />
                </List.Item>
              )}
            />
          </div>

          <Divider />

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
            <Space size={6}>
              <h3 style={{ margin: 0 }}>Skills</h3>
              <Tag color="blue" style={{ margin: 0 }}>service</Tag>
            </Space>
            <Space size={4}>
              <Button
                type="text"
                size="small"
                icon={<ReloadOutlined />}
                onClick={() => loadWorkspaceSkills(workspaceInput || workspaceDir, true)}
                loading={skillsLoading}
              />
              <Button
                size="small"
                icon={<DownloadOutlined />}
                onClick={() => openCatalogImport('skills')}
              >
                Library
              </Button>
            </Space>
          </div>
          {workspaceSkillErrors.length > 0 && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: '8px' }}
              message={`${workspaceSkillErrors.length} skill${workspaceSkillErrors.length > 1 ? 's' : ''} failed to load`}
            />
          )}
          {workspaceSkills.length === 0 ? (
            <Empty description="No skills" image={Empty.PRESENTED_IMAGE_SIMPLE}>
              <Button size="small" icon={<DownloadOutlined />} onClick={() => openCatalogImport('skills')}>
                Open Library
              </Button>
            </Empty>
          ) : (
            <List
              size="small"
              loading={skillsLoading}
              dataSource={workspaceSkills}
              renderItem={(skill) => {
                const skillSummary = compactSkillDescription(skill);
                const compactPath = shortSkillPath(skill.path);

                return (
                  <List.Item
                    className="workspace-file-row"
                    actions={[
                      <Tooltip key="preview" title="Preview">
                        <Button
                          type="text"
                          size="small"
                          icon={<EyeOutlined />}
                          onClick={() => setPreviewSkill(skill)}
                        />
                      </Tooltip>,
                    ]}
                  >
                    <List.Item.Meta
                      avatar={<AppstoreAddOutlined style={{ color: '#722ed1' }} />}
                      title={
                        <Text style={{ fontSize: '13px', maxWidth: '190px' }} ellipsis={{ tooltip: skill.name }}>
                          {skill.name}
                        </Text>
                      }
                      description={
                        <Space direction="vertical" size={4} style={{ width: '100%' }}>
                          <Paragraph
                            type="secondary"
                            ellipsis={{ rows: 2 }}
                            style={{ fontSize: '11px', marginBottom: 0, lineHeight: 1.35 }}
                          >
                            {skillSummary}
                          </Paragraph>
                          <Space size={4} wrap>
                            <Tag color="purple" style={{ margin: 0 }}>skill</Tag>
                            {skill.resources && skill.resources.length > 0 && (
                              <Tag style={{ margin: 0 }}>{skill.resources.length} resource(s)</Tag>
                            )}
                            {compactPath && (
                              <Tag style={{ margin: 0 }}>{compactPath}</Tag>
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
              placeholder="/root/data/Maze/workspace"
            />
            <Text type="secondary" style={{ display: 'block', fontSize: 12, marginTop: 6 }}>
              Service-side workspace for workflows, tasks, skills, files, and run artifacts.
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
        footer={newTaskMode === 'template' ? [
          <Button key="close" onClick={closeNewWorkspaceTaskModal}>
            Close
          </Button>,
        ] : newTaskMode === 'manual' ? [
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
              { label: 'Built-in Templates', value: 'template' },
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

          {newTaskMode !== 'template' && (
            <>
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
            </>
          )}

          {newTaskMode === 'template' ? (
            renderBuiltinTaskTemplates(true)
          ) : newTaskMode === 'manual' ? (
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

      <Modal
        title={previewSkill?.name || 'Skill'}
        open={!!previewSkill}
        onCancel={() => setPreviewSkill(null)}
        footer={[
          <Button key="close" onClick={() => setPreviewSkill(null)}>
            Close
          </Button>,
        ]}
        width={720}
        destroyOnClose
      >
        {previewSkill && (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Space size={4} wrap>
              <Tag color="purple">workspace</Tag>
              {previewSkill.truncated && <Tag color="orange">truncated</Tag>}
              {previewSkill.resources && previewSkill.resources.length > 0 && (
                <Tag color="geekblue">{previewSkill.resources.length} resource(s)</Tag>
              )}
              {previewSkill.original_chars !== undefined && (
                <Tag>{previewSkill.returned_chars || 0}/{previewSkill.original_chars} chars</Tag>
              )}
            </Space>

            {previewSkill.description && (
              <Paragraph style={{ marginBottom: 0 }}>
                {previewSkill.description}
              </Paragraph>
            )}

            {previewSkill.path && (
              <Text copyable={{ text: previewSkill.path }} type="secondary" style={{ fontSize: 12 }}>
                {previewSkill.path}
              </Text>
            )}

            {previewSkill.resources && previewSkill.resources.length > 0 && (
              <div>
                <Text strong>Resources</Text>
                <pre
                  style={{
                    marginTop: 6,
                    maxHeight: 180,
                    overflow: 'auto',
                    background: '#f5f5f5',
                    padding: 12,
                    borderRadius: 4,
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                    fontSize: 12,
                  }}
                >
                  {JSON.stringify(previewSkill.resources, null, 2)}
                </pre>
              </div>
            )}

            {previewSkill.metadata && Object.keys(previewSkill.metadata).length > 0 && (
              <div>
                <Text strong>Metadata</Text>
                <pre
                  style={{
                    marginTop: 6,
                    maxHeight: 220,
                    overflow: 'auto',
                    background: '#f5f5f5',
                    padding: 12,
                    borderRadius: 4,
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                    fontSize: 12,
                  }}
                >
                  {JSON.stringify(previewSkill.metadata, null, 2)}
                </pre>
              </div>
            )}
          </Space>
        )}
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
