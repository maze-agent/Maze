import express from 'express';
import cors from 'cors';
import { spawn } from 'child_process';
import { v4 as uuidv4 } from 'uuid';
import path from 'path';
import { fileURLToPath } from 'url';
import http from 'http';
import fs from 'fs/promises';
import fsSync from 'fs';
import crypto from 'crypto';
import os from 'os';
import { tmpdir } from 'os';
import { BUILTIN_TASK_ALIASES, compileWorkflowToDagSpec } from './workflow_dag_spec.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const app = express();
const server = http.createServer(app);

app.use(cors());
app.use(express.json({ limit: '50mb' }));

const workspaceAgentCapabilities = new Map();
const agentSessionWriteQueues = new Map();
const localWorkspaceManifests = new Map();
const PROJECT_ROOT = path.resolve(__dirname, '../../../..');
const WORKSPACE_ROOT_DIR = path.resolve(process.env.MAZE_WORKSPACE_ROOT_DIR || process.env.MAZE_WORKSPACE_DIR || path.join(PROJECT_ROOT, 'workspaces'));
const WORKSPACES_DIR = path.resolve(process.env.MAZE_WORKSPACES_DIR || WORKSPACE_ROOT_DIR);
const DEFAULT_WORKSPACE_ID = process.env.MAZE_DEFAULT_WORKSPACE_ID || 'default';
const DEFAULT_WORKSPACE_DIR = path.join(WORKSPACES_DIR, DEFAULT_WORKSPACE_ID);
const LEGACY_WORKSPACE_DIR = WORKSPACE_ROOT_DIR;
const SYSTEM_CATALOG_DIR = path.resolve(process.env.MAZE_SYSTEM_CATALOG_DIR || path.join(PROJECT_ROOT, 'system_catalog'));
const MAZE_CORE_URL = process.env.MAZE_CORE_URL || 'http://localhost:8000';
const GAIA_STAGING_ROOT = path.resolve(
  process.env.MAZE_GAIA_STAGING_ROOT
    || path.join(os.homedir(), '.maze', 'playground', 'gaia-staging'),
);
const MAZE_CORE_REQUEST_TIMEOUT_MS = Math.min(
  5 * 60 * 1000,
  Math.max(100, Number(process.env.MAZE_CORE_REQUEST_TIMEOUT_MS) || 30 * 1000),
);
const TERMINAL_STATIC_RUN_STATUSES = new Set(['completed', 'failed', 'canceled', 'timed_out', 'interrupted']);
const GAIA_TRACE_WORKFLOWS = new Set(['reason', 'file']);
const GAIA_SAMPLE_REF_PATTERN = /^gaia-[0-9a-f]{32}$/;
const GAIA_SUBMISSION_TOKEN_PATTERN = /^[0-9a-f]{64}$/;
const GAIA_FILE_EXTENSIONS = new Set(['.txt', '.md', '.pdf']);
const GAIA_MAX_FILE_BYTES = Math.min(
  32 * 1024 * 1024,
  Math.max(1, Number(process.env.MAZE_GAIA_MAX_FILE_BYTES) || 32 * 1024 * 1024),
);
const GAIA_TERMINAL_EVENTS = {
  succeeded: { type: 'benchmark_run_succeeded', status: 'completed' },
  failed: { type: 'benchmark_run_failed', status: 'failed' },
  cancelled: { type: 'benchmark_run_canceled', status: 'canceled' },
  canceled: { type: 'benchmark_run_canceled', status: 'canceled' },
  timed_out: { type: 'benchmark_run_timed_out', status: 'timed_out' },
  interrupted: { type: 'benchmark_run_interrupted', status: 'interrupted' },
};
const staticRunWriteQueues = new Map();
const systemWorkflowLoadQueues = new Map();
const recoveredStaticRunWorkspaces = new Map();
const activeGaiaSubmissions = new Set();
const workspaceTasksCache = new Map();
const activeWorkerProfileSecrets = new Map();

function getPythonBin() {
  if (process.env.PYTHON_BIN) {
    return process.env.PYTHON_BIN;
  }
  if (process.env.MAZE_CONDA_PREFIX) {
    return path.join(
      process.env.MAZE_CONDA_PREFIX,
      process.platform === 'win32' ? 'python.exe' : 'bin/python'
    );
  }

  const defaultMazePython = '/root/miniconda3/envs/maze/bin/python';
  if (process.platform !== 'win32' && fsSync.existsSync(defaultMazePython)) {
    return defaultMazePython;
  }

  if (process.env.CONDA_PREFIX) {
    return path.join(
      process.env.CONDA_PREFIX,
      process.platform === 'win32' ? 'python.exe' : 'bin/python'
    );
  }

  return 'python';
}

const PYTHON_BIN = getPythonBin();

// ========== 工作目录文件辅助函数 ==========

function toPosixPath(filePath) {
  return filePath.split(path.sep).join('/');
}

async function workspaceTasksSignature(workspaceDir) {
  const tasksDir = path.join(workspaceDir, 'tasks');
  const files = [];

  async function walk(dir) {
    const entries = await fs.readdir(dir, { withFileTypes: true }).catch((error) => {
      if (error?.code === 'ENOENT') {
        return [];
      }
      throw error;
    });

    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        await walk(fullPath);
        continue;
      }
      if (!entry.isFile() || !entry.name.endsWith('.py') || entry.name.startsWith('__')) {
        continue;
      }

      const stat = await fs.stat(fullPath);
      files.push([
        toPosixPath(path.relative(workspaceDir, fullPath)),
        stat.mtimeMs,
        stat.size,
      ].join(':'));
    }
  }

  await walk(tasksDir);
  return files.sort().join('|');
}

function clearWorkspaceTasksCache(workspaceDir) {
  if (workspaceDir) {
    workspaceTasksCache.delete(path.resolve(workspaceDir));
  }
}

function safeFileName(name, fallbackPrefix = 'workflow') {
  const safeName = String(name || '')
    .trim()
    .replace(/[^a-zA-Z0-9-_]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .toLowerCase();

  if (safeName && safeName !== 'untitled-workflow') {
    return safeName;
  }

  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  return `${fallbackPrefix}-${stamp}`;
}

function safeMcpProfileName(value) {
  const safe = String(value || '')
    .trim()
    .replace(/[^a-zA-Z0-9_.-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80);
  if (!safe) {
    throw new Error('MCP profile name is required');
  }
  return safe;
}

function safeWorkspaceId(value, fallbackPrefix = 'ws') {
  const safe = String(value || '')
    .trim()
    .replace(/[^a-zA-Z0-9_.-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80);
  return safe || `${fallbackPrefix}-${Date.now().toString(36)}`;
}

function normalizeWorkspaceRef(value) {
  const normalized = path.posix.normalize(String(value || '').trim().replace(/^\/+/, ''));
  if (!normalized || normalized === '.' || normalized.startsWith('../') || normalized === '..' || normalized.includes('/../')) {
    throw new Error('Workspace reference must stay inside the workspaces directory');
  }
  return normalized
    .split('/')
    .map((part) => safeWorkspaceId(part, DEFAULT_WORKSPACE_ID))
    .join('/');
}

function isWindowsDrivePath(value) {
  return /^[a-zA-Z]:[\\/]/.test(String(value || '').trim());
}

function rejectUnsafeWorkspaceInput(value) {
  const text = String(value || '').trim();
  if (!text) return;
  if (isWindowsDrivePath(text)) {
    throw new Error('Windows drive paths cannot be used as service-side workspace paths');
  }
  if (text.includes('\\')) {
    throw new Error('Workspace paths must use POSIX-style separators on this service');
  }
}

function workspaceIdFromDir(workspaceDir) {
  const resolved = path.resolve(workspaceDir);
  const relative = path.relative(WORKSPACES_DIR, resolved);
  if (relative && !relative.startsWith('..') && !path.isAbsolute(relative)) {
    return safeWorkspaceId(relative, DEFAULT_WORKSPACE_ID);
  }
  if (resolved === LEGACY_WORKSPACE_DIR) {
    return 'legacy';
  }
  return safeWorkspaceId(path.basename(resolved), DEFAULT_WORKSPACE_ID);
}

function resolveWorkspaceDirInput(input = '') {
  const raw = String(input || '').trim();
  rejectUnsafeWorkspaceInput(raw);
  if (!raw) {
    return DEFAULT_WORKSPACE_DIR;
  }

  if (!path.isAbsolute(raw)) {
    return path.join(WORKSPACES_DIR, raw.includes('/') ? normalizeWorkspaceRef(raw) : safeWorkspaceId(raw, DEFAULT_WORKSPACE_ID));
  }

  const resolved = path.resolve(raw);
  if (resolved === PROJECT_ROOT) {
    throw new Error('Project root cannot be used as a workspace directory');
  }
  return resolved;
}

async function readJsonFile(filePath, fallback = null) {
  try {
    return JSON.parse(await fs.readFile(filePath, 'utf-8'));
  } catch (error) {
    if (error.code === 'ENOENT') {
      return fallback;
    }
    throw error;
  }
}

function workspaceManifestPath(workspaceDir) {
  return path.join(workspaceDir, 'workspace.json');
}

async function writeWorkspaceManifest(workspaceDir, manifest) {
  const now = new Date().toISOString();
  const next = {
    ...manifest,
    updated_at: now,
    manifest_version: Number(manifest.manifest_version || 0) + 1,
  };
  await writeJsonAtomic(workspaceManifestPath(workspaceDir), next);
  return next;
}

async function ensureWorkspaceManifest(workspaceDir, options = {}) {
  const workspaceId = safeWorkspaceId(options.workspaceId || workspaceIdFromDir(workspaceDir), DEFAULT_WORKSPACE_ID);
  const manifestPath = workspaceManifestPath(workspaceDir);
  const existing = await readJsonFile(manifestPath, null);
  if (existing) {
    return existing;
  }

  const now = new Date().toISOString();
  const manifest = {
    schema: 'maze_workspace',
    schema_version: 1,
    manifest_version: 1,
    workspace_id: workspaceId,
    name: String(options.name || (workspaceId === DEFAULT_WORKSPACE_ID ? 'Default workspace' : 'Untitled workspace')),
    created_at: now,
    updated_at: now,
    mode: String(options.mode || 'session'),
    default_sandbox: 'workspace_sandbox',
    files_dir: 'files',
    workflows_dir: 'workflows',
    tasks_dir: 'tasks',
    skills_dir: 'skills',
    runs_dir: 'runs',
    policy_path: 'policies/sandbox_policy.json',
    imports: [],
    local_mounts: [],
  };
  await writeJsonAtomic(manifestPath, manifest);
  return manifest;
}

async function updateWorkspaceManifest(workspaceDir, updater) {
  const current = await ensureWorkspaceManifest(workspaceDir);
  const draft = {
    ...current,
    imports: Array.isArray(current.imports) ? [...current.imports] : [],
    local_mounts: Array.isArray(current.local_mounts) ? [...current.local_mounts] : [],
  };
  const updated = await updater(draft) || draft;
  return writeWorkspaceManifest(workspaceDir, updated);
}

async function recordWorkspaceImport(workspaceDir, entry) {
  return updateWorkspaceManifest(workspaceDir, (manifest) => {
    manifest.imports.push({
      ...entry,
      imported_at: new Date().toISOString(),
    });
    return manifest;
  });
}

async function touchWorkspace(workspaceDir) {
  return updateWorkspaceManifest(workspaceDir, (manifest) => manifest);
}

async function ensureWorkspacePolicy(workspaceDir) {
  const policyPath = path.join(workspaceDir, 'policies', 'sandbox_policy.json');
  if (!await fileExists(policyPath)) {
    await writeJsonAtomic(policyPath, {
      schema: 'maze_sandbox_policy',
      schema_version: 1,
      permission: {
        read: {
          '*': 'allow',
          '.env': 'deny',
          '.env.*': 'deny',
          '*secret*': 'deny',
          '*credential*': 'deny',
          '*token*': 'deny',
          'api_key*': 'deny',
          'mcp_profiles/*': 'deny',
        },
        write: {
          '*': 'ask',
          '.env': 'deny',
          '.env.*': 'deny',
          '*secret*': 'deny',
          '*credential*': 'deny',
          '*token*': 'deny',
          'api_key*': 'deny',
          'mcp_profiles/*': 'deny',
        },
        exec_code: { '*': 'ask', 'python *': 'allow', 'rm *': 'deny' },
        mcp: { '*': 'ask' },
        skill: { '*': 'allow' },
      },
    });
  }
}

function workspacePolicyPath(workspaceDir) {
  return path.join(workspaceDir, 'policies', 'sandbox_policy.json');
}

async function ensureWorkspaceDirs(workspaceDir) {
  const resolved = resolveWorkspaceDirInput(workspaceDir);
  await fs.mkdir(resolved, { recursive: true });
  await fs.mkdir(path.join(resolved, 'tasks'), { recursive: true });
  await fs.mkdir(path.join(resolved, 'workflows'), { recursive: true });
  await fs.mkdir(path.join(resolved, 'files'), { recursive: true });
  await fs.mkdir(path.join(resolved, 'skills'), { recursive: true });
  await fs.mkdir(path.join(resolved, 'mcp_profiles'), { recursive: true });
  await fs.mkdir(path.join(resolved, 'cluster_workers'), { recursive: true });
  await fs.mkdir(path.join(resolved, 'agent_sessions'), { recursive: true });
  await fs.mkdir(path.join(resolved, 'agent_drafts'), { recursive: true });
  await fs.mkdir(path.join(resolved, 'agent_runs'), { recursive: true });
  await fs.mkdir(path.join(resolved, 'policies'), { recursive: true });
  await fs.mkdir(path.join(resolved, 'runs'), { recursive: true });
  await ensureWorkspacePolicy(resolved);
  await ensureWorkspaceManifest(resolved);
  await recoverInterruptedStaticRuns(resolved);
  return resolved;
}

async function resolveWorkspaceContext(input = {}) {
  const requestedWorkspaceId = input.workspaceId || input.workspace_id || '';
  const requestedWorkspaceDir = input.workspaceDir || input.workspace_dir || '';
  const workspaceInput = requestedWorkspaceId
    ? safeWorkspaceId(requestedWorkspaceId, DEFAULT_WORKSPACE_ID)
    : (requestedWorkspaceDir || DEFAULT_WORKSPACE_DIR);
  const workspaceDir = await ensureWorkspaceDirs(workspaceInput);
  const manifest = await ensureWorkspaceManifest(workspaceDir, requestedWorkspaceId
    ? { workspaceId: safeWorkspaceId(requestedWorkspaceId, DEFAULT_WORKSPACE_ID) }
    : {});

  return {
    workspaceId: manifest.workspace_id,
    workspaceDir,
    manifest,
    workspaceManifestVersion: Number(manifest.manifest_version || 1),
  };
}

function workspaceResponseFields(context) {
  return {
    workspaceId: context.workspaceId,
    workspaceDir: context.workspaceDir,
    workspaceManifestVersion: context.workspaceManifestVersion,
  };
}

async function recordWorkspaceMutation(workspaceDir, type, detail = {}) {
  return updateWorkspaceManifest(workspaceDir, (manifest) => {
    manifest.last_change = {
      type,
      ...detail,
      at: new Date().toISOString(),
    };
    return manifest;
  });
}

async function createWorkspace({ workspaceId, name, mode } = {}) {
  const finalWorkspaceId = safeWorkspaceId(workspaceId || `ws-${Date.now().toString(36)}`, 'ws');
  const workspaceDir = await ensureWorkspaceDirs(path.join(WORKSPACES_DIR, finalWorkspaceId));
  let manifest = await ensureWorkspaceManifest(workspaceDir, {
    workspaceId: finalWorkspaceId,
    name,
    mode,
  });
  if (name || mode) {
    manifest = await updateWorkspaceManifest(workspaceDir, (draft) => {
      if (name) draft.name = String(name);
      if (mode) draft.mode = String(mode);
      return draft;
    });
  }
  return { workspaceId: manifest.workspace_id, workspaceDir, manifest };
}

async function ensureSystemCatalogDirs() {
  for (const name of ['workflows', 'tasks', 'skills']) {
    await fs.mkdir(path.join(SYSTEM_CATALOG_DIR, name), { recursive: true });
  }
}

function normalizeWorkflowRelativePath(relativePath, workflowName) {
  let normalized = String(relativePath || '').trim().replace(/\\/g, '/').replace(/^\/+/, '');

  if (!normalized) {
    normalized = `workflows/${safeFileName(workflowName)}.json`;
  } else if (!normalized.startsWith('workflows/')) {
    normalized = `workflows/${normalized}`;
  }

  normalized = path.posix.normalize(normalized);

  if (!normalized.startsWith('workflows/') || normalized.includes('/../') || normalized.startsWith('../')) {
    throw new Error('Workflow path must stay inside the workspace workflows directory');
  }

  if (!normalized.endsWith('.json')) {
    normalized = `${normalized}.json`;
  }

  return normalized;
}

function resolveWorkflowFile(workspaceDir, relativePath, workflowName) {
  const normalized = normalizeWorkflowRelativePath(relativePath, workflowName);
  const workflowsDir = path.resolve(workspaceDir, 'workflows');
  const fullPath = path.resolve(workspaceDir, normalized);

  if (!fullPath.startsWith(workflowsDir + path.sep)) {
    throw new Error('Workflow path must stay inside the workspace workflows directory');
  }

  return { relativePath: normalized, fullPath, workflowsDir };
}

async function listWorkflowFiles(dir) {
  const entries = await fs.readdir(dir, { withFileTypes: true }).catch(() => []);
  const files = [];

  for (const entry of entries) {
    if (entry.name.startsWith('.')) {
      continue;
    }
    const entryPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...await listWorkflowFiles(entryPath));
    } else if (entry.isFile() && entry.name.endsWith('.json')) {
      files.push(entryPath);
    }
  }

  return files;
}

function parseMarkdownFrontmatter(raw) {
  const text = String(raw || '');
  if (!text.startsWith('---\n')) {
    return {};
  }
  const end = text.indexOf('\n---', 4);
  if (end < 0) {
    return {};
  }
  const metadata = {};
  const frontmatter = text.slice(4, end).split(/\r?\n/);
  for (const line of frontmatter) {
    const match = line.match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
    if (!match) {
      continue;
    }
    const key = match[1];
    const value = match[2].trim();
    if (!value) {
      metadata[key] = '';
    } else if (value.startsWith('[') && value.endsWith(']')) {
      metadata[key] = value
        .slice(1, -1)
        .split(',')
        .map((item) => item.trim().replace(/^['"]|['"]$/g, ''))
        .filter(Boolean);
    } else {
      metadata[key] = value.replace(/^['"]|['"]$/g, '');
    }
  }
  return metadata;
}

async function catalogItemMetadata(type, fullPath, entry) {
  if (type === 'skills' && entry.isDirectory()) {
    const skillPath = path.join(fullPath, 'SKILL.md');
    const raw = await fs.readFile(skillPath, 'utf-8').catch(() => '');
    const frontmatter = parseMarkdownFrontmatter(raw);
    return {
      description: frontmatter.description || '',
      tags: Array.isArray(frontmatter.tags) ? frontmatter.tags : [],
    };
  }

  if (type === 'workflows' && entry.isFile() && entry.name.endsWith('.json')) {
    try {
      const payload = JSON.parse(await fs.readFile(fullPath, 'utf-8'));
      const workflow = payload?.workflow || payload || {};
      const recommendedSkills = workflow.recommendedSkills || payload.recommendedSkills || workflow.skills || [];
      return {
        description: workflow.description || payload.description || '',
        tags: Array.isArray(workflow.tags || payload.tags) ? (workflow.tags || payload.tags) : [],
        recommendedSkills: Array.isArray(recommendedSkills) ? recommendedSkills.map(String) : [],
      };
    } catch {
      return {};
    }
  }

  return {};
}

function normalizeWorkflowPayload(payload) {
  const workflow = payload?.workflow || payload;
  const nodes = workflow?.nodes;
  const edges = workflow?.edges;
  const rawIncludedTasks =
    payload?.includedTasks ||
    workflow?.includedTasks ||
    workflow?.taskDefinitions ||
    payload?.taskDefinitions ||
    [];

  if (!Array.isArray(nodes) || !Array.isArray(edges)) {
    throw new Error('Invalid workflow file: nodes and edges are required');
  }

  return {
    name: workflow?.name || 'Imported Workflow',
    nodes: nodes.map((node) => ({
      ...node,
      type: 'taskNode',
    })),
    edges: edges.map((edge) => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      sourceHandle: edge.sourceHandle || undefined,
      targetHandle: edge.targetHandle || undefined,
    })),
    includedTasks: Array.isArray(rawIncludedTasks) ? rawIncludedTasks : [],
  };
}

async function fileExists(filePath) {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

function statusForFileError(error) {
  const explicitStatus = Number(error?.status);
  if (Number.isInteger(explicitStatus) && explicitStatus >= 400 && explicitStatus <= 599) {
    return explicitStatus;
  }
  return error?.code === 'ENOENT' ? 404 : 500;
}

function normalizeTaskRelativePath(relativePath) {
  let normalized = String(relativePath || '').trim().replace(/\\/g, '/').replace(/^\/+/, '');

  if (!normalized) {
    throw new Error('Task definition needs a relativePath');
  }
  if (!normalized.startsWith('tasks/')) {
    normalized = `tasks/${normalized}`;
  }

  normalized = path.posix.normalize(normalized);

  if (!normalized.startsWith('tasks/') || normalized.includes('/../') || normalized.startsWith('../')) {
    throw new Error('Task path must stay inside the workspace tasks directory');
  }
  if (!normalized.endsWith('.py')) {
    normalized = `${normalized}.py`;
  }

  return normalized;
}

function resolveTaskDefinitionFile(workspaceDir, relativePath) {
  const normalized = normalizeTaskRelativePath(relativePath);
  const tasksDir = path.resolve(workspaceDir, 'tasks');
  const fullPath = path.resolve(workspaceDir, normalized);

  if (!fullPath.startsWith(tasksDir + path.sep)) {
    throw new Error('Task path must stay inside the workspace tasks directory');
  }

  return { relativePath: normalized, fullPath };
}

function normalizeWorkspaceFileRelativePath(relativePath = '') {
  let normalized = String(relativePath || '').trim().replace(/\\/g, '/').replace(/^\/+/, '');
  normalized = path.posix.normalize(normalized);
  if (normalized === '.') {
    normalized = '';
  }
  if (normalized.startsWith('../') || normalized === '..' || normalized.includes('/../')) {
    throw new Error('Workspace file path must stay inside workspace/files');
  }
  return normalized;
}

function assertAgentFileReadAllowed(relativePath) {
  const normalized = String(relativePath || '').replace(/\\/g, '/');
  const parts = normalized.split('/').filter(Boolean);
  const denied = parts.some((part) => {
    const lower = part.toLowerCase();
    return (
      lower === '.env' ||
      lower.startsWith('.env.') ||
      lower.includes('secret') ||
      lower.includes('credential') ||
      lower.includes('password') ||
      lower.includes('token') ||
      lower.includes('api_key') ||
      lower.includes('apikey')
    );
  });
  if (denied) {
    const error = new Error('Workspace Agent cannot read secret, token, credential, password, api key, or .env files');
    error.status = 403;
    error.code = 'AGENT_FILE_READ_DENIED';
    throw error;
  }
}

function normalizeLocalWorkspaceId(value = '') {
  return String(value || '')
    .trim()
    .replace(/[^a-zA-Z0-9_.:-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 120) || 'default';
}

function resolveWorkspaceFilePath(workspaceDir, relativePath = '') {
  const normalized = normalizeWorkspaceFileRelativePath(relativePath);
  const filesDir = path.resolve(workspaceDir, 'files');
  const fullPath = path.resolve(filesDir, normalized);

  if (fullPath !== filesDir && !fullPath.startsWith(filesDir + path.sep)) {
    throw new Error('Workspace file path must stay inside workspace/files');
  }

  return { relativePath: normalized, fullPath, filesDir };
}

async function describeWorkspaceFile(filesDir, fullPath) {
  const stat = await fs.stat(fullPath);
  const relativePath = toPosixPath(path.relative(filesDir, fullPath));
  return {
    name: path.basename(fullPath),
    relativePath,
    type: stat.isDirectory() ? 'directory' : 'file',
    size: stat.isFile() ? stat.size : null,
    updatedAt: stat.mtime.toISOString(),
  };
}

async function readWorkspaceTaskCode(workspaceDir, relativePath) {
  if (!relativePath) {
    return '';
  }

  try {
    const { fullPath } = resolveTaskDefinitionFile(workspaceDir, relativePath);
    return await fs.readFile(fullPath, 'utf-8');
  } catch {
    return '';
  }
}

function hashTaskCode(code) {
  return crypto.createHash('sha256').update(String(code || ''), 'utf8').digest('hex');
}

function safePythonIdentifier(name, fallback = 'generated_task') {
  let value = String(name || '')
    .trim()
    .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
    .replace(/[^a-zA-Z0-9_]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .toLowerCase();

  if (!value) {
    value = fallback;
  }
  if (!/^[a-zA-Z_]/.test(value)) {
    value = `task_${value}`;
  }

  const pythonKeywords = new Set([
    'false', 'none', 'true', 'and', 'as', 'assert', 'async', 'await', 'break', 'class',
    'continue', 'def', 'del', 'elif', 'else', 'except', 'finally', 'for', 'from',
    'global', 'if', 'import', 'in', 'is', 'lambda', 'nonlocal', 'not', 'or', 'pass',
    'raise', 'return', 'try', 'while', 'with', 'yield',
  ]);

  if (pythonKeywords.has(value)) {
    value = `${value}_task`;
  }

  return value;
}

function normalizeOpenAIBaseUrl(baseUrl) {
  const trimmed = String(baseUrl || process.env.MARBLE_API_URL || '').trim().replace(/\/+$/, '');
  if (!trimmed) {
    throw new Error('Base URL is required');
  }

  const endpoint = `${trimmed}/chat/completions`;
  const parsed = new URL(endpoint);
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error('Base URL must use http or https');
  }
  return endpoint;
}

function getOpenAICompatibleApiKeys(apiKey) {
  const explicitKey = String(apiKey || '').trim();
  if (explicitKey) {
    return [explicitKey];
  }

  return String(process.env.MARBLE_API_KEYS || '')
    .split(',')
    .map((key) => key.trim())
    .filter(Boolean);
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 90000) {
  const controller = new AbortController();
  const externalSignal = options.signal;
  let timedOut = false;
  const abortFromExternal = () => controller.abort();
  if (externalSignal?.aborted) {
    controller.abort();
  } else if (externalSignal) {
    externalSignal.addEventListener('abort', abortFromExternal, { once: true });
  }
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  try {
    return await fetch(url, {
      ...options,
      signal: controller.signal,
    });
  } catch (error) {
    if (error?.name === 'AbortError') {
      const reason = timedOut && !externalSignal?.aborted
        ? `LLM request timed out after ${Math.round(Number(timeoutMs || 0) / 1000)}s`
        : 'LLM request was canceled';
      const abortError = new Error(reason);
      abortError.code = timedOut && !externalSignal?.aborted ? 'LLM_TIMEOUT' : 'LLM_ABORTED';
      abortError.status = timedOut && !externalSignal?.aborted ? 504 : 499;
      throw abortError;
    }
    throw error;
  } finally {
    clearTimeout(timer);
    if (externalSignal) {
      externalSignal.removeEventListener('abort', abortFromExternal);
    }
  }
}

async function callOpenAICompatibleChat({
  baseUrl,
  apiKey,
  model,
  messages,
  temperature = 0.2,
  maxTokens = 2048,
  signal,
}) {
  const explicitApiKey = String(apiKey || '').trim();
  const apiKeys = getOpenAICompatibleApiKeys(apiKey);
  if (apiKeys.length === 0) {
    throw new Error('API key is required');
  }
  if (!model || !String(model).trim()) {
    throw new Error('Model is required');
  }

  const endpoint = normalizeOpenAIBaseUrl(baseUrl);
  let lastError = null;

  for (const key of apiKeys) {
    const response = await fetchWithTimeout(endpoint, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${key}`,
      },
      signal,
      body: JSON.stringify({
        model,
        messages,
        temperature,
        max_tokens: maxTokens,
        stream: false,
      }),
    });

    const text = await response.text();
    let payload = {};
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = { error: text };
      }
    }

    if (!response.ok) {
      const message =
        payload?.error?.message ||
        payload?.message ||
        payload?.detail ||
        (typeof payload?.error === 'string' ? payload.error : '') ||
        `LLM request failed: ${response.status}`;
      const error = new Error(message);
      error.status = response.status;
      error.payload = payload;
      lastError = error;

      if (!explicitApiKey && [401, 403, 429, 500, 502, 503, 504].includes(response.status)) {
        continue;
      }
      throw error;
    }

    const choice = payload?.choices?.[0] || {};
    const content = choice?.message?.content || choice?.delta?.content || '';
    const reasoningContent = choice?.message?.reasoning_content || choice?.delta?.reasoning_content || '';

    return {
      payload,
      content: String(content || ''),
      reasoningContent: String(reasoningContent || ''),
    };
  }

  throw lastError || new Error('LLM request failed');
}

function extractFencedBlock(content, language) {
  const escapedLanguage = String(language || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const pattern = new RegExp(`\`\`\`${escapedLanguage}\\s*([\\s\\S]*?)\`\`\``, 'i');
  const match = String(content || '').match(pattern);
  return match?.[1]?.trim() || '';
}

function tryParseJsonFromText(content) {
  const text = String(content || '').trim();
  const jsonFence = extractFencedBlock(text, 'json');
  const candidates = [
    text,
    jsonFence,
  ].filter(Boolean);

  const firstBrace = text.indexOf('{');
  const lastBrace = text.lastIndexOf('}');
  if (firstBrace !== -1 && lastBrace > firstBrace) {
    candidates.push(text.slice(firstBrace, lastBrace + 1));
  }

  for (const candidate of candidates) {
    try {
      return JSON.parse(candidate);
    } catch {
      // Try the next candidate.
    }
  }

  return null;
}

function extractGeneratedTask(content, requestedTaskName, requestedRelativePath) {
  const parsed = tryParseJsonFromText(content) || {};
  const pythonFence = extractFencedBlock(content, 'python') || extractFencedBlock(content, 'py');
  const code = String(parsed.code || pythonFence || content || '').trim();
  const functionName = safePythonIdentifier(
    parsed.function_name || parsed.functionName || parsed.name || requestedTaskName,
  );

  let relativePath;
  try {
    relativePath = normalizeTaskRelativePath(
      parsed.relative_path || parsed.relativePath || requestedRelativePath || `tasks/ai_generated/${functionName}.py`,
    );
  } catch {
    relativePath = `tasks/ai_generated/${functionName}.py`;
  }

  const warnings = [];
  if (!code.includes('@task')) {
    warnings.push('Generated code does not appear to contain a @task decorator.');
  }
  if (!code.includes('from maze import task')) {
    warnings.push('Generated code does not explicitly import task from maze.');
  }
  if (!code.includes('return {')) {
    warnings.push('Generated code should return a dict.');
  }

  return {
    functionName,
    relativePath,
    code,
    notes: parsed.notes || parsed.explanation || '',
    warnings,
  };
}

function summarizeTaskContext(taskContext = []) {
  if (!Array.isArray(taskContext)) {
    return [];
  }

  return taskContext.slice(0, 12).map((task) => ({
    node_id: task?.nodeId || task?.node_id || '',
    label: task?.label || task?.name || '',
    category: task?.category || '',
    function_name: task?.functionName || task?.function_name || '',
    task_ref: task?.taskRef || '',
    relative_path: task?.relativePath || task?.relative_path || '',
    description: String(task?.description || '').slice(0, 500),
    inputs: Array.isArray(task?.inputs)
      ? task.inputs.map((input) => ({
          name: input?.name || '',
          type: input?.dataType || input?.type || 'Any',
          source: input?.source || undefined,
          from_task: input?.taskSource || undefined,
        }))
      : [],
    outputs: Array.isArray(task?.outputs)
      ? task.outputs.map((output) => ({
          name: output?.name || '',
          type: output?.dataType || output?.type || 'Any',
        }))
      : [],
    code_preview: String(task?.codePreview || '').slice(0, 1200),
  }));
}

function formatTaskContext(taskContext = []) {
  const summarized = summarizeTaskContext(taskContext);
  if (summarized.length === 0) {
    return 'No existing workflow tasks were provided.';
  }

  return JSON.stringify(summarized, null, 2);
}

function buildTaskGenerationMessages({ description, taskName, relativePath, taskContext = [] }) {
  const functionName = safePythonIdentifier(taskName || 'generated_task');
  const suggestedPath = relativePath || `tasks/ai_generated/${functionName}.py`;
  const exampleCode = [
    'from pathlib import Path',
    'from maze import task',
    '',
    '@task(resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 0})',
    'def summarize_text(input_path: str = "input.txt", output_path: str = "reports/summary.txt"):',
    '    # Path(".") is the task sandbox root containing staged workspace files.',
    '    text = Path(input_path).read_text(encoding="utf-8")',
    '    summary = text[:200]',
    '    Path(output_path).parent.mkdir(parents=True, exist_ok=True)',
    '    Path(output_path).write_text(summary, encoding="utf-8")',
    '    return {"summary": summary, "summary_path": output_path}',
  ].join('\n');

  return [
    {
      role: 'system',
      content: [
        'You write Maze Playground workspace task files.',
        'Return JSON only. Do not wrap the JSON in Markdown.',
        'The JSON shape must be: {"function_name": "...", "relative_path": "tasks/ai_generated/name.py", "code": "...", "notes": "..."}',
        'Example valid JSON response:',
        JSON.stringify({
          function_name: 'summarize_text',
          relative_path: 'tasks/ai_generated/summarize_text.py',
          code: exampleCode,
          notes: 'Reads a staged workspace file, writes a report artifact, and returns JSON-safe values.',
        }, null, 2),
        'The code must be Python for exactly one Maze task.',
        'Use: from maze import task',
        'Use one @task(resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 0}) decorator.',
        'Use normal Python function parameters with safe defaults when useful.',
        'The task must return a dict.',
        'Tasks execute in a sandbox working directory. Path(".") / cwd is the logical files root for this task.',
        'Important: cwd will print as a run sandbox path, not the physical workspace/files directory. That is expected.',
        'The contents of workspace/files and direct parent artifacts are staged into cwd before execution.',
        'Do not prefix paths with "workspace/files/". Use "input.csv", "folder/data.json", or "reports/output.json" relative to Path(".").',
        'Read and write files with relative paths using pathlib.Path.',
        'Do not use absolute paths, parent directory traversal, home directories, environment secrets, subprocess, shell, package installation, or network calls.',
        'If the task creates files, include their relative paths in the returned dict.',
        'When existing workflow tasks are provided, match parameter names/types to upstream output names/types whenever that makes the new task easier to wire into the workflow.',
      ].join('\n'),
    },
    {
      role: 'user',
      content: [
        `Task description: ${description}`,
        `Preferred function name: ${functionName}`,
        `Preferred relative path: ${suggestedPath}`,
        '',
        'Existing workflow task definitions:',
        formatTaskContext(taskContext),
      ].join('\n'),
    },
  ];
}

function taskDefinitionKey(relativePath, functionName = '') {
  return `${normalizeTaskRelativePath(relativePath)}::${String(functionName || '')}`;
}

function nowEpochSeconds() {
  return Date.now() / 1000;
}

function staticRunsDir(workspaceDir) {
  return path.join(workspaceDir, 'runs');
}

function legacyStaticRunsDirs(workspaceDir) {
  return [
    path.join(workspaceDir, 'workflow_runs', 'static'),
    path.join(workspaceDir, 'workflow_runs', 'static_runs'),
  ];
}

function staticRunSearchDirs(workspaceDir) {
  return [staticRunsDir(workspaceDir), ...legacyStaticRunsDirs(workspaceDir)];
}

function staticRunDir(workspaceDir, runId, options = {}) {
  if (!runId || String(runId).includes('/') || String(runId).includes('\\')) {
    throw new Error(`Invalid workflow run id: ${runId}`);
  }
  if (options.write) {
    return path.join(staticRunsDir(workspaceDir), runId);
  }
  for (const runsDir of staticRunSearchDirs(workspaceDir)) {
    const candidate = path.join(runsDir, runId);
    if (fsSync.existsSync(path.join(candidate, 'run.json'))) {
      return candidate;
    }
  }
  return path.join(staticRunsDir(workspaceDir), runId);
}

function staticRunPath(workspaceDir, runId, options = {}) {
  return path.join(staticRunDir(workspaceDir, runId, options), 'run.json');
}

function staticRunEventsPath(workspaceDir, runId, options = {}) {
  return path.join(staticRunDir(workspaceDir, runId, options), 'events.jsonl');
}

function taskNodeSnapshotFromWorkflowNode(node) {
  const data = node?.data || {};
  return {
    node_id: node.id,
    task_name: data.functionName || data.label || node.id,
    label: data.label || data.functionName || node.id,
    category: data.category,
    status: 'pending',
    created_time: null,
    started_time: null,
    finished_time: null,
    result_summary: null,
    error: null,
    file_manifest: null,
    artifacts: [],
    node_ip: null,
    node_id_runtime: null,
    gpu_id: null,
  };
}

function createStaticRunSnapshot({ runId, workflow, workspaceDir, workspaceContext = null }) {
  const now = nowEpochSeconds();
  const nodes = workflow.nodes || [];
  const edges = workflow.edges || [];
  const taskNodes = Object.fromEntries(
    nodes.map((node) => [node.id, taskNodeSnapshotFromWorkflowNode(node)])
  );

  return {
    schema: 'static_workflow_run',
    schema_version: 1,
    kind: 'static',
    run_id: runId,
    workflow_id: workflow.id,
    workflow_name: workflow.name || 'Untitled Workflow',
    workspace_dir: workspaceDir,
    workspace_id: workspaceContext?.workspaceId || workspaceIdFromDir(workspaceDir),
    workspace_manifest_version: workspaceContext?.workspaceManifestVersion || null,
    status: 'running',
    created_time: now,
    updated_time: now,
    finished_time: null,
    task_counts: {
      total: nodes.length,
      pending: nodes.length,
      running: 0,
      completed: 0,
      failed: 0,
    },
    task_nodes: taskNodes,
    graph: {
      nodes: nodes.map((node) => node.id),
      edges: edges.map((edge) => ({
        source: edge.source,
        target: edge.target,
      })),
    },
    events: {
      count: 0,
      last_seq: 0,
    },
    final_result: null,
    error: null,
    maze_run_id: null,
    metadata: {
      workspace_id: workspaceContext?.workspaceId || workspaceIdFromDir(workspaceDir),
      workspace_dir: workspaceDir,
      workspace_manifest_version: workspaceContext?.workspaceManifestVersion || null,
    },
  };
}

function stableJsonValue(value) {
  if (Array.isArray(value)) {
    return value.map(stableJsonValue);
  }
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, stableJsonValue(value[key])]),
    );
  }
  return value;
}

function sha256Text(value) {
  return crypto.createHash('sha256').update(String(value), 'utf-8').digest('hex');
}

function gaiaSubmissionFingerprint({
  workflow,
  sampleRef,
  workspaceId,
  mazeWorkflowId,
  timeoutSeconds,
  inputs,
  finalOutputRefs,
  executionFile,
}) {
  return sha256Text(JSON.stringify(stableJsonValue({
    workflow,
    sample_ref: sampleRef,
    playground_workspace_id: workspaceId,
    maze_workflow_id: mazeWorkflowId,
    timeout_seconds: timeoutSeconds,
    inputs,
    final_output_refs: finalOutputRefs,
    execution_file: executionFile ? {
      name: executionFile.name,
      sha256: executionFile.sha256,
      size: executionFile.content.length,
    } : null,
  })));
}

function normalizeGaiaSubmissionToken(value) {
  const token = String(value || '');
  if (!GAIA_SUBMISSION_TOKEN_PATTERN.test(token)) {
    const error = new Error('submissionToken must be 64 lowercase hexadecimal characters');
    error.status = 400;
    throw error;
  }
  return token;
}

function validateGaiaExecutionFile(workflow, rawFile) {
  if (workflow !== 'file') {
    if (rawFile !== undefined && rawFile !== null) {
      const error = new Error('executionFile is only valid for the file workflow');
      error.status = 400;
      throw error;
    }
    return null;
  }
  if (!rawFile || typeof rawFile !== 'object' || Array.isArray(rawFile)) {
    const error = new Error('file workflow requires executionFile');
    error.status = 400;
    throw error;
  }

  const name = String(rawFile.name || '').trim();
  if (
    !name
    || name === '.'
    || name === '..'
    || name.includes('/')
    || name.includes('\\')
    || name.includes('\0')
    || Buffer.byteLength(name, 'utf-8') > 255
  ) {
    const error = new Error('executionFile.name must be a single safe file name');
    error.status = 400;
    throw error;
  }
  const extension = path.extname(name).toLowerCase();
  if (!GAIA_FILE_EXTENSIONS.has(extension)) {
    const error = new Error('executionFile must be a .txt, .md, or .pdf file');
    error.status = 400;
    throw error;
  }

  const contentBase64 = rawFile.contentBase64 ?? rawFile.content_base64;
  if (
    typeof contentBase64 !== 'string'
    || !contentBase64
    || contentBase64.length % 4 !== 0
    || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(contentBase64)
  ) {
    const error = new Error('executionFile.contentBase64 must be strict base64');
    error.status = 400;
    throw error;
  }
  const content = Buffer.from(contentBase64, 'base64');
  if (content.toString('base64') !== contentBase64) {
    const error = new Error('executionFile.contentBase64 is not canonical base64');
    error.status = 400;
    throw error;
  }
  if (content.length > GAIA_MAX_FILE_BYTES) {
    const error = new Error(`executionFile exceeds the ${GAIA_MAX_FILE_BYTES} byte limit`);
    error.status = 413;
    throw error;
  }

  const expectedSha256 = String(rawFile.sha256 || '');
  if (!/^[0-9a-f]{64}$/.test(expectedSha256)) {
    const error = new Error('executionFile.sha256 must be a lowercase SHA-256 digest');
    error.status = 400;
    throw error;
  }
  const actualSha256 = crypto.createHash('sha256').update(content).digest('hex');
  if (actualSha256 !== expectedSha256) {
    const error = new Error('executionFile.sha256 does not match its content');
    error.status = 400;
    throw error;
  }
  return { name, sha256: actualSha256, content };
}

function pathIsInside(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative === '' || (relative && !relative.startsWith('..') && !path.isAbsolute(relative));
}

async function lstatOrNull(filePath) {
  return fs.lstat(filePath).catch((error) => {
    if (error.code === 'ENOENT') return null;
    throw error;
  });
}

function gaiaPathError(message) {
  const error = new Error(message);
  error.status = 400;
  error.code = 'GAIA_PATH_UNSAFE';
  return error;
}

async function requireRealDirectoryWithin(root, candidate, label) {
  if (!pathIsInside(root, candidate)) {
    throw gaiaPathError(`${label} escaped its managed root`);
  }
  const stat = await lstatOrNull(candidate);
  if (!stat?.isDirectory() || stat.isSymbolicLink()) {
    throw gaiaPathError(`${label} must be a real directory`);
  }
  const [canonicalRoot, canonicalCandidate] = await Promise.all([
    fs.realpath(root),
    fs.realpath(candidate),
  ]);
  if (!pathIsInside(canonicalRoot, canonicalCandidate)) {
    throw gaiaPathError(`${label} escaped its managed root`);
  }
  return canonicalCandidate;
}

async function ensureRealDirectoryWithin(root, candidate, label, mode = 0o700) {
  if (!pathIsInside(root, candidate)) {
    throw gaiaPathError(`${label} escaped its managed root`);
  }
  try {
    await fs.mkdir(candidate, { mode });
  } catch (error) {
    if (error.code !== 'EEXIST') throw error;
  }
  const canonical = await requireRealDirectoryWithin(root, candidate, label);
  await fs.chmod(canonical, mode);
  return canonical;
}

async function rejectSymlinkIfPresent(filePath, label) {
  const stat = await lstatOrNull(filePath);
  if (stat?.isSymbolicLink()) {
    throw gaiaPathError(`${label} cannot be a symbolic link`);
  }
}

async function requireManagedGaiaWorkspace(context) {
  const managedRoot = await fs.realpath(WORKSPACES_DIR);
  const workspaceStat = await fs.lstat(context.workspaceDir);
  if (workspaceStat.isSymbolicLink()) {
    const error = new Error('GAIA workspace cannot be a symbolic link');
    error.status = 400;
    throw error;
  }
  const workspaceDir = await fs.realpath(context.workspaceDir);
  if (!pathIsInside(managedRoot, workspaceDir)) {
    const error = new Error('GAIA workspace must stay inside the managed workspaces directory');
    error.status = 400;
    throw error;
  }
  return workspaceDir;
}

async function ensureManagedGaiaWorkspaceContext(workspaceIdInput) {
  const requestedWorkspaceId = String(workspaceIdInput || DEFAULT_WORKSPACE_ID).trim();
  const workspaceId = safeWorkspaceId(requestedWorkspaceId || DEFAULT_WORKSPACE_ID, DEFAULT_WORKSPACE_ID);
  await fs.mkdir(WORKSPACES_DIR, { recursive: true });
  const managedRoot = await fs.realpath(WORKSPACES_DIR);
  const workspaceDir = await ensureRealDirectoryWithin(
    managedRoot,
    path.join(managedRoot, workspaceId),
    'GAIA workspace',
  );
  const directoryNames = [
    'tasks',
    'workflows',
    'files',
    'skills',
    'mcp_profiles',
    'cluster_workers',
    'agent_sessions',
    'agent_drafts',
    'agent_runs',
    'policies',
    'runs',
  ];
  for (const name of directoryNames) {
    await ensureRealDirectoryWithin(
      workspaceDir,
      path.join(workspaceDir, name),
      `GAIA workspace ${name} directory`,
    );
  }
  const legacyRunsRoot = path.join(workspaceDir, 'workflow_runs');
  if (await lstatOrNull(legacyRunsRoot)) {
    const canonicalLegacyRoot = await requireRealDirectoryWithin(
      workspaceDir,
      legacyRunsRoot,
      'GAIA legacy runs directory',
    );
    for (const legacyDir of legacyStaticRunsDirs(workspaceDir)) {
      if (await lstatOrNull(legacyDir)) {
        await requireRealDirectoryWithin(
          canonicalLegacyRoot,
          legacyDir,
          'GAIA legacy static runs directory',
        );
      }
    }
  }
  await rejectSymlinkIfPresent(workspaceManifestPath(workspaceDir), 'GAIA workspace manifest');
  await rejectSymlinkIfPresent(workspacePolicyPath(workspaceDir), 'GAIA workspace policy');
  await ensureWorkspacePolicy(workspaceDir);
  const manifest = await ensureWorkspaceManifest(workspaceDir, { workspaceId });
  await rejectSymlinkIfPresent(workspaceManifestPath(workspaceDir), 'GAIA workspace manifest');
  await rejectSymlinkIfPresent(workspacePolicyPath(workspaceDir), 'GAIA workspace policy');
  await recoverInterruptedStaticRuns(workspaceDir);
  return {
    workspaceId: manifest.workspace_id,
    workspaceDir,
    manifest,
    workspaceManifestVersion: Number(manifest.manifest_version || 1),
  };
}

async function requirePrivateGaiaStagingRoot() {
  const existing = await lstatOrNull(GAIA_STAGING_ROOT);
  if (existing?.isSymbolicLink()) {
    throw gaiaPathError('GAIA private staging root cannot be a symbolic link');
  }
  await fs.mkdir(GAIA_STAGING_ROOT, { recursive: true, mode: 0o700 });
  const stat = await fs.lstat(GAIA_STAGING_ROOT);
  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    throw gaiaPathError('GAIA private staging root must be a real directory');
  }
  await fs.chmod(GAIA_STAGING_ROOT, 0o700);
  return fs.realpath(GAIA_STAGING_ROOT);
}

function gaiaStagingPrefix(workspaceDir, runId) {
  return `${sha256Text(`${path.resolve(workspaceDir)}::${runId}`)}-`;
}

async function clearGaiaInputStaging(stagingRoot) {
  const stagingStat = await lstatOrNull(stagingRoot);
  if (!stagingStat) return;
  if (!stagingStat.isDirectory() || stagingStat.isSymbolicLink()) {
    throw gaiaPathError('Refusing to remove unsafe GAIA input staging');
  }
  const privateRoot = await requirePrivateGaiaStagingRoot();
  const canonicalStagingRoot = await fs.realpath(stagingRoot);
  if (
    canonicalStagingRoot === privateRoot
    || !pathIsInside(privateRoot, canonicalStagingRoot)
  ) {
    throw gaiaPathError('Refusing to remove GAIA input staging outside its private root');
  }
  await fs.rm(canonicalStagingRoot, { recursive: true, force: true });
}

async function ensureGaiaRunDirectory(workspaceDir, runId) {
  const runsDir = await requireRealDirectoryWithin(
    workspaceDir,
    staticRunsDir(workspaceDir),
    'GAIA runs directory',
  );
  return ensureRealDirectoryWithin(
    runsDir,
    staticRunDir(workspaceDir, runId, { write: true }),
    'GAIA run directory',
  );
}

async function cleanupRecoveredGaiaStaging(workspaceDir, runId) {
  const activeKey = `${path.resolve(workspaceDir)}::${runId}`;
  if (activeGaiaSubmissions.has(activeKey)) return;
  const privateRoot = await requirePrivateGaiaStagingRoot();
  const prefix = gaiaStagingPrefix(workspaceDir, runId);
  const entries = await fs.readdir(privateRoot, { withFileTypes: true });
  for (const entry of entries) {
    if (!entry.name.startsWith(prefix)) continue;
    await clearGaiaInputStaging(path.join(privateRoot, entry.name));
  }
}

async function stageGaiaExecutionFile(context, runId, executionFile) {
  const workspaceDir = await requireManagedGaiaWorkspace(context);
  const privateRoot = await requirePrivateGaiaStagingRoot();
  const stagingRoot = await fs.mkdtemp(
    path.join(privateRoot, gaiaStagingPrefix(workspaceDir, runId)),
  );
  await fs.chmod(stagingRoot, 0o700);
  const filesDir = path.join(stagingRoot, 'files');

  try {
    const canonicalStagingRoot = await requireRealDirectoryWithin(
      privateRoot,
      stagingRoot,
      'GAIA private staging directory',
    );
    const canonicalFilesDir = await ensureRealDirectoryWithin(
      canonicalStagingRoot,
      filesDir,
      'GAIA staging files directory',
    );
    if (executionFile) {
      const filePath = path.join(canonicalFilesDir, executionFile.name);
      const flags = (
        fsSync.constants.O_WRONLY
        | fsSync.constants.O_CREAT
        | fsSync.constants.O_EXCL
        | (fsSync.constants.O_NOFOLLOW || 0)
      );
      const fileHandle = await fs.open(filePath, flags, 0o600);
      try {
        await fileHandle.writeFile(executionFile.content);
        await fileHandle.chmod(0o600);
      } finally {
        await fileHandle.close();
      }
    }
  } catch (error) {
    await clearGaiaInputStaging(stagingRoot).catch(() => {});
    throw error;
  }

  return {
    workspaceDir: stagingRoot,
    async clearInput() {
      await clearGaiaInputStaging(stagingRoot);
    },
  };
}

function isGaiaTrace(snapshot) {
  return snapshot?.metadata?.benchmark === 'gaia';
}

function publicGaiaMetadata(snapshot) {
  return {
    benchmark: 'gaia',
    workflow: snapshot?.metadata?.workflow,
    sample_ref: snapshot?.metadata?.sample_ref,
    playground_run_id: snapshot?.run_id || snapshot?.metadata?.playground_run_id,
  };
}

function publicStaticRunSnapshot(snapshot) {
  if (!isGaiaTrace(snapshot)) return snapshot;
  return {
    schema: snapshot.schema || 'static_workflow_run',
    schema_version: snapshot.schema_version || 1,
    kind: 'static',
    ...(snapshot.summary ? { summary: true } : {}),
    run_id: snapshot.run_id,
    workflow_id: snapshot.workflow_id,
    workflow_name: snapshot.workflow_name,
    workspace_id: snapshot.workspace_id,
    workspace_manifest_version: snapshot.workspace_manifest_version,
    status: snapshot.status,
    created_time: snapshot.created_time,
    updated_time: snapshot.updated_time,
    finished_time: snapshot.finished_time,
    task_counts: snapshot.task_counts || {},
    task_nodes: snapshot.summary ? undefined : {},
    graph: snapshot.summary ? undefined : { nodes: [], edges: [] },
    events: snapshot.events || { count: 0, last_seq: 0 },
    final_result: null,
    error: null,
    metadata: publicGaiaMetadata(snapshot),
  };
}

function publicStaticRunWorkspaceFields(context, snapshot) {
  if (!isGaiaTrace(snapshot)) return workspaceResponseFields(context);
  return {
    workspaceId: context.workspaceId,
    workspaceManifestVersion: context.workspaceManifestVersion,
  };
}

function publicStaticRunEvent(snapshot, event) {
  if (!isGaiaTrace(snapshot)) return event;
  return {
    type: event.type,
    schema_version: event.schema_version,
    seq: event.seq,
    timestamp: event.timestamp,
    data: {
      workflow_run_id: snapshot.run_id,
    },
  };
}

function createGaiaTraceSnapshot({
  runId,
  workflow,
  sampleRef,
  context,
  mazeWorkflowId,
  submissionTokenHash,
  idempotencyKey,
  submissionFingerprint,
}) {
  const snapshot = createStaticRunSnapshot({
    runId,
    workflow: {
      id: `benchmark:gaia:${workflow}`,
      name: `GAIA ${workflow}`,
      nodes: [],
      edges: [],
    },
    workspaceDir: context.workspaceDir,
    workspaceContext: context,
  });
  snapshot.metadata = {
    benchmark: 'gaia',
    workflow,
    sample_ref: sampleRef,
    playground_run_id: runId,
    maze_run_id: null,
  };
  snapshot.gaia_private = {
    submission_token_sha256: submissionTokenHash,
    maze_workflow_id: mazeWorkflowId,
    idempotency_key: idempotencyKey,
    submission_fingerprint: submissionFingerprint,
    submission_state: 'prepared',
  };
  return snapshot;
}

function requireGaiaTraceRun(snapshot) {
  if (snapshot?.metadata?.benchmark !== 'gaia') {
    const error = new Error('Playground run is not a GAIA benchmark run');
    error.status = 400;
    throw error;
  }
  return snapshot;
}

function requireGaiaSubmissionToken(snapshot, token) {
  const actual = sha256Text(normalizeGaiaSubmissionToken(token));
  const expected = String(snapshot?.gaia_private?.submission_token_sha256 || '');
  if (!expected || actual !== expected) {
    const error = new Error('GAIA submission capability is invalid');
    error.status = 403;
    throw error;
  }
}

function gaiaTraceResponse(snapshot, { includeMazeRunId = false } = {}) {
  const response = {
    playgroundRunId: snapshot.run_id,
    status: snapshot.status,
    trace: publicGaiaMetadata(snapshot),
  };
  if (includeMazeRunId) {
    response.mazeRunId = snapshot.maze_run_id || null;
  }
  return response;
}

function gaiaTerminalForCoreStatus(status) {
  const normalized = String(status || '').trim().toLowerCase();
  if (normalized === 'canceled') return GAIA_TERMINAL_EVENTS.cancelled;
  return GAIA_TERMINAL_EVENTS[normalized] || null;
}

function requireMappedCoreGaiaRun(snapshot, coreRun) {
  if (
    !snapshot.maze_run_id
    || String(coreRun?.run_id || '') !== String(snapshot.maze_run_id)
    || !coreGaiaRunMatchesTrace(coreRun, snapshot)
  ) {
    const error = new Error('Maze run binding does not match the GAIA Playground trace');
    error.status = 409;
    throw error;
  }
  return coreRun;
}

function coreGaiaRunMatchesTrace(coreRun, snapshot) {
  const metadata = coreRun?.metadata || {};
  const privateBinding = snapshot?.gaia_private || {};
  return (
    metadata.benchmark === 'gaia'
    && metadata.playground_run_id === snapshot.run_id
    && metadata.sample_ref === snapshot.metadata?.sample_ref
    && metadata.workflow === snapshot.metadata?.workflow
    && String(coreRun?.workflow_id || '') === String(privateBinding.maze_workflow_id || '')
    && String(coreRun?.idempotency_key || '') === String(privateBinding.idempotency_key || '')
    && String(coreRun?.idempotency_fingerprint || '') === String(privateBinding.submission_fingerprint || '')
  );
}

function coreRunFromDetailPayload(payload) {
  const coreRun = payload?.run;
  if (!coreRun || typeof coreRun !== 'object' || Array.isArray(coreRun)) {
    const error = new Error('Maze Core returned a malformed run response');
    error.status = 502;
    throw error;
  }
  return coreRun;
}

async function loadCoreRun(runId) {
  const payload = await callMazeCore(`/runs/${encodeURIComponent(runId)}`);
  return coreRunFromDetailPayload(payload);
}

async function listCoreStaticRuns() {
  const payload = await callMazeCore('/runs?kind=static&detail=true');
  if (!Array.isArray(payload?.runs)) {
    const error = new Error('Maze Core returned a malformed static run list');
    error.status = 502;
    throw error;
  }
  return payload.runs;
}

async function findGaiaTraceBySampleRef(workspaceDir, sampleRef) {
  const matches = (await listStaticRunFilesForWorkspace(workspaceDir)).filter((run) => (
    isGaiaTrace(run) && run.metadata?.sample_ref === sampleRef
  ));
  if (matches.length > 1) {
    const error = new Error('Multiple Playground traces use this GAIA sample reference');
    error.status = 409;
    throw error;
  }
  return matches[0] || null;
}

async function interruptGaiaTraceUnlocked(workspaceDir, runId, reason) {
  const { snapshot } = await appendAndApplyStaticRunEventUnlocked(workspaceDir, runId, {
    type: GAIA_TERMINAL_EVENTS.interrupted.type,
    data: { reason },
    timestamp: new Date().toISOString(),
  });
  return snapshot;
}

async function reconcileGaiaTraceUnlocked(
  workspaceDir,
  runId,
  coreRuns,
  { markMissing = false } = {},
) {
  let snapshot = requireGaiaTraceRun(await loadStaticRun(workspaceDir, runId));
  if (TERMINAL_STATIC_RUN_STATUSES.has(snapshot.status)) {
    await saveStaticRun(workspaceDir, snapshot);
    return snapshot;
  }

  let coreRun = null;
  if (snapshot.maze_run_id) {
    const byId = coreRuns.filter((candidate) => (
      String(candidate?.run_id || '') === String(snapshot.maze_run_id)
    ));
    if (byId.length === 1 && coreGaiaRunMatchesTrace(byId[0], snapshot)) {
      coreRun = byId[0];
    } else if (markMissing) {
      return interruptGaiaTraceUnlocked(
        workspaceDir,
        runId,
        byId.length ? 'core_mapping_mismatch' : 'core_mapping_missing',
      );
    } else {
      const error = new Error('Maze run mapping does not match the GAIA Playground trace');
      error.status = 409;
      throw error;
    }
  } else {
    const matches = coreRuns.filter((candidate) => coreGaiaRunMatchesTrace(candidate, snapshot));
    if (matches.length === 1) {
      coreRun = matches[0];
      const mazeRunId = String(coreRun.run_id || '').trim();
      if (!mazeRunId) {
        const error = new Error('Maze Core run is missing its run id');
        error.status = 502;
        throw error;
      }
      ({ snapshot } = await appendAndApplyStaticRunEventUnlocked(workspaceDir, runId, {
        type: 'maze_run_created',
        data: { maze_run_id: mazeRunId },
        timestamp: new Date().toISOString(),
      }));
    } else if (matches.length > 1) {
      if (markMissing) {
        return interruptGaiaTraceUnlocked(workspaceDir, runId, 'duplicate_core_metadata_match');
      }
      const error = new Error('Multiple Maze runs match the GAIA Playground trace');
      error.status = 409;
      throw error;
    } else if (markMissing) {
      return interruptGaiaTraceUnlocked(workspaceDir, runId, 'core_run_missing_after_restart');
    } else {
      await saveStaticRun(workspaceDir, snapshot);
      return snapshot;
    }
  }

  const normalizedStatus = String(coreRun?.status || '').trim().toLowerCase();
  const terminal = gaiaTerminalForCoreStatus(normalizedStatus);
  if (terminal) {
    ({ snapshot } = await appendAndApplyStaticRunEventUnlocked(workspaceDir, runId, {
      type: terminal.type,
      data: {},
      timestamp: new Date().toISOString(),
    }));
    return snapshot;
  }
  if (!['created', 'running'].includes(normalizedStatus)) {
    const error = new Error(`Maze Core returned an unknown static run status: ${normalizedStatus || '<empty>'}`);
    error.status = 502;
    throw error;
  }
  await saveStaticRun(workspaceDir, snapshot);
  return snapshot;
}

async function ensureGaiaTraceMappingUnlocked(workspaceDir, runId) {
  let snapshot = requireGaiaTraceRun(await loadStaticRun(workspaceDir, runId));
  if (!snapshot.maze_run_id && !TERMINAL_STATIC_RUN_STATUSES.has(snapshot.status)) {
    snapshot = await reconcileGaiaTraceUnlocked(
      workspaceDir,
      runId,
      await listCoreStaticRuns(),
      { markMissing: false },
    );
  }
  if (!snapshot.maze_run_id) {
    const error = new Error('Playground run has no verified Maze run mapping');
    error.status = 409;
    throw error;
  }
  return snapshot;
}

function gaiaLocalStatusMatchesCoreTerminal(snapshot, terminal) {
  return (
    snapshot.status === terminal?.status
    || (snapshot.status === 'timed_out' && terminal?.status === 'canceled')
  );
}

function rejectGaiaWorkspacePathFields(body, { includeExecutionWorkspace = false } = {}) {
  const forbidden = [
    'playgroundWorkspaceDir',
    'playground_workspace_dir',
    'workspaceDir',
    'workspace_dir',
  ];
  if (includeExecutionWorkspace) {
    forbidden.push('executionWorkspaceDir', 'execution_workspace_dir');
  }
  if (forbidden.some((field) => Object.prototype.hasOwnProperty.call(body, field))) {
    const error = new Error('GAIA requests accept a managed playgroundWorkspaceId, not a workspace path');
    error.status = 400;
    throw error;
  }
}

async function resolveGaiaWorkspaceContext(body, options = {}) {
  rejectGaiaWorkspacePathFields(body, options);
  return ensureManagedGaiaWorkspaceContext(
    body.playgroundWorkspaceId || body.playground_workspace_id,
  );
}

function gaiaCoreIdempotencyKey(submissionToken) {
  return `gaia-${sha256Text(submissionToken)}`;
}

function redactGaiaRunIdentifiers(value, gaiaRunIds, pseudonyms = new Map()) {
  if (typeof value === 'string' && gaiaRunIds.has(value)) {
    if (!pseudonyms.has(value)) {
      pseudonyms.set(value, `gaia-${sha256Text(value).slice(0, 32)}`);
    }
    return pseudonyms.get(value);
  }
  if (Array.isArray(value)) {
    return value.map((item) => redactGaiaRunIdentifiers(item, gaiaRunIds, pseudonyms));
  }
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        redactGaiaRunIdentifiers(item, gaiaRunIds, pseudonyms),
      ]),
    );
  }
  return value;
}

function requireGaiaSubmissionReceipt(receipt, snapshot) {
  const mazeRunId = String(receipt?.run_id || '').trim();
  const privateBinding = snapshot.gaia_private || {};
  if (
    !mazeRunId
    || receipt?.idempotency_key !== privateBinding.idempotency_key
    || receipt?.idempotency_fingerprint !== privateBinding.submission_fingerprint
  ) {
    const error = new Error('Maze Core returned a malformed idempotent submission receipt');
    error.status = 502;
    error.code = 'MAZE_CORE_MALFORMED_RECEIPT';
    throw error;
  }
  return mazeRunId;
}

async function bindGaiaCoreRun(workspaceDir, playgroundRunId, coreRun) {
  return withStaticRunWriteQueue(workspaceDir, playgroundRunId, async () => {
    let snapshot = requireGaiaTraceRun(await loadStaticRun(workspaceDir, playgroundRunId));
    if (snapshot.maze_run_id) {
      return requireMappedCoreGaiaRun(snapshot, coreRun) && snapshot;
    }
    if (!coreGaiaRunMatchesTrace(coreRun, snapshot)) {
      const error = new Error('Maze run binding does not match the GAIA Playground trace');
      error.status = 409;
      throw error;
    }
    ({ snapshot } = await appendAndApplyStaticRunEventUnlocked(
      workspaceDir,
      playgroundRunId,
      {
        type: 'maze_run_created',
        data: { maze_run_id: String(coreRun.run_id) },
        timestamp: new Date().toISOString(),
      },
    ));
    return snapshot;
  });
}

async function submitGaiaTraceToCore({
  context,
  snapshot,
  inputs,
  finalOutputRefs,
  timeoutSeconds,
  executionFile,
}) {
  const playgroundRunId = snapshot.run_id;
  const activeKey = `${path.resolve(context.workspaceDir)}::${playgroundRunId}`;
  activeGaiaSubmissions.add(activeKey);
  let stagedExecution = null;
  let submittedCoreRun = null;
  let submissionError = null;
  let cleanupError = null;

  try {
    ({ snapshot } = await appendAndApplyStaticRunEvent(
      context.workspaceDir,
      playgroundRunId,
      {
        type: 'benchmark_submission_started',
        data: {},
        timestamp: new Date().toISOString(),
      },
    ));
    stagedExecution = await stageGaiaExecutionFile(context, playgroundRunId, executionFile);
    const corePayload = {
      workflow_id: snapshot.gaia_private.maze_workflow_id,
      inputs,
      final_output_refs: finalOutputRefs,
      timeout_seconds: timeoutSeconds,
      metadata: snapshot.metadata,
      idempotency_key: snapshot.gaia_private.idempotency_key,
      idempotency_fingerprint: snapshot.gaia_private.submission_fingerprint,
    };
    if (stagedExecution) {
      corePayload.file_context = {
        enabled: true,
        private: true,
        workspace_dir: stagedExecution.workspaceDir,
        artifact_store: {
          type: 'head_http',
          base_url: MAZE_CORE_URL,
          private: true,
        },
      };
    }

    const receipt = await callMazeCore('/run_workflow', {
      method: 'POST',
      body: corePayload,
    });
    const mazeRunId = requireGaiaSubmissionReceipt(receipt, snapshot);
    submittedCoreRun = requireMappedCoreGaiaRun(
      { ...snapshot, maze_run_id: mazeRunId },
      await loadCoreRun(mazeRunId),
    );
  } catch (error) {
    submissionError = error;
  } finally {
    if (stagedExecution) {
      try {
        await stagedExecution.clearInput();
      } catch (error) {
        cleanupError = error;
      }
    }
    activeGaiaSubmissions.delete(activeKey);
  }

  if (!submittedCoreRun) {
    let reconciled = null;
    let listSucceeded = false;
    try {
      const coreRuns = await listCoreStaticRuns();
      listSucceeded = true;
      reconciled = await withStaticRunWriteQueue(
        context.workspaceDir,
        playgroundRunId,
        () => reconcileGaiaTraceUnlocked(
          context.workspaceDir,
          playgroundRunId,
          coreRuns,
          { markMissing: false },
        ),
      );
    } catch (error) {
      console.error(`Maze submission reconciliation remains retryable for ${playgroundRunId}:`, error.message);
    }
    if (reconciled?.maze_run_id) {
      if (cleanupError) throw cleanupError;
      return { snapshot: reconciled, recovered: true };
    }

    const locallyTimedOut = ['MAZE_CORE_TIMEOUT', 'MAZE_CORE_ABORTED'].includes(
      submissionError?.code,
    );
    const localPathFailure = submissionError?.code === 'GAIA_PATH_UNSAFE';
    if (listSucceeded && submissionError?.status && !locallyTimedOut) {
      await appendAndApplyStaticRunEvent(context.workspaceDir, playgroundRunId, {
        type: 'benchmark_submission_failed',
        data: {},
        timestamp: new Date().toISOString(),
      }).catch(() => {});
    }
    console.error(`Maze submission failed for retained Playground run ${playgroundRunId}`);
    const error = new Error(
      localPathFailure
        ? 'GAIA staging path is unsafe'
        : (
            submissionError?.status === 409
              ? 'Maze workflow submission conflicts with an existing idempotency binding'
              : 'Maze workflow submission failed'
          ),
    );
    error.status = localPathFailure
      ? 400
      : (
          submissionError?.status === 409
            ? 409
            : (locallyTimedOut ? 504 : 502)
        );
    error.playgroundRunId = playgroundRunId;
    error.mazeRunId = null;
    throw error;
  }

  snapshot = await bindGaiaCoreRun(context.workspaceDir, playgroundRunId, submittedCoreRun);
  if (cleanupError) {
    const error = new Error('GAIA input staging cleanup failed');
    error.status = 500;
    error.playgroundRunId = playgroundRunId;
    error.mazeRunId = snapshot.maze_run_id;
    throw error;
  }
  return { snapshot, recovered: false };
}

function recomputeStaticRunTaskCounts(snapshot) {
  const counts = {
    total: 0,
    pending: 0,
    running: 0,
    completed: 0,
    failed: 0,
  };

  Object.values(snapshot.task_nodes || {}).forEach((node) => {
    counts.total += 1;
    const status = node.status || 'pending';
    if (counts[status] !== undefined) {
      counts[status] += 1;
    }
  });

  snapshot.task_counts = counts;
}

async function writeJsonAtomic(filePath, payload, options = {}) {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  const tmpPath = `${filePath}.${process.pid}.${Date.now()}.${Math.random().toString(16).slice(2)}.tmp`;
  await fs.writeFile(
    tmpPath,
    `${JSON.stringify(payload, null, 2)}\n`,
    options.mode
      ? { encoding: 'utf-8', mode: options.mode }
      : 'utf-8',
  );
  if (options.mode) {
    await fs.chmod(tmpPath, options.mode);
  }
  await fs.rename(tmpPath, filePath);
}

async function writeTextAtomic(filePath, content) {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  const tmpPath = `${filePath}.${process.pid}.${Date.now()}.${Math.random().toString(16).slice(2)}.tmp`;
  await fs.writeFile(tmpPath, content, 'utf-8');
  await fs.rename(tmpPath, filePath);
}

function mcpProfilesDir(workspaceDir) {
  return path.join(workspaceDir, 'mcp_profiles');
}

function mcpProfilePath(workspaceDir, name) {
  return path.join(mcpProfilesDir(workspaceDir), `${safeMcpProfileName(name)}.json`);
}

function workerProfilesDir(workspaceDir) {
  return path.join(workspaceDir, 'cluster_workers');
}

function workerProfilesPath(workspaceDir) {
  return path.join(workerProfilesDir(workspaceDir), 'worker_profiles.json');
}

function safeWorkerProfileId(value, fallbackPrefix = 'worker') {
  return safeWorkspaceId(value, fallbackPrefix);
}

function hasActiveWorkerPassword(workspaceDir, profileId) {
  const entry = activeWorkerProfileSecrets.get(workerSecretKey(workspaceDir, profileId));
  if (!entry) return false;
  if (Date.now() > entry.expiresAt) {
    activeWorkerProfileSecrets.delete(workerSecretKey(workspaceDir, profileId));
    return false;
  }
  return Boolean(entry.password);
}

function redactedWorkerProfile(profile, workspaceDir = '') {
  const auth = profile.auth || {};
  return {
    ...profile,
    auth: {
      method: auth.method || 'password',
      hasPassword: Boolean(workspaceDir && hasActiveWorkerPassword(workspaceDir, profile.id)),
      hasPrivateKey: Boolean(auth.privateKeyPath),
      privateKeyPath: auth.privateKeyPath || '',
    },
  };
}

function sanitizeWorkerProfileInput(input = {}, existing = null) {
  const now = new Date().toISOString();
  const id = safeWorkerProfileId(input.id || existing?.id || input.name || input.host);
  const host = String(input.host || existing?.host || '').trim();
  if (!host) {
    throw new Error('worker host is required');
  }
  const port = Number(input.port || existing?.port || 22);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error('worker ssh port must be between 1 and 65535');
  }
  const username = String(input.username || existing?.username || 'root').trim() || 'root';
  const authMethod = String(input.auth?.method || input.authMethod || existing?.auth?.method || 'password');
  const privateKeyPath = String(input.auth?.privateKeyPath || input.privateKeyPath || existing?.auth?.privateKeyPath || '').trim();
  const remoteProjectDir = String(input.remoteProjectDir || existing?.remoteProjectDir || PROJECT_ROOT).trim();
  const condaEnv = String(input.condaEnv || existing?.condaEnv || 'maze').trim() || 'maze';
  const condaSh = String(input.condaSh || existing?.condaSh || '/root/miniconda3/etc/profile.d/conda.sh').trim();
  const headUrl = String(input.headUrl || existing?.headUrl || MAZE_CORE_URL).trim();
  const heartbeatInterval = Number(input.heartbeatInterval || existing?.heartbeatInterval || 10);
  const logDir = String(input.logDir || existing?.logDir || path.posix.join(remoteProjectDir, 'logs')).trim();
  const hasIncomingPassword = Boolean(input.auth?.password || input.password);
  return {
    id,
    name: String(input.name || existing?.name || id).trim() || id,
    host,
    port,
    username,
    remoteProjectDir,
    condaEnv,
    condaSh,
    headUrl,
    heartbeatInterval: Number.isFinite(heartbeatInterval) ? Math.max(1, heartbeatInterval) : 10,
    logDir,
    auth: {
      method: authMethod === 'key' ? 'key' : 'password',
      privateKeyPath,
    },
    createdAt: existing?.createdAt || now,
    updatedAt: now,
    lastAction: existing?.lastAction || null,
  };
}

async function loadWorkerProfiles(workspaceDir) {
  const payload = await readJsonFile(workerProfilesPath(workspaceDir), {
    schema: 'maze_worker_profiles',
    schema_version: 1,
    profiles: [],
  });
  return {
    schema: payload.schema || 'maze_worker_profiles',
    schema_version: Number(payload.schema_version || 1),
    profiles: Array.isArray(payload.profiles) ? payload.profiles : [],
  };
}

async function saveWorkerProfiles(workspaceDir, profiles) {
  await fs.mkdir(workerProfilesDir(workspaceDir), { recursive: true });
  await writeJsonAtomic(workerProfilesPath(workspaceDir), {
    schema: 'maze_worker_profiles',
    schema_version: 1,
    updatedAt: new Date().toISOString(),
    profiles,
  });
}

function workerSecretKey(workspaceDir, profileId) {
  return `${path.resolve(workspaceDir)}:${profileId}`;
}

function rememberWorkerPassword(workspaceDir, profileId, password) {
  if (!password) return;
  activeWorkerProfileSecrets.set(workerSecretKey(workspaceDir, profileId), {
    password: String(password),
    expiresAt: Date.now() + 6 * 60 * 60 * 1000,
  });
}

function getWorkerPassword(workspaceDir, profileId, incomingPassword = '') {
  if (incomingPassword) return String(incomingPassword);
  const entry = activeWorkerProfileSecrets.get(workerSecretKey(workspaceDir, profileId));
  if (!entry) return '';
  if (Date.now() > entry.expiresAt) {
    activeWorkerProfileSecrets.delete(workerSecretKey(workspaceDir, profileId));
    return '';
  }
  return entry.password;
}

function sshTarget(profile) {
  return `${profile.username}@${profile.host}`;
}

function shellQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

function headAddrFromUrl(url, fallbackHost = '') {
  const parsed = new URL(url || MAZE_CORE_URL);
  const hostname = ['127.0.0.1', 'localhost', '::1'].includes(parsed.hostname) && fallbackHost
    ? fallbackHost
    : parsed.hostname;
  return `${hostname}:${parsed.port || (parsed.protocol === 'https:' ? '443' : '80')}`;
}

function mazeRuntimePath() {
  const candidates = [];
  if (process.env.MAZE_CONDA_PREFIX) {
    candidates.push(path.join(process.env.MAZE_CONDA_PREFIX, 'bin'));
  }
  candidates.push(path.dirname(PYTHON_BIN));
  return candidates.filter(Boolean).join(':');
}

function limitCommandResult(result, maxOutputChars = 60000) {
  const limitText = (value) => {
    const text = String(value || '');
    if (text.length <= maxOutputChars) return text;
    return `${text.slice(0, maxOutputChars)}\n... output truncated ...\n`;
  };
  return {
    ...result,
    stdout: limitText(result.stdout),
    stderr: limitText(result.stderr),
  };
}

function remoteWorkerCommand(profile, action) {
  const projectDir = profile.remoteProjectDir;
  const logDir = profile.logDir || path.posix.join(projectDir, 'logs');
  const pidPath = path.posix.join(logDir, 'maze_worker_remote.pid');
  const quotedCondaSh = shellQuote(profile.condaSh);
  const quotedCondaEnv = shellQuote(profile.condaEnv);
  const quotedLogDir = shellQuote(logDir);
  const quotedPidPath = shellQuote(pidPath);
  const quotedProjectDir = shellQuote(projectDir);
  const base = [
    `source ${quotedCondaSh} 2>/dev/null || true`,
    `conda activate ${quotedCondaEnv} 2>/dev/null || true`,
    `mkdir -p ${quotedLogDir}`,
  ];
  if (action === 'test') {
    return [...base, 'hostname', 'pwd'].join('; ');
  }
  const stopLines = [
    `if [ -f ${quotedPidPath} ]; then kill "$(cat ${quotedPidPath})" 2>/dev/null || true; fi`,
    `pkill -f "[p]ython -m maze.cli.cli start --worker --addr" 2>/dev/null || true`,
    'ray stop --force >/dev/null 2>&1 || true',
    'for i in 1 2 3 4 5; do pgrep -f "[r]aylet|[g]cs_server|[p]lasma_store" >/dev/null || break; sleep 1; done',
  ];
  if (action === 'stop') {
    return [
      ...base,
      ...stopLines,
      'echo stopped',
    ].join('\n');
  }
  if (action === 'logs') {
    return [
      ...base,
      `LOG="$(ls -t ${quotedLogDir}/maze_worker_remote_*.log 2>/dev/null | head -1)"`,
      'if [ -n "$LOG" ]; then echo "LOG=$LOG"; tail -120 "$LOG"; else echo "no worker log"; fi',
    ].join('\n');
  }
  const headAddr = headAddrFromUrl(profile.headUrl || MAZE_CORE_URL, process.env.MAZE_HEAD_HOST || '');
  const workerShell = [
    `source ${shellQuote(profile.condaSh)} 2>/dev/null || true`,
    `conda activate ${shellQuote(profile.condaEnv)} 2>/dev/null || true`,
    `cd ${shellQuote(projectDir)}`,
    'MAZE_WORKER_PY=python',
    `exec "$MAZE_WORKER_PY" -m maze.cli.cli start --worker --addr ${shellQuote(headAddr)} --agent --heartbeat-interval ${Number(profile.heartbeatInterval || 10)} --log-level INFO`,
  ].join('; ');
  return [
    ...base,
    ...stopLines,
    `cd ${quotedProjectDir}`,
    'WORKER_LOG="$PWD/logs/maze_worker_remote_$(date +%Y%m%d_%H%M%S).log"',
    `setsid env PYTHONPATH=${shellQuote(projectDir)} PYTHONUNBUFFERED=1 /bin/bash -c ${shellQuote(workerShell)} > "$WORKER_LOG" 2>&1 < /dev/null &`,
    'echo $! > "$PWD/logs/maze_worker_remote.pid"',
    'printf "REMOTE_WORKER_PID=%s\\nREMOTE_WORKER_LOG=%s\\n" "$(cat "$PWD/logs/maze_worker_remote.pid")" "$WORKER_LOG"',
  ].join('\n');
}

async function runSshCommand(profile, command, options = {}) {
  const timeoutMs = Number(options.timeoutMs || 45000);
  const password = options.password || '';
  const args = [
    '-p',
    String(profile.port),
    '-o',
    'StrictHostKeyChecking=no',
    '-o',
    'UserKnownHostsFile=/root/.ssh/known_hosts',
    '-o',
    'ConnectTimeout=10',
  ];
  const tempFiles = [];
  const sshEnv = {
    ...process.env,
    DISPLAY: process.env.DISPLAY || 'maze-ssh',
  };
  if (profile.auth?.method === 'key') {
    if (!profile.auth.privateKeyPath) {
      throw new Error('privateKeyPath is required for key auth');
    }
    args.push('-i', profile.auth.privateKeyPath);
  } else if (password) {
    const tempDir = await fs.mkdtemp(path.join(tmpdir(), 'maze-ssh-'));
    const askpassPath = path.join(tempDir, 'askpass.sh');
    await fs.writeFile(askpassPath, `#!/usr/bin/env bash\nprintf '%s\\n' ${shellQuote(password)}\n`, { encoding: 'utf-8', mode: 0o700 });
    tempFiles.push(tempDir);
    args.push('-o', 'BatchMode=no', '-o', 'PreferredAuthentications=password,keyboard-interactive');
    sshEnv.SSH_ASKPASS_REQUIRE = 'force';
    sshEnv.SSH_ASKPASS = askpassPath;
  } else if (profile.auth?.method === 'password') {
    throw new Error('password is required for this worker profile in the current backend session');
  }
  args.push(sshTarget(profile), command);

  return new Promise((resolve, reject) => {
    const child = spawn('setsid', ['ssh', ...args], {
      env: sshEnv,
    });
    let stdout = '';
    let stderr = '';
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      reject(new Error(`SSH command timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (data) => { stdout += data; });
    child.stderr.on('data', (data) => { stderr += data; });
    child.on('error', reject);
    child.on('close', async (code) => {
      clearTimeout(timer);
      for (const tempFile of tempFiles) {
        await fs.rm(tempFile, { recursive: true, force: true }).catch(() => {});
      }
      const result = { code, stdout, stderr, ok: code === 0 };
      if (code === 0) {
        resolve(result);
      } else {
        const error = new Error(stderr || stdout || `SSH command failed with code ${code}`);
        error.result = result;
        reject(error);
      }
    });
  });
}

async function runLocalCommand(command, options = {}) {
  const timeoutMs = Number(options.timeoutMs || 30000);
  const cwd = options.cwd || PROJECT_ROOT;
  return new Promise((resolve, reject) => {
    const child = spawn('/bin/bash', ['-lc', command], {
      cwd,
      env: {
        ...process.env,
        PATH: `${mazeRuntimePath()}:${process.env.PATH || ''}`,
        PYTHONPATH: process.env.PYTHONPATH || PROJECT_ROOT,
      },
    });
    let stdout = '';
    let stderr = '';
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      reject(new Error(`Command timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (data) => { stdout += data; });
    child.stderr.on('data', (data) => { stderr += data; });
    child.on('error', reject);
    child.on('close', (code) => {
      clearTimeout(timer);
      const result = { code, stdout, stderr, ok: code === 0 };
      if (code === 0) {
        resolve(result);
      } else {
        const error = new Error(stderr || stdout || `Command failed with code ${code}`);
        error.result = result;
        reject(error);
      }
    });
  });
}

async function runWorkerDraftTest(profile, options = {}) {
  const timeoutMs = Number(options.timeoutMs || 30000);
  const password = options.password || '';
  const checks = [];
  try {
    const ping = await runLocalCommand(`ping -c 1 -W 2 ${shellQuote(profile.host)}`, { timeoutMs: 5000, cwd: PROJECT_ROOT });
    checks.push({ name: 'ping', ok: true, stdout: ping.stdout, stderr: ping.stderr });
  } catch (error) {
    checks.push({
      name: 'ping',
      ok: false,
      stdout: error.result?.stdout || '',
      stderr: error.result?.stderr || error.message,
      warning: true,
    });
  }

  const sshCommand = [
    'echo SSH_OK',
    'hostname',
    'pwd',
    `test -d ${shellQuote(profile.remoteProjectDir)} && echo PROJECT_DIR_OK || echo PROJECT_DIR_MISSING`,
    `test -f ${shellQuote(profile.condaSh)} && echo CONDA_SH_OK || echo CONDA_SH_MISSING`,
  ].join('\n');
  const ssh = await runSshCommand(profile, sshCommand, { password, timeoutMs });
  checks.push({ name: 'ssh', ok: true, stdout: ssh.stdout, stderr: ssh.stderr });
  return {
    ok: checks.every((check) => check.ok || check.warning),
    checks,
    result: limitCommandResult(ssh, 20000),
  };
}

function safeAgentId(value, fallbackPrefix = 'agent') {
  const safe = String(value || '')
    .trim()
    .replace(/[^a-zA-Z0-9_.-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 100);
  return safe || `${fallbackPrefix}-${Date.now().toString(36)}-${crypto.randomBytes(3).toString('hex')}`;
}

function agentSessionsDir(workspaceDir) {
  return path.join(workspaceDir, 'agent_sessions');
}

function agentDraftsDir(workspaceDir) {
  return path.join(workspaceDir, 'agent_drafts');
}

function agentSessionPath(workspaceDir, sessionId) {
  return path.join(agentSessionsDir(workspaceDir), `${safeAgentId(sessionId, 'session')}.json`);
}

function agentDraftPath(workspaceDir, draftId) {
  return path.join(agentDraftsDir(workspaceDir), `${safeAgentId(draftId, 'draft')}.json`);
}

const SECRET_KEY_PATTERN = /(^|[_-])(api[_-]?key|authorization|secret|credential|password|passwd|bearer|access[_-]?token|refresh[_-]?token)([_-]|$)/i;

function redactSecretText(text) {
  return String(text || '')
    .replace(/(^|[^a-zA-Z0-9])sk-[a-zA-Z0-9_-]{8,}/g, '$1<redacted>')
    .replace(/\b(api[_-]?key|authorization|secret|credential|password|passwd|bearer|access[_-]?token|refresh[_-]?token|token)\b\s*[:=]\s*["']?[^"',;\n\r]+/gi, '$1=<redacted>');
}

function redactSecrets(value) {
  if (typeof value === 'string') {
    return redactSecretText(value);
  }
  if (Array.isArray(value)) {
    return value.map((item) => redactSecrets(item));
  }
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [
      key,
      SECRET_KEY_PATTERN.test(key) ? '<redacted>' : redactSecrets(item),
    ]));
  }
  return value;
}

function normalizeAgentTurns(turns = []) {
  return (Array.isArray(turns) ? turns : [])
    .map((turn) => {
      const runId = String(turn?.dynamic_run_id || turn?.dynamicRunId || '').trim();
      return {
        id: safeAgentId(turn?.id || `turn-${runId}`, 'turn'),
        dynamic_run_id: runId,
        created_at: String(turn?.created_at || turn?.createdAt || new Date().toISOString()),
      };
    })
    .filter((turn) => turn.dynamic_run_id);
}

function agentSessionSummary(session) {
  const turns = normalizeAgentTurns(session.turns);
  const legacyMessages = Array.isArray(session.messages) ? session.messages : [];
  return {
    id: session.id,
    title: session.title,
    createdAt: session.createdAt,
    updatedAt: session.updatedAt,
    workspaceId: session.workspaceId,
    workspaceDir: session.workspaceDir,
    messageCount: legacyMessages.length + turns.length * 2,
    turnCount: turns.length,
    dynamicRunId: turns.at(-1)?.dynamic_run_id || null,
    summary: session.summary || '',
    compaction: session.compaction || null,
    metadata: redactSecrets(session.metadata || {}),
  };
}

function collectAgentDraftIdsFromMessages(messages = []) {
  const draftIds = new Set();
  for (const message of Array.isArray(messages) ? messages : []) {
    for (const part of Array.isArray(message?.parts) ? message.parts : []) {
      const result = part?.result || {};
      const draft = result.draft || part?.draft;
      if (draft?.id) {
        draftIds.add(String(draft.id));
      }
    }
  }
  return Array.from(draftIds);
}

async function createAgentSessionRecord(context, input = {}) {
  const now = new Date().toISOString();
  const session = {
    schema: 'maze_workspace_agent_session',
    schema_version: 2,
    id: safeAgentId(input.id, 'session'),
    title: String(input.title || input.message || 'Workspace Agent').slice(0, 80),
    workspaceId: context.workspaceId,
    workspaceDir: context.workspaceDir,
    createdAt: now,
    updatedAt: now,
    metadata: redactSecrets(input.metadata || {}),
    turns: [],
  };
  await writeJsonAtomic(agentSessionPath(context.workspaceDir, session.id), session);
  return session;
}

async function loadAgentSession(workspaceDir, sessionId) {
  const raw = await fs.readFile(agentSessionPath(workspaceDir, sessionId), 'utf-8');
  const session = JSON.parse(raw);
  session.turns = normalizeAgentTurns(session.turns);
  if (session.schema_version === 1) {
    session.messages = Array.isArray(session.messages) ? session.messages : [];
  }
  return session;
}

async function saveAgentSession(workspaceDir, session) {
  session.updatedAt = new Date().toISOString();
  session.turns = normalizeAgentTurns(session.turns);
  if (session.turns.length > 0 || session.schema_version !== 1) {
    session.schema_version = 2;
  }
  await writeJsonAtomic(agentSessionPath(workspaceDir, session.id), redactSecrets(session));
  return session;
}

async function withAgentSessionWriteQueue(workspaceDir, sessionId, operation) {
  const key = agentSessionPath(workspaceDir, sessionId);
  const previous = agentSessionWriteQueues.get(key) || Promise.resolve();
  const current = previous.catch(() => {}).then(operation);
  agentSessionWriteQueues.set(key, current);
  try {
    return await current;
  } finally {
    if (agentSessionWriteQueues.get(key) === current) {
      agentSessionWriteQueues.delete(key);
    }
  }
}

async function appendAgentSessionTurn(workspaceDir, session, dynamicRunId) {
  const runId = String(dynamicRunId || '').trim();
  if (!runId) throw new Error('dynamic_run_id is required');
  return withAgentSessionWriteQueue(workspaceDir, session.id, async () => {
    const storedSession = await loadAgentSession(workspaceDir, session.id);
    let turn = storedSession.turns.find((item) => item.dynamic_run_id === runId);
    if (!turn) {
      turn = {
        id: safeAgentId('', 'turn'),
        dynamic_run_id: runId,
        created_at: new Date().toISOString(),
      };
      storedSession.turns.push(turn);
    }
    await saveAgentSession(workspaceDir, storedSession);
    Object.assign(session, storedSession);
    return turn;
  });
}

async function updateAgentSessionRecord(workspaceDir, sessionId, updates = {}) {
  return withAgentSessionWriteQueue(workspaceDir, sessionId, async () => {
    const session = await loadAgentSession(workspaceDir, sessionId);
    if (Object.prototype.hasOwnProperty.call(updates, 'title')) {
      const title = String(updates.title || '').trim();
      if (!title) {
        const error = new Error('Session title is required');
        error.status = 400;
        throw error;
      }
      session.title = title.slice(0, 80);
    }
    if (updates.metadata && typeof updates.metadata === 'object' && !Array.isArray(updates.metadata)) {
      session.metadata = {
        ...(session.metadata || {}),
        ...redactSecrets(updates.metadata),
      };
    }
    await saveAgentSession(workspaceDir, session);
    return session;
  });
}

async function deleteAgentSessionRecord(workspaceDir, sessionId) {
  return withAgentSessionWriteQueue(workspaceDir, sessionId, async () => {
    await fs.unlink(agentSessionPath(workspaceDir, sessionId));
    return { id: sessionId };
  });
}

function agentDynamicEventData(event) {
  return event?.data && typeof event.data === 'object' ? event.data : {};
}

function agentViewMessage(sessionId, id, role, createdAt, parts) {
  return {
    id,
    sessionId,
    role,
    createdAt: createdAt || new Date().toISOString(),
    parts: redactSecrets(parts || []),
  };
}

function agentMessagesFromDynamicTurn(sessionId, turn, run, events) {
  const runId = turn.dynamic_run_id;
  const messages = [];
  const started = events.find((event) => event.type === 'workspace_agent_turn_started')
    || events.find((event) => event.type === 'agent_run_started');
  const startedData = agentDynamicEventData(started);
  const userText = String(startedData.message || startedData.prompt || '').trim();
  if (userText) {
    messages.push(agentViewMessage(
      sessionId,
      `${runId}:user`,
      'user',
      started?.timestamp || turn.created_at,
      [{ type: 'text', text: userText }],
    ));
  }

  for (const event of events) {
    const data = agentDynamicEventData(event);
    if (event.type === 'agent_action' && data.tool) {
      const toolCallId = `${runId}:tool:${data.step || event.seq || messages.length}`;
      messages.push(agentViewMessage(
        sessionId,
        `${toolCallId}:call`,
        'assistant',
        event.timestamp,
        [{
          type: 'tool_call',
          id: toolCallId,
          name: data.tool,
          input: data.args || {},
        }],
      ));
    }
    if (event.type === 'agent_observation' && data.tool) {
      const toolCallId = `${runId}:tool:${data.step || event.seq || messages.length}`;
      messages.push(agentViewMessage(
        sessionId,
        `${toolCallId}:result`,
        'tool',
        event.timestamp,
        [{
          type: 'tool_result',
          toolCallId,
          name: data.tool,
          result: data.result,
        }],
      ));
    }
  }

  const finalEvent = [...events].reverse().find((event) => event.type === 'agent_final');
  const errorEvent = [...events].reverse().find((event) => event.type === 'agent_error');
  if (finalEvent) {
    messages.push(agentViewMessage(
      sessionId,
      `${runId}:assistant`,
      'assistant',
      finalEvent.timestamp,
      [{ type: 'text', text: String(agentDynamicEventData(finalEvent).answer || 'Done.') }],
    ));
  } else if (errorEvent) {
    messages.push(agentViewMessage(
      sessionId,
      `${runId}:error`,
      'assistant',
      errorEvent.timestamp,
      [{ type: 'error', message: String(agentDynamicEventData(errorEvent).error || 'Workspace Agent failed') }],
    ));
  } else if (['canceled', 'timed_out', 'interrupted', 'failed'].includes(String(run?.status || ''))) {
    const reason = run?.cancel_reason || run?.failure_reason?.message || run?.failure_reason || `Run ${run.status}`;
    messages.push(agentViewMessage(
      sessionId,
      `${runId}:terminal`,
      'assistant',
      new Date(Number(run?.finished_time || run?.updated_time || 0) * 1000).toISOString(),
      [{ type: 'error', message: String(reason) }],
    ));
  }
  return messages;
}

function collectAgentDraftIdsFromEvents(events = []) {
  const draftIds = new Set();
  for (const event of events) {
    const data = agentDynamicEventData(event);
    const candidates = [
      data.draft,
      data.result?.draft,
      data.result?.structured_content?.draft,
      data.result?.structuredContent?.draft,
      data.tool_result?.content?.draft,
      data.tool_result?.content?.structured_content?.draft,
      data.observation?.draft,
      data.observation?.result?.draft,
    ];
    for (const draft of candidates) {
      if (draft?.id) draftIds.add(String(draft.id));
    }
  }
  return Array.from(draftIds);
}

async function loadAgentDynamicTurn(sessionId, turn) {
  try {
    const runId = encodeURIComponent(turn.dynamic_run_id);
    const [runPayload, eventPayload] = await Promise.all([
      callMazeCore(`/dynamic_runs/${runId}`),
      callMazeCore(`/dynamic_runs/${runId}/events`),
    ]);
    const run = runPayload.run || runPayload;
    const events = eventPayload.events || [];
    return {
      turn,
      run,
      events,
      messages: agentMessagesFromDynamicTurn(sessionId, turn, run, events),
      draftIds: collectAgentDraftIdsFromEvents(events),
    };
  } catch (error) {
    return { turn, run: null, events: [], messages: [], draftIds: [], error: error.message };
  }
}

async function loadAgentSessionView(context, session, options = {}) {
  const legacyMessages = Array.isArray(session.messages) ? session.messages.map(redactSecrets) : [];
  const turns = normalizeAgentTurns(options.turns ?? session.turns);
  const dynamicTurns = await Promise.all(turns.map((turn) => loadAgentDynamicTurn(session.id, turn)));
  const messages = [
    ...legacyMessages,
    ...dynamicTurns.flatMap((turn) => turn.messages),
  ].sort((left, right) => String(left.createdAt || '').localeCompare(String(right.createdAt || '')));
  const draftIds = new Set([
    ...collectAgentDraftIdsFromMessages(legacyMessages),
    ...dynamicTurns.flatMap((turn) => turn.draftIds),
  ]);
  const drafts = [];
  if (options.includeDrafts !== false) {
    for (const draftId of draftIds) {
      try {
        drafts.push(agentDraftPublic(await loadAgentDraft(context.workspaceDir, draftId)));
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
      }
    }
  }
  return {
    messages,
    drafts,
    unavailableTurns: dynamicTurns.filter((turn) => turn.error).map((turn) => turn.turn.dynamic_run_id),
  };
}

function agentMessagePromptText(message) {
  const parts = (message.parts || []).map((part) => {
    if (part.type === 'text') return String(part.text || '');
    if (part.type === 'error') return `[error] ${String(part.message || '')}`;
    if (part.type === 'tool_call') return `[tool ${part.name}] ${JSON.stringify(part.input || {})}`;
    if (part.type === 'tool_result') return `[result ${part.name}] ${JSON.stringify(part.result || {})}`;
    return '';
  }).filter(Boolean);
  return parts.length ? `${message.role}: ${parts.join('\n')}` : '';
}

function buildWorkspaceAgentPrompt(message, historyMessages, summary = '', maxChars = 12000) {
  const limit = Math.max(1000, Number(maxChars) || 12000);
  const currentPrefix = 'Current user request:\n';
  const current = currentPrefix + redactSecretText(message).slice(0, limit - currentPrefix.length);
  const summaryPrefix = 'Prior conversation summary:\n';
  const summaryBudget = Math.max(0, Math.min(3000, limit - current.length - summaryPrefix.length - 2));
  const summaryText = summaryBudget
    ? redactSecretText(summary).trim().slice(-summaryBudget)
    : '';
  const summaryBlock = summaryText ? summaryPrefix + summaryText : '';
  const historyPrefix = 'Conversation history:\n';
  const budget = Math.max(
    0,
    limit - current.length - (summaryBlock ? summaryBlock.length + 2 : 0) - historyPrefix.length - 2,
  );
  const selected = [];
  let used = 0;
  for (const item of [...(historyMessages || [])].reverse()) {
    const text = agentMessagePromptText(item);
    if (!text) continue;
    const clipped = text.slice(-Math.min(text.length, 3000));
    if (used + clipped.length + 2 > budget) break;
    selected.unshift(clipped);
    used += clipped.length + 2;
  }
  const historyBlock = selected.length ? historyPrefix + selected.join('\n\n') : '';
  return [summaryBlock, historyBlock, current].filter(Boolean).join('\n\n');
}

async function buildAgentSessionExport(context, sessionId) {
  const session = await loadAgentSession(context.workspaceDir, sessionId);
  const view = await loadAgentSessionView(context, session);
  return redactSecrets({
    schema: 'maze_workspace_agent_session_export',
    schema_version: 2,
    exportedAt: new Date().toISOString(),
    workspaceId: context.workspaceId,
    workspaceManifestVersion: context.workspaceManifestVersion,
    session: agentSessionSummary(session),
    turns: normalizeAgentTurns(session.turns),
    messages: view.messages,
    drafts: view.drafts,
    unavailableTurns: view.unavailableTurns,
    summary: session.summary || '',
    compaction: session.compaction || null,
  });
}

async function listAgentSessions(workspaceDir) {
  const entries = await fs.readdir(agentSessionsDir(workspaceDir), { withFileTypes: true }).catch(() => []);
  const sessions = [];
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith('.json')) continue;
    try {
      const raw = await fs.readFile(path.join(agentSessionsDir(workspaceDir), entry.name), 'utf-8');
      sessions.push(agentSessionSummary(JSON.parse(raw)));
    } catch {
      // Ignore malformed session files in the list view.
    }
  }
  sessions.sort((a, b) => String(b.updatedAt || '').localeCompare(String(a.updatedAt || '')));
  return sessions;
}

function normalizeAgentTaskDefinitions(taskDefinitions = []) {
  if (!Array.isArray(taskDefinitions)) return [];
  return taskDefinitions
    .map((definition) => ({
      type: 'workspace',
      relativePath: normalizeTaskRelativePath(definition.relativePath || definition.taskPath || definition.sourcePath || ''),
      functionName: definition.functionName || definition.name || undefined,
      displayName: definition.displayName || definition.label || definition.functionName || undefined,
      code: String(definition.code || ''),
      inputs: Array.isArray(definition.inputs) ? definition.inputs : [],
      outputs: Array.isArray(definition.outputs) ? definition.outputs : [],
      resources: normalizeTaskResources(definition.resources),
    }))
    .filter((definition) => definition.relativePath && definition.code.trim());
}

function normalizeTaskResources(resources = {}) {
  const raw = resources && typeof resources === 'object' ? resources : {};
  return {
    cpu_num: Math.max(1, Number(raw.cpu_num ?? raw.cpu ?? 1) || 1),
    gpu_mem: Math.max(0, Number(raw.gpu_mem ?? raw.gpuMemoryMb ?? 0) || 0),
    io_num: Math.max(0, Number(raw.io_num ?? 0) || 0),
  };
}

function normalizeTaskKind(data = {}) {
  const raw = String(data.task_kind || data.taskKind || '').toLowerCase();
  if (['cpu', 'gpu', 'io'].includes(raw)) return raw;
  const resources = normalizeTaskResources(data.resources || {});
  if (resources.gpu_mem > 0 || data.modelAnchor || data.model_anchor || data.localModel) return 'gpu';
  return 'cpu';
}

function validateAgentTaskDefinitionCode(definition) {
  const errors = [];
  const code = String(definition?.code || '');
  const relativePath = definition?.relativePath || 'task definition';
  const decoratorMatches = code.matchAll(/@task\s*\(([\s\S]*?)\)/g);

  for (const match of decoratorMatches) {
    const args = match[1] || '';
    if (/(^|[,\s])(?:inputs|outputs)\s*=/.test(args)) {
      errors.push(
        `${relativePath}: @task no longer accepts inputs/outputs. Use @task or @task(resources={...}); Maze infers inputs from the function signature and outputs from returned dict keys.`,
      );
    }
  }
  if (/(["'])workspace\/files\//.test(code) || /(["'])files\//.test(code)) {
    errors.push(
      `${relativePath}: task code should read/write files relative to the task cwd, for example "input.csv" or "reports/output.md"; do not prefix paths with "workspace/files/" or "files/".`,
    );
  }

  return errors;
}

function normalizeWorkflowNodeInputs(inputs) {
  if (!Array.isArray(inputs)) return [];
  return inputs.map((input) => {
    const nextInput = { ...(input || {}) };
    if (nextInput.source === 'node') {
      const taskId = nextInput.nodeId || nextInput.taskId || nextInput.taskSource?.taskId || nextInput.taskSource?.nodeId;
      const outputKey = nextInput.outputName || nextInput.outputKey || nextInput.taskSource?.outputKey || nextInput.taskSource?.outputName;
      nextInput.source = 'task';
      if (taskId || outputKey) {
        nextInput.taskSource = {
          taskId: String(taskId || ''),
          outputKey: String(outputKey || ''),
        };
      }
      delete nextInput.nodeId;
      delete nextInput.outputName;
    } else if (nextInput.source === 'task' && nextInput.taskSource) {
      nextInput.taskSource = {
        taskId: String(nextInput.taskSource.taskId || nextInput.taskSource.nodeId || nextInput.taskId || ''),
        outputKey: String(nextInput.taskSource.outputKey || nextInput.taskSource.outputName || nextInput.outputKey || ''),
      };
      delete nextInput.taskId;
      delete nextInput.outputKey;
      delete nextInput.nodeId;
      delete nextInput.outputName;
    }
    return nextInput;
  });
}

function normalizeAgentWorkflowDraftInput(input = {}) {
  const workflow = input.workflow && typeof input.workflow === 'object' ? input.workflow : input;
  const name = String(workflow.name || input.name || 'Agent Workflow').trim() || 'Agent Workflow';
  const nodes = Array.isArray(workflow.nodes) ? workflow.nodes : [];
  const edges = Array.isArray(workflow.edges) ? workflow.edges : [];
  return {
    name,
    relativePath: normalizeWorkflowRelativePath(input.relativePath || workflow.relativePath || '', name),
    workflow: {
      name,
      nodes: nodes.map((node, index) => ({
        id: String(node.id || `node-${index + 1}`),
        type: 'taskNode',
        position: node.position && typeof node.position === 'object'
          ? node.position
          : { x: 120 + index * 260, y: 120 },
        data: {
          category: node.data?.category || node.category || 'workspace',
          nodeType: node.data?.nodeType || node.nodeType || 'task',
          label: node.data?.label || node.label || node.data?.functionName || node.functionName || `Task ${index + 1}`,
          taskRef: node.data?.taskRef || node.taskRef,
          workspaceDir: node.data?.workspaceDir,
          taskPath: node.data?.taskPath || node.data?.relativePath || node.taskPath || node.relativePath,
          functionName: node.data?.functionName || node.functionName,
          customCode: node.data?.customCode || node.customCode,
          inputs: normalizeWorkflowNodeInputs(node.data?.inputs || node.inputs),
          outputs: Array.isArray(node.data?.outputs || node.outputs) ? (node.data?.outputs || node.outputs) : [],
          task_kind: normalizeTaskKind(node.data || node),
          resources: normalizeTaskResources(node.data?.resources || node.resources),
          configured: node.data?.configured !== false,
        },
      })),
      edges: edges.map((edge, index) => ({
        id: String(edge.id || `edge-${index + 1}`),
        source: String(edge.source || ''),
        target: String(edge.target || ''),
        sourceHandle: edge.sourceHandle || undefined,
        targetHandle: edge.targetHandle || undefined,
      })).filter((edge) => edge.source && edge.target),
    },
    taskDefinitions: normalizeAgentTaskDefinitions(input.taskDefinitions || workflow.taskDefinitions || workflow.includedTasks || []),
    description: String(input.description || workflow.description || ''),
  };
}

async function writeAgentDraft(workspaceDir, draft) {
  const now = new Date().toISOString();
  const next = {
    schema: 'maze_workspace_agent_draft',
    schema_version: 1,
    createdAt: draft.createdAt || now,
    updatedAt: now,
    status: draft.status || 'draft',
    ...draft,
  };
  await writeJsonAtomic(agentDraftPath(workspaceDir, next.id), redactSecrets(next));
  return next;
}

async function loadAgentDraft(workspaceDir, draftId) {
  const raw = await fs.readFile(agentDraftPath(workspaceDir, draftId), 'utf-8');
  return JSON.parse(raw);
}

async function readWorkspaceWorkflowForAgent(context, input = {}) {
  const relativePathInput = input.relativePath || input.path || '';
  if (!relativePathInput) {
    const error = new Error('relativePath is required');
    error.status = 400;
    throw error;
  }
  const { relativePath, fullPath } = resolveWorkflowFile(context.workspaceDir, relativePathInput, 'workflow');
  const raw = await fs.readFile(fullPath, 'utf-8');
  const payload = JSON.parse(raw);
  const workflow = normalizeWorkflowPayload(payload);
  const importResult = await importTaskDefinitions(context.workspaceDir, workflow.includedTasks, workflow.name);
  const hydratedNodes = await hydrateWorkspaceWorkflowNodes(
    workflow.nodes,
    context.workspaceDir,
    workflow.includedTasks,
    importResult.taskPathMap,
  );
  const stat = await fs.stat(fullPath);
  const taskDefinitions = collectTaskDefinitions(hydratedNodes, workflow.includedTasks).map((definition) => ({
    relativePath: definition.relativePath,
    functionName: definition.functionName,
    displayName: definition.displayName,
    inputs: definition.inputs || [],
    outputs: definition.outputs || [],
    resources: definition.resources || {},
    code: input.includeCode === false ? undefined : String(definition.code || '').slice(0, Math.min(Math.max(Number(input.maxCodeChars || 4000), 0), 20000)),
    truncated: input.includeCode === false ? undefined : String(definition.code || '').length > Math.min(Math.max(Number(input.maxCodeChars || 4000), 0), 20000),
  }));
  return {
    ok: true,
    relativePath,
    updatedAt: stat.mtime.toISOString(),
    size: stat.size,
    workflow: {
      name: workflow.name,
      nodes: hydratedNodes,
      edges: workflow.edges,
    },
    taskDefinitions,
    importedTaskDefinitions: {
      imported: importResult.imported || [],
      skipped: importResult.skipped || [],
      remapped: importResult.remapped || [],
    },
  };
}

async function readWorkspaceTaskForAgent(context, input = {}) {
  const relativePathInput = input.relativePath || input.path || '';
  if (!relativePathInput) {
    const error = new Error('relativePath is required');
    error.status = 400;
    throw error;
  }
  const maxChars = Math.min(Math.max(Number(input.maxChars || 12000), 0), 50000);
  const { relativePath, fullPath } = resolveTaskDefinitionFile(context.workspaceDir, relativePathInput);
  const stat = await fs.stat(fullPath);
  if (!stat.isFile()) {
    const error = new Error('Task path is not a file');
    error.status = 400;
    throw error;
  }
  const code = await fs.readFile(fullPath, 'utf-8');
  return {
    ok: true,
    relativePath,
    updatedAt: stat.mtime.toISOString(),
    size: stat.size,
    code: code.slice(0, maxChars),
    truncated: code.length > maxChars,
  };
}

async function listWorkspaceFilesForAgent(context, input = {}) {
  const { fullPath, filesDir, relativePath } = resolveWorkspaceFilePath(context.workspaceDir, input.path || '');
  const stat = await fs.stat(fullPath).catch((error) => {
    if (error.code === 'ENOENT') return null;
    throw error;
  });
  if (!stat) {
    const error = new Error('Workspace file path not found');
    error.status = 404;
    throw error;
  }
  if (!stat.isDirectory()) {
    const error = new Error('Workspace file path is not a directory');
    error.status = 400;
    throw error;
  }
  const maxEntries = Math.min(Math.max(Number(input.maxEntries || 100), 1), 500);
  const entries = await fs.readdir(fullPath, { withFileTypes: true });
  const files = await Promise.all(entries.slice(0, maxEntries).map((entry) => (
    describeWorkspaceFile(filesDir, path.join(fullPath, entry.name))
  )));
  files.sort((a, b) => {
    if (a.type !== b.type) return a.type === 'directory' ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
  return {
    ok: true,
    path: relativePath,
    files,
    truncated: entries.length > maxEntries,
    totalEntries: entries.length,
  };
}

async function readWorkspaceFileForAgent(context, input = {}) {
  const relativePathInput = input.relativePath || input.path || '';
  if (!relativePathInput) {
    const error = new Error('relativePath is required');
    error.status = 400;
    throw error;
  }
  const maxBytes = Math.min(Math.max(Number(input.maxBytes || 256 * 1024), 1), 1024 * 1024);
  const maxChars = Math.min(Math.max(Number(input.maxChars || 20000), 1), 100000);
  const { fullPath, relativePath } = resolveWorkspaceFilePath(context.workspaceDir, relativePathInput);
  assertAgentFileReadAllowed(relativePath);
  const stat = await fs.stat(fullPath);
  if (!stat.isFile()) {
    const error = new Error('Workspace file path is not a file');
    error.status = 400;
    throw error;
  }
  if (stat.size > maxBytes) {
    const error = new Error(`Workspace file is too large to read: ${stat.size} bytes > ${maxBytes} bytes`);
    error.status = 413;
    error.code = 'FILE_TOO_LARGE';
    throw error;
  }
  const buffer = await fs.readFile(fullPath);
  const hasNul = buffer.includes(0);
  const text = buffer.toString('utf-8');
  return {
    ok: true,
    relativePath,
    updatedAt: stat.mtime.toISOString(),
    size: stat.size,
    content: hasNul ? '' : text.slice(0, maxChars),
    encoding: hasNul ? 'binary' : 'utf-8',
    truncated: !hasNul && text.length > maxChars,
    binary: hasNul,
  };
}

async function createAgentWorkflowDraft(context, input = {}) {
  const normalized = normalizeAgentWorkflowDraftInput(input);
  const draftId = safeAgentId(input.draftId, 'draft');
  const draft = await writeAgentDraft(context.workspaceDir, {
    id: draftId,
    workspaceId: context.workspaceId,
    workspaceDir: context.workspaceDir,
    name: normalized.name,
    relativePath: normalized.relativePath,
    description: normalized.description,
    workflow: normalized.workflow,
    taskDefinitions: normalized.taskDefinitions,
    validation: validateAgentWorkflowDraftShape(normalized),
    saved: null,
    run: null,
  });
  return agentDraftPublic(draft);
}

async function cloneWorkspaceWorkflowToDraft(context, input = {}) {
  const relativePath = input.relativePath || input.sourceRelativePath || input.path || '';
  if (!relativePath) {
    const error = new Error('relativePath is required');
    error.status = 400;
    throw error;
  }
  const source = await readWorkspaceWorkflowForAgent(context, {
    relativePath,
    includeCode: input.includeCode !== false,
    maxCodeChars: input.maxCodeChars || 12000,
  });
  const draft = await createAgentWorkflowDraft(context, {
    draftId: input.draftId,
    name: input.name || source.workflow.name,
    relativePath: input.draftRelativePath || input.targetRelativePath || source.relativePath,
    description: input.description || `Draft cloned from ${source.relativePath}.`,
    nodes: source.workflow.nodes,
    edges: source.workflow.edges,
    taskDefinitions: source.taskDefinitions || [],
  });
  return {
    ...draft,
    source: {
      relativePath: source.relativePath,
      updatedAt: source.updatedAt,
      size: source.size,
    },
  };
}

function hasOwnValue(value, key) {
  return Boolean(value && typeof value === 'object' && Object.prototype.hasOwnProperty.call(value, key));
}

async function updateAgentWorkflowDraft(context, draftId, input = {}) {
  const current = await loadAgentDraft(context.workspaceDir, draftId);
  const workflowInput = input.workflow && typeof input.workflow === 'object' && !Array.isArray(input.workflow)
    ? input.workflow
    : input;
  const merged = {
    name: hasOwnValue(workflowInput, 'name') ? workflowInput.name : (current.workflow?.name || current.name),
    relativePath: hasOwnValue(input, 'relativePath')
      ? input.relativePath
      : (hasOwnValue(workflowInput, 'relativePath') ? workflowInput.relativePath : current.relativePath),
    description: hasOwnValue(input, 'description')
      ? input.description
      : (hasOwnValue(workflowInput, 'description') ? workflowInput.description : current.description),
    nodes: hasOwnValue(workflowInput, 'nodes') ? workflowInput.nodes : (current.workflow?.nodes || []),
    edges: hasOwnValue(workflowInput, 'edges') ? workflowInput.edges : (current.workflow?.edges || []),
    taskDefinitions: hasOwnValue(input, 'taskDefinitions')
      ? input.taskDefinitions
      : (
        hasOwnValue(workflowInput, 'taskDefinitions')
          ? workflowInput.taskDefinitions
          : (
            hasOwnValue(workflowInput, 'includedTasks')
              ? workflowInput.includedTasks
              : (current.taskDefinitions || [])
          )
      ),
  };
  const normalized = normalizeAgentWorkflowDraftInput(merged);
  const draft = await writeAgentDraft(context.workspaceDir, {
    ...current,
    status: 'draft',
    dismissedAt: null,
    dismissedReason: '',
    name: normalized.name,
    relativePath: normalized.relativePath,
    description: normalized.description,
    workflow: normalized.workflow,
    taskDefinitions: normalized.taskDefinitions,
    validation: validateAgentWorkflowDraftShape(normalized),
    saved: null,
    run: null,
    revision: Number(current.revision || 1) + 1,
    updatedBy: 'workspace_agent',
  });
  return agentDraftPublic(draft);
}

function validateAgentWorkflowDraftShape(draft) {
  const workflow = draft.workflow || {};
  const nodes = Array.isArray(workflow.nodes) ? workflow.nodes : [];
  const edges = Array.isArray(workflow.edges) ? workflow.edges : [];
  const errors = [];
  const warnings = [];
  const nodeIds = new Set();

  if (!String(workflow.name || draft.name || '').trim()) {
    errors.push('Workflow name is required.');
  }
  if (nodes.length === 0) {
    errors.push('At least one task node is required.');
  }

  for (const node of nodes) {
    if (!node.id) {
      errors.push('Every node needs an id.');
      continue;
    }
    if (nodeIds.has(node.id)) {
      errors.push(`Duplicate node id: ${node.id}`);
    }
    nodeIds.add(node.id);
    if (!node.data?.label) {
      warnings.push(`Node ${node.id} has no label.`);
    }
    if (node.data?.category === 'workspace') {
      if (!node.data?.taskPath) {
        errors.push(`Workspace node ${node.id} needs taskPath.`);
      }
      if (!node.data?.functionName) {
        errors.push(`Workspace node ${node.id} needs functionName.`);
      }
    }
    if (node.data?.category === 'builtin' && !node.data?.taskRef) {
      errors.push(`Builtin node ${node.id} needs taskRef.`);
    }
  }

  for (const edge of edges) {
    if (!nodeIds.has(edge.source)) {
      errors.push(`Edge ${edge.id || `${edge.source}->${edge.target}`} has unknown source ${edge.source}.`);
    }
    if (!nodeIds.has(edge.target)) {
      errors.push(`Edge ${edge.id || `${edge.source}->${edge.target}`} has unknown target ${edge.target}.`);
    }
  }

  const taskDefinitions = normalizeAgentTaskDefinitions(draft.taskDefinitions || []);
  for (const definition of taskDefinitions) {
    errors.push(...validateAgentTaskDefinitionCode(definition));
  }
  const definitions = new Set(taskDefinitions.map((definition) => taskDefinitionKey(definition.relativePath, definition.functionName)));
  for (const node of nodes) {
    if (node.data?.category !== 'workspace') continue;
    if (!node.data.taskPath) {
      continue;
    }
    const key = taskDefinitionKey(node.data.taskPath, node.data.functionName);
    if (!definitions.has(key)) {
      warnings.push(`Workspace node ${node.id} references ${node.data.taskPath} without an inline task definition; an existing workspace task must provide it.`);
    }
  }

  return {
    ok: errors.length === 0,
    errors,
    warnings,
    nodeCount: nodes.length,
    edgeCount: edges.length,
    taskDefinitionCount: taskDefinitions.length,
    validatedAt: new Date().toISOString(),
  };
}

function agentDraftPublic(draft) {
  return {
    id: draft.id,
    status: draft.status || 'draft',
    revision: draft.revision || 1,
    name: draft.name || draft.workflow?.name,
    relativePath: draft.relativePath,
    description: draft.description || '',
    workflow: draft.workflow,
    taskDefinitions: draft.taskDefinitions || [],
    validation: draft.validation || null,
    saved: draft.saved || null,
    run: draft.run || null,
    fixContext: draft.fixContext || null,
    dismissedAt: draft.dismissedAt || null,
    dismissedReason: draft.dismissedReason || '',
    createdAt: draft.createdAt,
    updatedAt: draft.updatedAt,
  };
}

async function validateAgentWorkflowDraft(context, draftId) {
  const draft = await loadAgentDraft(context.workspaceDir, draftId);
  draft.validation = validateAgentWorkflowDraftShape(draft);
  await writeAgentDraft(context.workspaceDir, draft);
  return agentDraftPublic(draft);
}

async function dismissAgentWorkflowDraft(context, draftId, options = {}) {
  const draft = await loadAgentDraft(context.workspaceDir, draftId);
  draft.status = 'dismissed';
  draft.dismissedAt = new Date().toISOString();
  draft.dismissedReason = String(options.reason || '').slice(0, 500);
  await writeAgentDraft(context.workspaceDir, draft);
  return agentDraftPublic(draft);
}

async function saveAgentWorkflowDraft(context, draftId, options = {}) {
  if (!options.confirmed) {
    const error = new Error('Confirmation required before saving a workflow draft');
    error.status = 409;
    error.code = 'CONFIRMATION_REQUIRED';
    throw error;
  }

  const draft = await loadAgentDraft(context.workspaceDir, draftId);
  draft.validation = validateAgentWorkflowDraftShape(draft);
  if (!draft.validation.ok) {
    const error = new Error(`Draft is invalid: ${draft.validation.errors.join('; ')}`);
    error.status = 400;
    throw error;
  }

  const importResult = await importTaskDefinitions(context.workspaceDir, draft.taskDefinitions || [], draft.workflow?.name || draft.name);
  const taskPathMap = importResult.taskPathMap || new Map();
  const workflowNodes = (draft.workflow.nodes || []).map((node) => stripNodeTaskCode(node, context.workspaceDir));
  const hydratedNodes = await hydrateWorkspaceWorkflowNodes(
    workflowNodes,
    context.workspaceDir,
    draft.taskDefinitions || [],
    taskPathMap,
  );
  const strippedHydrated = hydratedNodes.map((node) => stripNodeTaskCode(node, context.workspaceDir));
  const workflowName = draft.workflow?.name || draft.name || 'Agent Workflow';
  const { relativePath, fullPath } = resolveWorkflowFile(context.workspaceDir, options.relativePath || draft.relativePath, workflowName);
  if (await fileExists(fullPath) && draft.saved?.relativePath !== relativePath && options.overwrite !== true) {
    const error = new Error(`Workflow already exists: ${relativePath}`);
    error.status = 409;
    error.code = 'WORKFLOW_EXISTS';
    throw error;
  }
  const workflowId = options.workflowId || draft.saved?.workflowId || uuidv4();
  const payload = {
    schema: 'maze-playground-workflow',
    version: 3,
    savedAt: new Date().toISOString(),
    workflow: {
      name: workflowName,
      sourceWorkflowId: workflowId,
      nodes: strippedHydrated,
      edges: draft.workflow.edges || [],
    },
  };

  await fs.mkdir(path.dirname(fullPath), { recursive: true });
  await fs.writeFile(fullPath, JSON.stringify(payload, null, 2), 'utf-8');
  const manifest = await recordWorkspaceMutation(context.workspaceDir, 'agent_workflow_saved', {
    path: relativePath,
    draft_id: draft.id,
    name: workflowName,
    imported_task_count: importResult.imported?.length || 0,
  });

  const workflow = {
    id: workflowId,
    name: workflowName,
    mazeWorkflowId: draft.saved?.mazeWorkflowId || null,
    nodes: hydratedNodes,
    edges: draft.workflow.edges || [],
    createdAt: draft.saved?.createdAt || new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    status: 'saved',
    workspaceDir: context.workspaceDir,
    workspaceId: context.workspaceId,
    relativePath,
  };

  draft.saved = {
    workflowId,
    relativePath,
    savedAt: new Date().toISOString(),
    workspaceManifestVersion: Number(manifest.manifest_version || context.workspaceManifestVersion),
    importedTaskDefinitions: {
      imported: importResult.imported || [],
      skipped: importResult.skipped || [],
      remapped: importResult.remapped || [],
    },
  };
  draft.workflow = {
    ...draft.workflow,
    nodes: hydratedNodes,
  };
  await writeAgentDraft(context.workspaceDir, draft);

  return {
    draft: agentDraftPublic(draft),
    workflow,
    workspaceId: manifest.workspace_id,
    workspaceDir: context.workspaceDir,
    workspaceManifestVersion: Number(manifest.manifest_version || context.workspaceManifestVersion),
    relativePath,
  };
}

async function runAgentWorkflowDraft(context, draftId, options = {}) {
  if (!options.confirmed) {
    const error = new Error('Confirmation required before running a workflow draft');
    error.status = 409;
    error.code = 'CONFIRMATION_REQUIRED';
    throw error;
  }
  const saved = await saveAgentWorkflowDraft(context, draftId, { ...options, confirmed: true });
  const workflow = saved.workflow;
  const submission = await submitPlaygroundWorkflow({
    workflow,
    context: {
      ...context,
      workspaceManifestVersion: saved.workspaceManifestVersion,
    },
    playgroundWorkflowId: workflow.id,
    workflowPath: saved.relativePath,
    draftId,
  });

  const draft = await loadAgentDraft(context.workspaceDir, draftId);
  draft.run = {
    runId: submission.runId,
    workflowId: workflow.id,
    coreWorkflowId: submission.coreWorkflowId,
    submissionId: submission.submissionId,
    startedAt: new Date().toISOString(),
    status: 'submitted',
  };
  await writeAgentDraft(context.workspaceDir, draft);

  return {
    draft: agentDraftPublic(draft),
    workflow,
    runId: submission.runId,
    workflowId: workflow.id,
    coreWorkflowId: submission.coreWorkflowId,
    submissionId: submission.submissionId,
    workspaceId: context.workspaceId,
    workspaceDir: context.workspaceDir,
  };
}

function agentToolDefinitions() {
  return [
    {
      type: 'function',
      function: {
        name: 'list_workspace_items',
        description: 'List workspace workflows, tasks, skills, and files with compact metadata.',
        parameters: {
          type: 'object',
          properties: {
            include: {
              type: 'array',
              items: { type: 'string', enum: ['workflows', 'tasks', 'skills', 'files'] },
              description: 'Which item types to include. Defaults to all.',
            },
          },
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'list_workspace_files',
        description: 'List files under workspace/files. Use this before reading user data files.',
        parameters: {
          type: 'object',
          properties: {
            path: { type: 'string', description: 'Optional path inside workspace/files.' },
            maxEntries: { type: 'number', description: 'Maximum entries to return, capped by the server.' },
          },
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'read_workspace_file',
        description: 'Read a text-like file under workspace/files with size and content caps.',
        parameters: {
          type: 'object',
          required: ['relativePath'],
          properties: {
            relativePath: { type: 'string', description: 'A path inside workspace/files.' },
            maxBytes: { type: 'number', description: 'Maximum file size to read, capped by the server.' },
            maxChars: { type: 'number', description: 'Maximum content chars to return, capped by the server.' },
          },
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'read_current_workflow',
        description: 'Read the workflow currently open in the Maze Playground UI.',
        parameters: { type: 'object', properties: {} },
      },
    },
    {
      type: 'function',
      function: {
        name: 'read_workspace_workflow',
        description: 'Read a saved workspace workflow by relativePath and return hydrated nodes, edges, and task definitions.',
        parameters: {
          type: 'object',
          required: ['relativePath'],
          properties: {
            relativePath: { type: 'string', description: 'A workflows/*.json path.' },
            includeCode: { type: 'boolean', description: 'Whether to include task code snippets. Defaults to true.' },
            maxCodeChars: { type: 'number', description: 'Maximum task code chars per task, capped by the server.' },
          },
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'read_workspace_task',
        description: 'Read a workspace task Python file by relativePath.',
        parameters: {
          type: 'object',
          required: ['relativePath'],
          properties: {
            relativePath: { type: 'string', description: 'A tasks/*.py path.' },
            maxChars: { type: 'number', description: 'Maximum code chars, capped by the server.' },
          },
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'create_workflow_draft',
        description: 'Create a workflow draft immediately when the user asks for a new workflow or agrees to proceed with a proposed workflow design. This does not overwrite saved workflows.',
        parameters: {
          type: 'object',
          required: ['name', 'nodes', 'edges'],
          properties: {
            name: { type: 'string' },
            description: { type: 'string' },
            relativePath: { type: 'string', description: 'Optional workflows/*.json path.' },
            nodes: { type: 'array', items: { type: 'object' } },
            edges: { type: 'array', items: { type: 'object' } },
            taskDefinitions: {
              type: 'array',
              items: { type: 'object' },
              description: 'Optional workspace task files with relativePath, functionName, code, inputs, outputs, resources.',
            },
          },
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'clone_workflow_to_draft',
        description: 'Clone an existing saved workspace workflow into a draft so it can be safely revised without overwriting the source.',
        parameters: {
          type: 'object',
          required: ['relativePath'],
          properties: {
            relativePath: { type: 'string', description: 'Source workflows/*.json path.' },
            name: { type: 'string', description: 'Optional draft workflow name.' },
            description: { type: 'string' },
            draftRelativePath: { type: 'string', description: 'Optional draft target workflows/*.json path.' },
            draftId: { type: 'string' },
            includeCode: { type: 'boolean', description: 'Whether to include task code snippets. Defaults to true.' },
            maxCodeChars: { type: 'number', description: 'Maximum task code chars per task, capped by the server.' },
          },
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'update_workflow_draft',
        description: 'Update an existing workflow draft in place when the user asks to iterate on a prior draft.',
        parameters: {
          type: 'object',
          required: ['draftId'],
          properties: {
            draftId: { type: 'string' },
            name: { type: 'string' },
            description: { type: 'string' },
            relativePath: { type: 'string', description: 'Optional workflows/*.json path.' },
            nodes: { type: 'array', items: { type: 'object' } },
            edges: { type: 'array', items: { type: 'object' } },
            taskDefinitions: {
              type: 'array',
              items: { type: 'object' },
              description: 'Optional replacement workspace task files with relativePath, functionName, code, inputs, outputs, resources.',
            },
          },
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'validate_workflow_draft',
        description: 'Validate a workflow draft structure and task references.',
        parameters: {
          type: 'object',
          required: ['draftId'],
          properties: {
            draftId: { type: 'string' },
          },
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'inspect_recent_run_errors',
        description: 'Inspect recent static or dynamic workflow run failures.',
        parameters: {
          type: 'object',
          properties: {
            limit: { type: 'number' },
          },
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'inspect_workflow_run',
        description: 'Inspect one workflow run by runId, including status, nodes, recent events, artifacts, final result, and guidance.',
        parameters: {
          type: 'object',
          required: ['runId'],
          properties: {
            runId: { type: 'string', description: 'The workflow run id to inspect.' },
            kind: {
              type: 'string',
              enum: ['auto', 'static', 'dynamic'],
              description: 'Run storage type. Defaults to auto.',
            },
            eventLimit: { type: 'number', description: 'Maximum recent events to return, capped by the server.' },
            nodeLimit: { type: 'number', description: 'Maximum nodes to return, capped by the server.' },
            artifactLimit: { type: 'number', description: 'Maximum artifacts to return, capped by the server.' },
          },
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'create_fix_draft_from_run',
        description: 'Create a safe workflow draft for fixing a failed workflow run. This does not overwrite or run anything.',
        parameters: {
          type: 'object',
          required: ['runId'],
          properties: {
            runId: { type: 'string', description: 'The failed static workflow run id.' },
            workflowRelativePath: { type: 'string', description: 'Optional source workflows/*.json path if the run cannot be mapped automatically.' },
            draftRelativePath: { type: 'string', description: 'Optional target workflows/*.json path for the fix draft.' },
            name: { type: 'string', description: 'Optional draft workflow name.' },
            maxCodeChars: { type: 'number', description: 'Maximum task code chars per related task, capped by the server.' },
          },
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'promote_run_artifact',
        description: 'Copy an artifact from a static workflow run into workspace/files so it can be reused by later workflow drafts.',
        parameters: {
          type: 'object',
          required: ['runId', 'path'],
          properties: {
            runId: { type: 'string', description: 'The static workflow run id that produced the artifact.' },
            path: { type: 'string', description: 'Artifact path from inspect_workflow_run, for example reports/output.txt.' },
            taskId: { type: 'string', description: 'Optional task id or node id that produced the artifact.' },
            targetPath: { type: 'string', description: 'Optional target path under workspace/files. Defaults to artifact path.' },
            overwrite: { type: 'boolean', description: 'Whether to overwrite an existing workspace file. Defaults to true.' },
          },
        },
      },
    },
  ];
}

function compactAgentDiagnosticText(value, maxLength = 420) {
  let text = '';
  if (typeof value === 'string') {
    text = value;
  } else if (value !== undefined && value !== null) {
    try {
      text = JSON.stringify(redactSecrets(value));
    } catch {
      text = String(value);
    }
  }
  text = text.replace(/\s+/g, ' ').trim();
  return text.length > maxLength ? `${text.slice(0, maxLength - 1)}…` : text;
}

function agentIssueGuidance(issue) {
  const text = compactAgentDiagnosticText(issue, 500);
  const lower = text.toLowerCase();
  let stage = 'runtime';
  let suggestion = 'Inspect the failed node/event, fix the task code, inputs, resources, or environment, then rerun.';

  if (lower.includes('api key') || lower.includes('unauthorized') || lower.includes('401')) {
    stage = 'llm';
    suggestion = 'Check the LLM base URL, API key, and model before rerunning.';
  } else if (lower.includes('mcp') && (lower.includes('connection') || lower.includes('not found') || lower.includes('closed'))) {
    stage = 'mcp';
    suggestion = 'Test the MCP profile/server, verify command/url/cwd/env, and rerun after discovery succeeds.';
  } else if (lower.includes('permission')) {
    stage = 'permission';
    suggestion = 'Review the requested permission target and either approve it or adjust the workflow/tool to use allowed paths.';
  } else if (lower.includes('docker')) {
    stage = 'sandbox';
    suggestion = 'Use workspace_sandbox or connect a worker that reports docker_sandbox=true.';
  } else if (lower.includes('timeout') || lower.includes('timed out')) {
    stage = 'execution';
    suggestion = 'Increase timeout, reduce max steps, or split the workflow into smaller tasks.';
  } else if (lower.includes('no registered') || lower.includes('no alive') || lower.includes('insufficient')) {
    stage = 'scheduler';
    suggestion = 'Check Cluster resources and lower CPU/GPU requests or reconnect worker nodes.';
  } else if (lower.includes('json') || lower.includes('parse')) {
    stage = 'llm/tool';
    suggestion = 'Inspect the raw LLM/tool output and make the prompt or schema stricter.';
  } else if (lower.includes('modulenotfounderror') || lower.includes('no module named')) {
    stage = 'dependency';
    suggestion = 'Use an available dependency, vendor the code into a workspace task, or install the package in the runtime environment.';
  } else if (lower.includes('filenotfounderror') || lower.includes('no such file or directory')) {
    stage = 'filesystem';
    suggestion = 'Use workspace-relative paths and ensure required files are created or uploaded before the run.';
  }

  return { stage, issue: text, suggestion };
}

function collectAgentRunIssues(run, nodes = [], events = []) {
  const rawIssues = [];
  if (run?.error) rawIssues.push(run.error);
  if (run?.failure_reason) rawIssues.push(run.failure_reason);
  if (run?.cancel_reason) rawIssues.push(run.cancel_reason);
  if (run?.final_result?.error) rawIssues.push(run.final_result.error);
  if (run?.final_result?.failure_reason) rawIssues.push(run.final_result.failure_reason);

  nodes.forEach((node) => {
    if (node?.error) rawIssues.push(node.error);
    if (node?.last_error) rawIssues.push(node.last_error);
    if (node?.pending_reason) rawIssues.push(node.pending_reason);
  });

  events.forEach((event) => {
    const data = event?.data || event;
    if ([
      'agent_error',
      'agent_skill_load_failed',
      'agent_mcp_discovery_failed',
      'agent_mcp_tool_call_finished',
      'agent_permission_denied',
      'task_exception',
      'workflow_failed',
    ].includes(event?.type)) {
      rawIssues.push(data?.error || data?.result || data?.reason || data);
    }
  });

  const byIssue = new Map();
  rawIssues.forEach((issue) => {
    const guidance = agentIssueGuidance(issue);
    if (guidance.issue && !byIssue.has(guidance.issue)) {
      byIssue.set(guidance.issue, guidance);
    }
  });
  return Array.from(byIssue.values()).slice(0, 8);
}

function summarizeAgentRunNodes(run, limit = 12) {
  const nodes = Object.values(run?.task_nodes || {});
  return nodes.slice(0, limit).map((node) => ({
    nodeId: node.node_id || node.id || node.task_id,
    taskId: node.task_id || node.maze_task_id,
    label: node.label || node.name || node.task_name,
    status: node.status,
    taskPath: node.task_path || node.taskPath,
    functionName: node.function_name || node.functionName,
    nodeIp: node.node_ip,
    gpuId: node.gpu_id,
    error: compactAgentDiagnosticText(node.error || node.last_error || node.pending_reason || '', 500),
  }));
}

function summarizeAgentRunEvents(events = [], limit = 12) {
  return events.slice(-limit).map((event) => {
    const data = event?.data || {};
    return {
      seq: event.seq,
      type: event.type,
      timestamp: event.timestamp,
      nodeId: data.node_id || data.nodeId,
      taskId: data.task_id || data.maze_task_id,
      tool: data.tool || data.tool_name || data.agent_tool,
      status: data.status,
      error: compactAgentDiagnosticText(data.error || data.result || data.reason || '', 420),
    };
  });
}

function buildAgentArtifactDownload(artifact, { kind, runId, workspaceId, workspaceDir, taskId, nodeId } = {}) {
  const artifactPath = String(artifact?.path || artifact?.relative_path || artifact?.name || artifact?.filename || '').trim();
  const sha256 = String(artifact?.sha256 || '').trim();
  const download = {};

  if (kind === 'static' && runId && artifactPath && (taskId || nodeId)) {
    const params = new URLSearchParams({
      taskId: String(taskId || nodeId),
      path: artifactPath,
    });
    if (workspaceId) {
      params.set('workspaceId', workspaceId);
    } else if (workspaceDir) {
      params.set('workspaceDir', workspaceDir);
    }
    download.staticRun = {
      method: 'GET',
      url: `/api/workflow-runs/static/${encodeURIComponent(runId)}/artifacts/download?${params.toString()}`,
    };
    download.url = download.staticRun.url;
    download.kind = 'static-run';
  }

  if (/^[a-f0-9]{64}$/i.test(sha256)) {
    download.cas = {
      method: 'GET',
      url: `/api/artifacts/sha256/${encodeURIComponent(sha256)}`,
    };
    if (!download.url) {
      download.url = download.cas.url;
      download.kind = 'cas';
    }
  }

  return download.url ? download : null;
}

function findStaticRunArtifact(run, { taskId, artifactPath } = {}) {
  const expectedTaskId = String(taskId || '').trim();
  const expectedPath = String(artifactPath || '').trim();
  if (!expectedPath) return null;

  for (const node of Object.values(run?.task_nodes || {})) {
    const taskMatches = !expectedTaskId || (
      node.maze_task_id === expectedTaskId
      || node.task_id === expectedTaskId
      || node.node_id === expectedTaskId
      || node.id === expectedTaskId
    );
    if (!taskMatches) continue;
    const artifacts = [
      ...(node.artifacts || []),
      ...(node.file_manifest?.files || []),
    ];
    const artifact = artifacts.find((item) => (
      item?.path === expectedPath
      || item?.relative_path === expectedPath
      || item?.name === expectedPath
      || item?.filename === expectedPath
    ));
    if (artifact) {
      return { node, artifact };
    }
  }

  return null;
}

function staticRunStorageRoots(workspaceDir, runId) {
  const roots = [];
  for (const runsDir of staticRunSearchDirs(workspaceDir)) {
    roots.push(path.resolve(runsDir, runId));
  }
  roots.push(
    path.resolve(workspaceDir, 'runs'),
    path.resolve(workspaceDir, 'workflow_runs'),
  );
  return Array.from(new Set(roots));
}

async function promoteArtifactIntoWorkspace(context, input = {}, options = {}) {
  const {
    targetPath,
    artifact = {},
    runId,
    taskId,
    path: artifactPath,
    sha256,
    storagePath,
    overwrite = true,
  } = input || {};

  let sourceSha = String(sha256 || artifact.sha256 || '').trim();
  let sourceStoragePath = String(storagePath || artifact.storage_path || '').trim();
  const sourceArtifactPath = String(artifactPath || artifact.path || artifact.name || sourceSha || '').trim();
  const destinationPath = targetPath || sourceArtifactPath;

  if (!destinationPath) {
    const error = new Error('targetPath is required');
    error.status = 400;
    throw error;
  }

  const workspaceDir = context.workspaceDir;
  if (!sourceStoragePath && runId && sourceArtifactPath) {
    const run = await loadStaticRun(workspaceDir, runId);
    const located = findStaticRunArtifact(run, {
      taskId: taskId || artifact.taskId || artifact.task_id || artifact.producer_task_id || artifact.nodeId || artifact.node_id,
      artifactPath: sourceArtifactPath,
    });
    const locatedArtifact = located?.artifact || null;
    sourceStoragePath = String(locatedArtifact?.storage_path || '').trim();
    if (!sourceSha) {
      sourceSha = String(locatedArtifact?.sha256 || '').trim();
    }
    if (!sourceStoragePath && !sourceSha) {
      const error = new Error('Static run artifact storage path not found');
      error.status = 404;
      throw error;
    }
  }

  if (!sourceSha && !sourceStoragePath) {
    const error = new Error('artifact sha256, storagePath, or static run artifact reference is required');
    error.status = 400;
    throw error;
  }

  const { fullPath, filesDir, relativePath } = resolveWorkspaceFilePath(workspaceDir, destinationPath);
  if (!overwrite && await fileExists(fullPath)) {
    const error = new Error(`Workspace file already exists: ${relativePath}`);
    error.status = 409;
    throw error;
  }

  await fs.mkdir(path.dirname(fullPath), { recursive: true });
  if (sourceStoragePath) {
    const resolvedStoragePath = path.resolve(sourceStoragePath);
    const allowedRunRoots = [
      path.resolve(workspaceDir, 'runs'),
      path.resolve(workspaceDir, 'workflow_runs'),
    ];
    const allowed = allowedRunRoots.some((root) => (
      resolvedStoragePath === root || resolvedStoragePath.startsWith(root + path.sep)
    ));
    if (!allowed) {
      const error = new Error('Static artifact storage path is outside this workspace run directory');
      error.status = 400;
      throw error;
    }
    await fs.copyFile(resolvedStoragePath, fullPath);
  } else {
    const { response, body } = await fetchMazeCoreBody(
      `/artifacts/sha256/${encodeURIComponent(sourceSha)}`,
      { signal: options.signal },
    );
    if (!response.ok) {
      const error = new Error(`Failed to download artifact: HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    await fs.writeFile(fullPath, body);
  }

  const file = await describeWorkspaceFile(filesDir, fullPath);
  const manifest = await recordWorkspaceMutation(workspaceDir, 'artifact_promoted', {
    path: file.relativePath,
    runId: runId || artifact.run_id || null,
    taskId: taskId || artifact.taskId || artifact.task_id || artifact.producer_task_id || null,
    sha256: sourceSha || artifact.sha256 || null,
  });

  return {
    success: true,
    workspaceId: manifest.workspace_id,
    workspaceDir,
    workspaceManifestVersion: Number(manifest.manifest_version || context.workspaceManifestVersion),
    file,
  };
}

function summarizeAgentRunArtifacts(run, limit = 12, options = {}) {
  const artifacts = [];
  for (const node of Object.values(run?.task_nodes || {})) {
    const nodeId = node.node_id || node.id || node.task_id;
    const taskId = node.task_id || node.maze_task_id;
    const addArtifact = (artifact, source) => {
      if (!artifact || artifacts.length >= limit) return;
      const artifactPath = artifact.path || artifact.relative_path || artifact.name || artifact.filename;
      artifacts.push({
        nodeId,
        taskId,
        source,
        path: artifactPath,
        sha256: artifact.sha256,
        sizeBytes: artifact.size_bytes || artifact.sizeBytes || artifact.bytes,
        contentType: artifact.content_type || artifact.contentType,
        description: compactAgentDiagnosticText(artifact.description || artifact.summary || '', 240),
        download: buildAgentArtifactDownload(artifact, {
          kind: options.kind,
          runId: options.runId || run?.run_id,
          workspaceId: options.workspaceId || run?.workspace_id,
          workspaceDir: options.workspaceDir,
          taskId,
          nodeId,
        }),
      });
    };
    for (const artifact of node?.artifacts || []) addArtifact(artifact, 'node.artifacts');
    for (const artifact of node?.file_manifest?.files || []) addArtifact(artifact, 'file_manifest.files');
  }
  return artifacts;
}

function summarizeAgentFinalResult(run) {
  const finalResult = run?.final_result;
  if (!finalResult || typeof finalResult !== 'object') return null;
  return {
    status: finalResult.status,
    stopReason: finalResult.stop_reason || finalResult.stopReason,
    answer: compactAgentDiagnosticText(finalResult.answer || finalResult.output || '', 700),
    error: compactAgentDiagnosticText(finalResult.error || finalResult.failure_reason || '', 700),
  };
}

function inferAgentDiagnosticStatus(run, nodes = []) {
  const status = String(run?.status || '').trim();
  if (status === 'failed' || status === 'canceled' || status === 'cancelled' || status === 'timed_out') {
    return status;
  }
  if (
    run?.error ||
    run?.failure_reason ||
    run?.cancel_reason ||
    nodes.some((node) => node?.status === 'failed' || node?.error || node?.last_error)
  ) {
    return 'failed';
  }
  return status || 'unknown';
}

async function buildStaticRunDiagnostic(context, summary, options = {}) {
  const run = await loadStaticRun(context.workspaceDir, summary.run_id).catch(() => summary);
  const events = await loadStaticRunEvents(context.workspaceDir, summary.run_id).catch(() => []);
  const rawNodes = Object.values(run.task_nodes || {});
  const nodes = summarizeAgentRunNodes(run, options.nodeLimit || 12);
  const eventSummary = summarizeAgentRunEvents(events, options.eventLimit || 12);
  const artifacts = summarizeAgentRunArtifacts(run, options.artifactLimit || 12, {
    kind: 'static',
    runId: run.run_id || summary.run_id,
    workspaceId: run.workspace_id || summary.workspace_id || context.workspaceId,
    workspaceDir: context.workspaceDir,
  });
  return {
    kind: 'static',
    runId: run.run_id || summary.run_id,
    workflowId: run.workflow_id || summary.workflow_id,
    workflowName: run.workflow_name || summary.workflow_name,
    status: inferAgentDiagnosticStatus(run, rawNodes),
    createdTime: run.created_time || summary.created_time,
    updatedTime: run.updated_time || summary.updated_time,
    finishedTime: run.finished_time || summary.finished_time,
    taskCounts: run.task_counts || summary.task_counts || {},
    error: compactAgentDiagnosticText(run.error || summary.error || '', 700),
    finalResult: summarizeAgentFinalResult(run),
    nodes,
    recentEvents: eventSummary,
    artifacts,
    guidance: collectAgentRunIssues(run, rawNodes, events),
  };
}

async function buildDynamicRunDiagnostic(summary, options = {}) {
  let run = summary;
  let events = [];
  try {
    const detail = await callMazeCore(`/dynamic_runs/${encodeURIComponent(summary.run_id)}`);
    run = detail.run || summary;
  } catch (error) {
    run = { ...summary, detail_error: error.message };
  }
  try {
    const payload = await callMazeCore(`/dynamic_runs/${encodeURIComponent(summary.run_id)}/events`);
    events = payload.events || [];
  } catch {
    events = [];
  }
  const rawNodes = Object.values(run.task_nodes || {});
  const nodes = summarizeAgentRunNodes(run, options.nodeLimit || 12);
  const eventSummary = summarizeAgentRunEvents(events, options.eventLimit || 12);
  const artifacts = summarizeAgentRunArtifacts(run, options.artifactLimit || 12, {
    kind: 'dynamic',
    runId: run.run_id || summary.run_id,
  });
  return {
    kind: run.kind || run.run_type || 'dynamic',
    runId: run.run_id || summary.run_id,
    status: inferAgentDiagnosticStatus(run, rawNodes),
    createdTime: run.created_time || summary.created_time,
    updatedTime: run.updated_time || summary.updated_time,
    finishedTime: run.finished_time || summary.finished_time,
    finalResult: summarizeAgentFinalResult(run),
    prompt: compactAgentDiagnosticText(run.final_result?.prompt || run.metadata?.prompt || '', 500),
    answer: compactAgentDiagnosticText(run.final_result?.answer || '', 500),
    error: compactAgentDiagnosticText(
      run.error_summary || run.failure_reason || run.cancel_reason || run.detail_error || summary.error_summary || '',
      700,
    ),
    nodes,
    recentEvents: eventSummary,
    artifacts,
    guidance: collectAgentRunIssues(run, rawNodes, events),
  };
}

async function inspectWorkflowRunForAgent(context, input = {}) {
  const runId = String(input.runId || input.run_id || '').trim();
  if (!runId) {
    throw new Error('runId is required');
  }
  if (runId.includes('/') || runId.includes('\\')) {
    throw new Error(`Invalid workflow run id: ${runId}`);
  }

  const kind = String(input.kind || 'auto').trim().toLowerCase() || 'auto';
  if (!['auto', 'static', 'dynamic'].includes(kind)) {
    throw new Error(`Unsupported workflow run kind: ${input.kind}`);
  }

  const options = {
    eventLimit: Math.min(Math.max(Number(input.eventLimit || 12), 1), 50),
    nodeLimit: Math.min(Math.max(Number(input.nodeLimit || 12), 1), 50),
    artifactLimit: Math.min(Math.max(Number(input.artifactLimit || 12), 1), 50),
  };

  let staticError = null;
  if (kind === 'auto' || kind === 'static') {
    try {
      const run = await loadStaticRun(context.workspaceDir, runId);
      return { ok: true, run: await buildStaticRunDiagnostic(context, run, options) };
    } catch (error) {
      staticError = error;
      if (kind === 'static') {
        throw new Error(`Static workflow run not found: ${runId}`);
      }
    }
  }

  if (kind === 'auto' || kind === 'dynamic') {
    try {
      const detail = await callMazeCore(`/dynamic_runs/${encodeURIComponent(runId)}`);
      return { ok: true, run: await buildDynamicRunDiagnostic(detail.run || { run_id: runId }, options) };
    } catch (dynamicError) {
      try {
        const detail = await callMazeCore(`/runs/${encodeURIComponent(runId)}`);
        const coreRun = requirePublicCoreRun(detail.run || { run_id: runId });
        return { ok: true, run: await buildDynamicRunDiagnostic(coreRun, options) };
      } catch (coreError) {
        if (kind === 'dynamic') {
          throw new Error(`Dynamic workflow run not found: ${runId}`);
        }
        const detail = compactAgentDiagnosticText(coreError.message || dynamicError.message || staticError?.message || '', 360);
        throw new Error(`Workflow run not found: ${runId}${detail ? ` (${detail})` : ''}`);
      }
    }
  }

  throw new Error(`Workflow run not found: ${runId}`);
}

async function findWorkspaceWorkflowByIdOrPath(context, { workflowId, workflowRelativePath, workflowName } = {}) {
  if (workflowRelativePath) {
    const source = await readWorkspaceWorkflowForAgent(context, {
      relativePath: workflowRelativePath,
      includeCode: true,
      maxCodeChars: 12000,
    });
    return source;
  }

  const files = await listWorkflowFiles(path.join(context.workspaceDir, 'workflows'));
  for (const filePath of files) {
    try {
      const raw = await fs.readFile(filePath, 'utf-8');
      const payload = JSON.parse(raw);
      const workflow = payload?.workflow || payload;
      const sourceWorkflowId = workflow?.sourceWorkflowId || workflow?.id || payload?.sourceWorkflowId || payload?.workflowId;
      const relativePath = toPosixPath(path.relative(context.workspaceDir, filePath));
      if (workflowId && sourceWorkflowId === workflowId) {
        return readWorkspaceWorkflowForAgent(context, {
          relativePath,
          includeCode: true,
          maxCodeChars: 12000,
        });
      }
    } catch {
      // Ignore malformed workflow files while searching for the source run workflow.
    }
  }

  if (workflowName) {
    for (const filePath of files) {
      try {
        const raw = await fs.readFile(filePath, 'utf-8');
        const payload = JSON.parse(raw);
        const workflow = normalizeWorkflowPayload(payload);
        if (workflow.name === workflowName) {
          return readWorkspaceWorkflowForAgent(context, {
            relativePath: toPosixPath(path.relative(context.workspaceDir, filePath)),
            includeCode: true,
            maxCodeChars: 12000,
          });
        }
      } catch {
        // Ignore malformed workflow files while searching by name.
      }
    }
  }

  return null;
}

function failedRunNodes(run) {
  return Object.values(run?.task_nodes || {})
    .filter((node) => node?.status === 'failed' || node?.error || node?.last_error)
    .map((node) => ({
      nodeId: node.node_id || node.id || node.task_id,
      taskId: node.task_id || node.maze_task_id,
      label: node.label || node.name || node.task_name,
      taskPath: node.task_path || node.taskPath,
      functionName: node.function_name || node.functionName,
      error: compactAgentDiagnosticText(node.error || node.last_error || '', 1000),
    }));
}

async function buildFixTaskContext(context, source, failedNodes, maxCodeChars) {
  const byKey = new Map();
  for (const definition of source?.taskDefinitions || []) {
    const key = taskDefinitionKey(definition.relativePath, definition.functionName);
    byKey.set(key, {
      relativePath: definition.relativePath,
      functionName: definition.functionName,
      displayName: definition.displayName,
      inputs: definition.inputs || [],
      outputs: definition.outputs || [],
      resources: definition.resources || {},
      code: String(definition.code || '').slice(0, maxCodeChars),
      truncated: String(definition.code || '').length > maxCodeChars,
    });
  }

  const related = [];
  for (const failed of failedNodes) {
    if (!failed.taskPath) continue;
    const key = taskDefinitionKey(failed.taskPath, failed.functionName || '');
    let definition = byKey.get(key) || Array.from(byKey.values()).find((item) => item.relativePath === failed.taskPath);
    if (!definition) {
      try {
        const task = await readWorkspaceTaskForAgent(context, {
          relativePath: failed.taskPath,
          maxChars: maxCodeChars,
        });
        definition = {
          relativePath: task.relativePath,
          functionName: failed.functionName,
          code: task.code,
          truncated: task.truncated,
        };
      } catch (error) {
        definition = {
          relativePath: failed.taskPath,
          functionName: failed.functionName,
          error: compactAgentDiagnosticText(error.message || error, 500),
        };
      }
    }
    related.push({
      failedNode: failed,
      task: definition,
    });
  }
  return related;
}

async function createFixDraftFromRunForAgent(context, input = {}) {
  const runId = String(input.runId || input.run_id || '').trim();
  if (!runId) {
    throw new Error('runId is required');
  }
  if (runId.includes('/') || runId.includes('\\')) {
    throw new Error(`Invalid workflow run id: ${runId}`);
  }

  const maxCodeChars = Math.min(Math.max(Number(input.maxCodeChars || 12000), 0), 50000);
  const run = await loadStaticRun(context.workspaceDir, runId).catch((error) => {
    const wrapped = new Error(`Static workflow run not found: ${runId}`);
    wrapped.status = error.code === 'ENOENT' ? 404 : 500;
    throw wrapped;
  });
  const events = await loadStaticRunEvents(context.workspaceDir, runId).catch(() => []);
  const rawNodes = Object.values(run.task_nodes || {});
  const failedNodes = failedRunNodes(run);
  const source = await findWorkspaceWorkflowByIdOrPath(context, {
    workflowId: run.workflow_id,
    workflowRelativePath: input.workflowRelativePath,
    workflowName: run.workflow_name,
  });
  if (!source) {
    const error = new Error('Could not find the saved workflow for this run. Pass workflowRelativePath to create a fix draft.');
    error.status = 404;
    error.code = 'WORKFLOW_SOURCE_NOT_FOUND';
    throw error;
  }

  const suffix = runId.slice(0, 8);
  const draftRelativePath = input.draftRelativePath
    || source.relativePath.replace(/\.json$/i, `-fix-${suffix}.json`);
  const draft = await createAgentWorkflowDraft(context, {
    name: input.name || `${source.workflow.name} Fix ${suffix}`,
    relativePath: draftRelativePath,
    description: `Fix draft for failed run ${runId} from ${source.relativePath}.`,
    nodes: source.workflow.nodes,
    edges: source.workflow.edges,
    taskDefinitions: source.taskDefinitions || [],
  });
  const taskContext = await buildFixTaskContext(context, source, failedNodes, maxCodeChars);
  const guidance = collectAgentRunIssues(run, rawNodes, events);
  const fixContext = {
    runId,
    status: inferAgentDiagnosticStatus(run, rawNodes),
    sourceWorkflow: {
      relativePath: source.relativePath,
      workflowId: run.workflow_id,
      workflowName: run.workflow_name || source.workflow.name,
    },
    failedNodes,
    guidance,
    relatedTasks: taskContext,
  };
  const persistedDraft = await loadAgentDraft(context.workspaceDir, draft.id);
  persistedDraft.fixContext = fixContext;
  persistedDraft.description = persistedDraft.description || `Fix draft for failed run ${runId}.`;
  await writeAgentDraft(context.workspaceDir, persistedDraft);
  const publicDraft = agentDraftPublic(persistedDraft);

  return {
    ok: true,
    draft: publicDraft,
    run: await buildStaticRunDiagnostic(context, run, {
      nodeLimit: 8,
      eventLimit: 8,
      artifactLimit: 8,
    }),
    nextStep: 'Use update_workflow_draft with this draft id to edit the failing task or workflow structure. Do not save or run until the user confirms.',
  };
}

async function executeAgentTool(context, name, input = {}, runtime = {}) {
  if (name === 'list_workspace_items') {
    const include = Array.isArray(input.include) && input.include.length
      ? new Set(input.include)
      : new Set(['workflows', 'tasks', 'skills']);
    const result = {};
    if (include.has('workflows')) {
      const files = await listWorkflowFiles(path.join(context.workspaceDir, 'workflows'));
      result.workflows = [];
      for (const filePath of files.slice(0, 80)) {
        try {
          const payload = JSON.parse(await fs.readFile(filePath, 'utf-8'));
          const workflow = normalizeWorkflowPayload(payload);
          const stat = await fs.stat(filePath);
          result.workflows.push({
            name: workflow.name,
            relativePath: toPosixPath(path.relative(context.workspaceDir, filePath)),
            nodeCount: workflow.nodes.length,
            edgeCount: workflow.edges.length,
            updatedAt: stat.mtime.toISOString(),
          });
        } catch (error) {
          result.workflows.push({
            relativePath: toPosixPath(path.relative(context.workspaceDir, filePath)),
            error: error.message,
          });
        }
      }
    }
    if (include.has('tasks')) {
      const tasks = await callPython('get_workspace_tasks', { workspaceDir: context.workspaceDir });
      result.tasks = (tasks.tasks || []).slice(0, 120).map((task) => ({
        displayName: task.displayName,
        functionName: task.functionName,
        relativePath: task.relativePath,
        inputs: task.inputs || [],
        outputs: task.outputs || [],
      }));
      if (tasks.errors?.length) result.taskErrors = tasks.errors;
    }
    if (include.has('skills')) {
      const skills = await callPython('list_workspace_skills', { workspaceDir: context.workspaceDir });
      result.skills = (skills.skills || []).slice(0, 80).map((skill) => ({
        name: skill.name,
        path: skill.path,
        description: skill.description,
      }));
      if (skills.errors?.length) result.skillErrors = skills.errors;
    }
    if (include.has('files')) {
      const listed = await listWorkspaceFilesForAgent(context, { maxEntries: 100 });
      result.files = listed.files;
      result.filesTruncated = listed.truncated;
    }
    return { ok: true, ...result };
  }

  if (name === 'read_current_workflow') {
    return {
      ok: true,
      currentWorkflow: redactSecrets(runtime.currentWorkflow || null),
    };
  }

  if (name === 'read_workspace_workflow') {
    return readWorkspaceWorkflowForAgent(context, input);
  }

  if (name === 'read_workspace_task') {
    return readWorkspaceTaskForAgent(context, input);
  }

  if (name === 'list_workspace_files') {
    return listWorkspaceFilesForAgent(context, input);
  }

  if (name === 'read_workspace_file') {
    return readWorkspaceFileForAgent(context, input);
  }

  if (name === 'create_workflow_draft') {
    const draft = await createAgentWorkflowDraft(context, input);
    return { ok: true, draft };
  }

  if (name === 'clone_workflow_to_draft') {
    const draft = await cloneWorkspaceWorkflowToDraft(context, input);
    return { ok: true, draft };
  }

  if (name === 'update_workflow_draft') {
    const draft = await updateAgentWorkflowDraft(context, input.draftId, input);
    return { ok: true, draft };
  }

  if (name === 'validate_workflow_draft') {
    const draft = await validateAgentWorkflowDraft(context, input.draftId);
    return { ok: true, draft };
  }

  if (name === 'inspect_recent_run_errors') {
    const limit = Math.min(Math.max(Number(input.limit || 8), 1), 30);
    const detail = input.detail !== false;
    const staticRuns = (await listStaticRunFilesForWorkspace(context.workspaceDir, { summary: true }))
      .filter((run) => run.status === 'failed' || run.error)
      .slice(0, limit)
      .sort((left, right) => Number(right.updated_time || 0) - Number(left.updated_time || 0));
    let dynamicRuns = [];
    try {
      const payload = await callMazeCore('/runs?limit=30&detail=false');
      dynamicRuns = (payload.runs || [])
        .filter((run) => run?.metadata?.benchmark !== 'gaia')
        .filter((run) => run.status === 'failed' || run.error_summary || run.failure_reason)
        .slice(0, limit)
        .sort((left, right) => Number(right.updated_time || 0) - Number(left.updated_time || 0));
    } catch (error) {
      dynamicRuns = [{ kind: 'dynamic', run_id: '', status: 'unavailable', error_summary: error.message }];
    }
    const combined = [
      ...staticRuns.map((run) => ({ kind: 'static', run })),
      ...dynamicRuns.map((run) => ({ kind: run.kind || run.run_type || 'dynamic', run })),
    ]
      .sort((left, right) => Number(right.run.updated_time || 0) - Number(left.run.updated_time || 0))
      .slice(0, limit);

    if (!detail) {
      return {
        ok: true,
        runs: combined.map(({ kind, run }) => ({
          kind,
          runId: run.run_id,
          workflowName: run.workflow_name,
          status: run.status,
          error: compactAgentDiagnosticText(run.error || run.error_summary || run.failure_reason || '', 700),
          updatedTime: run.updated_time,
        })),
      };
    }

    const diagnostics = [];
    for (const item of combined) {
      if (item.kind === 'static') {
        diagnostics.push(await buildStaticRunDiagnostic(context, item.run));
      } else if (item.run.run_id) {
        diagnostics.push(await buildDynamicRunDiagnostic(item.run));
      } else {
        diagnostics.push({
          kind: 'dynamic',
          status: item.run.status,
          error: compactAgentDiagnosticText(item.run.error_summary || item.run.error || '', 700),
          guidance: collectAgentRunIssues(item.run, [], []),
        });
      }
    }
    return { ok: true, runs: diagnostics };
  }

  if (name === 'inspect_workflow_run') {
    return inspectWorkflowRunForAgent(context, input);
  }

  if (name === 'create_fix_draft_from_run') {
    return createFixDraftFromRunForAgent(context, input);
  }

  if (name === 'promote_run_artifact') {
    const result = await promoteArtifactIntoWorkspace(context, {
      runId: input.runId || input.run_id,
      taskId: input.taskId || input.task_id || input.nodeId || input.node_id,
      path: input.path || input.artifactPath || input.artifact_path,
      targetPath: input.targetPath || input.target_path,
      overwrite: input.overwrite !== false,
    });
    return {
      ok: true,
      file: result.file,
      workspaceId: result.workspaceId,
      workspaceDir: result.workspaceDir,
      workspaceManifestVersion: result.workspaceManifestVersion,
      nextStep: 'Use list_workspace_files or read_workspace_file if you need to build a follow-up workflow that consumes this promoted file.',
    };
  }

  return { ok: false, error: `Unknown Workspace Agent tool: ${name}` };
}

function workspaceAgentSystemPrompt(context) {
  return [
    'You are the Maze Workspace Agent inside Maze Playground.',
    'Your job is to turn user intent into practical Maze workflow progress.',
    'Use tools proactively whenever they can advance the user request; do not only describe an action you can perform with a tool.',
    'If you say you will inspect, draft, validate, fix, promote, or update something, call the corresponding tool in the same assistant turn.',
    'Save and Run are explicit UI commands and are not agent tools. Never claim that you saved or ran a draft; create or update the draft and leave confirmation to the user.',
    'Only answer with text alone when you are explaining, asking for genuinely missing information, or leaving Save/Run confirmation to the UI.',
    'Create workflow drafts instead of overwriting saved workflows.',
    'When the user references an existing saved workflow or task path, read it with read_workspace_workflow or read_workspace_task before drafting changes.',
    'When the user wants to revise a saved workflow, prefer clone_workflow_to_draft first, then update_workflow_draft for changes.',
    'When the user references uploaded data or workspace files, list/read workspace files under workspace/files before proposing tasks that consume them.',
    'When the user asks to revise or extend an existing draft, use update_workflow_draft with the existing draftId instead of creating a new draft.',
    'When the user asks about a specific workflow run id, use inspect_workflow_run before explaining status, results, artifacts, or errors.',
    'When the user asks to fix a failed workflow run, use create_fix_draft_from_run to create a safe repair draft, then update_workflow_draft with concrete changes.',
    'When the user asks to reuse or save a run artifact into the workspace, inspect the run first if needed, then use promote_run_artifact with the artifact path.',
    'After promote_run_artifact, use the returned file.relativePath with read_workspace_file before creating a downstream workflow draft from that file.',
    'For downstream tasks that consume workspace files, pass the file relative path as a user input value, for example "reports/output.json"; do not use "workspace/files/..." or "files/..." in workflow input values.',
    'Do not attempt to save or run a draft.',
    'When creating workspace task definitions, generate safe Python Maze tasks using `from maze import task`, one @task function per file, no secrets, no absolute paths, no subprocess, no shell, no package installation, and no network calls.',
    'Use only `@task` or `@task(resources={...})`; never use `@task(inputs=...)`, `@task(outputs=...)`, or inputs/outputs decorator arguments. Maze infers inputs from function parameters and outputs from returned dict keys.',
    'For workspace workflow nodes, use category="workspace", nodeType="task", taskPath, functionName, inputs, outputs, resources, configured=true.',
    'Use user input values by setting each input item as {name, dataType, source:"user", value:"..."} when appropriate.',
    'For downstream node inputs, use {name, dataType, source:"task", taskSource:{taskId:"upstream-node-id", outputKey:"upstream_output_key"}}.',
    'If a draft is created, explain what it contains and tell the user they can Preview, Save, or Run it from the draft card.',
    `Workspace: ${context.workspaceId} (${context.workspaceDir})`,
  ].join('\n');
}

function createWorkspaceAgentCapability(context, runtime = {}) {
  const token = crypto.randomBytes(32).toString('hex');
  workspaceAgentCapabilities.set(token, {
    context,
    runtime: {
      currentWorkflow: redactSecrets(runtime.currentWorkflow || null),
      sessionId: runtime.sessionId || null,
    },
    runId: null,
    tools: new Set(agentToolDefinitions().map((tool) => tool.function.name)),
  });
  return token;
}

function bindWorkspaceAgentCapability(token, runId) {
  const capability = workspaceAgentCapabilities.get(token);
  if (capability) capability.runId = String(runId || '');
}

function revokeWorkspaceAgentCapabilities(runId) {
  const target = String(runId || '');
  if (!target) return;
  for (const [token, capability] of workspaceAgentCapabilities) {
    if (capability.runId === target) workspaceAgentCapabilities.delete(token);
  }
}

function workspaceAgentCapability(req) {
  const address = String(req.socket?.remoteAddress || '');
  if (!['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(address)) return null;
  const authorization = String(req.get('authorization') || '');
  if (!authorization.startsWith('Bearer ')) return null;
  return workspaceAgentCapabilities.get(authorization.slice(7).trim()) || null;
}

function workspaceAgentToolUrl() {
  return 'http://127.0.0.1:' + PORT + '/api/internal/workspace-agent/tool';
}

async function runWorkspaceAgent(context, input = {}) {
  const message = redactSecretText(String(input.message || '').trim()).slice(0, 12000);
  if (!message) {
    const error = new Error('message is required');
    error.status = 400;
    throw error;
  }

  const llm = input.llm || {};
  if (!String(llm.baseUrl || '').trim() || !String(llm.model || '').trim() || !String(llm.apiKey || '').trim()) {
    const error = new Error('LLM base URL, API key, and model are required');
    error.status = 400;
    throw error;
  }

  let session = input.sessionId
    ? await loadAgentSession(context.workspaceDir, input.sessionId).catch(() => null)
    : null;
  if (!session) {
    session = await createAgentSessionRecord(context, {
      title: input.title || message.slice(0, 60),
    });
  }

  const recentTurns = normalizeAgentTurns(session.turns).slice(-8);
  const history = await loadAgentSessionView(context, session, {
    turns: recentTurns,
    includeDrafts: false,
  });
  const prompt = buildWorkspaceAgentPrompt(message, history.messages, session.summary);
  const capabilityToken = createWorkspaceAgentCapability(context, {
    currentWorkflow: input.currentWorkflow,
    sessionId: session.id,
  });
  const timeoutSeconds = Math.min(
    Math.max(Math.ceil(Number(input.timeoutMs || 180000) / 1000), 30),
    600,
  );

  let started;
  try {
    started = await startReactWorkflowProcess(
      {
        mode: 'workspace-agent',
        prompt,
        workspaceAgentMessage: message,
        workspaceId: context.workspaceId,
        workspaceDir: context.workspaceDir,
        workspaceManifestVersion: context.workspaceManifestVersion,
        workspaceAgentTools: agentToolDefinitions(),
        workspaceAgentToolUrl: workspaceAgentToolUrl(),
        permissionPolicy: {
          mcp: Object.fromEntries(agentToolDefinitions().map((tool) => [tool.function.name, 'allow'])),
          skill: { '*': 'allow' },
        },
        systemPrompt: workspaceAgentSystemPrompt(context),
        maxSteps: Math.min(Math.max(Number(input.maxSteps || 8), 1), 16),
        maxTokens: Math.min(Math.max(Number(input.maxTokens || 2048), 1), 32768),
        taskTimeout: timeoutSeconds,
        baseUrl: String(llm.baseUrl).trim(),
        model: String(llm.model).trim(),
      },
      {
        MAZE_REACT_API_KEY: String(llm.apiKey),
        MAZE_WORKSPACE_AGENT_TOOL_TOKEN: capabilityToken,
      },
      () => workspaceAgentCapabilities.delete(capabilityToken),
    );
  } catch (error) {
    workspaceAgentCapabilities.delete(capabilityToken);
    throw error;
  }

  bindWorkspaceAgentCapability(capabilityToken, started.runId);

  try {
    await appendAgentSessionTurn(context.workspaceDir, session, started.runId);
  } catch (error) {
    workspaceAgentCapabilities.delete(capabilityToken);
    await callMazeCore('/runs/' + encodeURIComponent(started.runId) + '/cancel', {
      method: 'POST',
      body: { reason: 'Workspace Agent session persistence failed' },
    }).catch(() => null);
    throw error;
  }

  const savedSession = await loadAgentSession(context.workspaceDir, session.id);
  const [view, dynamicTurn] = await Promise.all([
    loadAgentSessionView(context, savedSession),
    loadAgentDynamicTurn(savedSession.id, normalizeAgentTurns(savedSession.turns).at(-1)),
  ]);
  return {
    success: true,
    run: {
      ...(dynamicTurn.run || {}),
      id: started.runId,
      dynamic_run_id: started.runId,
      status: dynamicTurn.run?.status || started.status || 'running',
    },
    session: agentSessionSummary(savedSession),
    messages: view.messages,
    drafts: view.drafts,
    events: dynamicTurn.events,
  };
}
function redactMcpServerConfig(server) {
  if (!server || typeof server !== 'object' || Array.isArray(server)) return {};
  const redacted = { ...server };
  if (redacted.env && typeof redacted.env === 'object' && !Array.isArray(redacted.env)) {
    redacted.env = Object.fromEntries(Object.entries(redacted.env).map(([key, value]) => [
      key,
      mcpStringHasEnvRefs(value) ? String(value) : '<hidden>',
    ]));
  }
  if (redacted.headers && typeof redacted.headers === 'object' && !Array.isArray(redacted.headers)) {
    redacted.headers = Object.fromEntries(Object.entries(redacted.headers).map(([key, value]) => [
      key,
      mcpStringHasEnvRefs(value) ? String(value) : '<hidden>',
    ]));
  }
  return redacted;
}

const MCP_ENV_REF_PATTERN = /\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g;

function mcpStringHasEnvRefs(value) {
  return typeof value === 'string' && /\$\{[A-Za-z_][A-Za-z0-9_]*\}/.test(value);
}

function collectMcpEnvRefs(value, refs = new Set()) {
  if (typeof value === 'string') {
    for (const match of value.matchAll(/\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g)) {
      refs.add(match[1]);
    }
    return refs;
  }
  if (Array.isArray(value)) {
    value.forEach((item) => collectMcpEnvRefs(item, refs));
    return refs;
  }
  if (value && typeof value === 'object') {
    Object.values(value).forEach((item) => collectMcpEnvRefs(item, refs));
  }
  return refs;
}

function expandMcpEnvRefsInString(value, { profileName = '', serverName = '', fieldName = '' } = {}) {
  if (typeof value !== 'string') return value;
  return value.replace(/\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (match, envName) => {
    if (process.env[envName] === undefined) {
      const scope = [
        profileName ? `profile "${profileName}"` : 'inline MCP config',
        serverName ? `server "${serverName}"` : '',
        fieldName ? `field "${fieldName}"` : '',
      ].filter(Boolean).join(', ');
      const error = new Error(`MCP env reference ${match} is not set${scope ? ` (${scope})` : ''}`);
      error.status = 400;
      error.missingEnv = envName;
      throw error;
    }
    return process.env[envName];
  });
}

function expandMcpEnvRefsInMap(mapValue, options = {}) {
  if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) return mapValue;
  return Object.fromEntries(Object.entries(mapValue).map(([key, value]) => [
    key,
    expandMcpEnvRefsInString(String(value ?? ''), { ...options, fieldName: `${options.fieldName || 'map'}.${key}` }),
  ]));
}

function expandMcpServersEnvRefs(servers = [], { profileName = '' } = {}) {
  return servers.map((server) => {
    const serverName = String(server?.name || '');
    return {
      ...server,
      env: expandMcpEnvRefsInMap(server.env, { profileName, serverName, fieldName: 'env' }),
      headers: expandMcpEnvRefsInMap(server.headers, { profileName, serverName, fieldName: 'headers' }),
    };
  });
}

function mcpProfileEnvRefSummary(servers = []) {
  const refs = collectMcpEnvRefs(servers);
  return {
    usesEnvRefs: refs.size > 0,
    envRefCount: refs.size,
    envRefs: Array.from(refs).sort(),
  };
}

function summarizeMcpProfile(profile) {
  const servers = Array.isArray(profile?.mcpServers) ? profile.mcpServers : [];
  const envRefs = mcpProfileEnvRefSummary(servers);
  const lastTest = profile?.lastTest && typeof profile.lastTest === 'object'
    ? {
        status: profile.lastTest.status || null,
        testedAt: profile.lastTest.testedAt || null,
        serverCount: profile.lastTest.serverCount ?? null,
        toolCount: profile.lastTest.toolCount ?? null,
        tools: Array.isArray(profile.lastTest.tools) ? profile.lastTest.tools : [],
        error: profile.lastTest.error || undefined,
        errorType: profile.lastTest.errorType || undefined,
      }
    : null;
  return {
    name: String(profile?.name || ''),
    description: String(profile?.description || ''),
    createdAt: profile?.createdAt || null,
    updatedAt: profile?.updatedAt || null,
    serverCount: servers.length,
    toolCount: lastTest?.status === 'ok' ? Number(lastTest.toolCount || 0) : 0,
    lastTest,
    ...envRefs,
    servers: summarizeMcpServers(servers),
    redactedMcpServers: servers.map(redactMcpServerConfig),
  };
}

async function loadMcpProfile(workspaceDir, name) {
  const profileName = safeMcpProfileName(name);
  const profile = await readJsonFile(mcpProfilePath(workspaceDir, profileName), null);
  if (!profile) {
    const error = new Error(`MCP profile not found: ${profileName}`);
    error.status = 404;
    throw error;
  }
  if (!Array.isArray(profile.mcpServers)) {
    throw new Error(`MCP profile ${profileName} is missing mcpServers`);
  }
  return {
    ...profile,
    name: profileName,
  };
}

async function listMcpProfiles(workspaceDir) {
  const dir = mcpProfilesDir(workspaceDir);
  const entries = await fs.readdir(dir, { withFileTypes: true }).catch(() => []);
  const profiles = [];
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith('.json')) continue;
    try {
      const profile = await readJsonFile(path.join(dir, entry.name), null);
      if (profile) {
        profiles.push(summarizeMcpProfile({
          ...profile,
          name: path.basename(entry.name, '.json'),
        }));
      }
    } catch {
      // Ignore malformed profile files in the list view.
    }
  }
  profiles.sort((a, b) => a.name.localeCompare(b.name));
  return profiles;
}

async function resolveMcpServersForRequest(context, { mcpServers, mcpProfileName } = {}) {
  const profileName = mcpProfileName ? safeMcpProfileName(mcpProfileName) : '';
  if (profileName) {
    const profile = await loadMcpProfile(context.workspaceDir, profileName);
    const normalized = validateMcpServers(profile.mcpServers);
    const expanded = expandMcpServersEnvRefs(normalized, { profileName });
    return {
      mcpServers: expanded,
      profileName,
      profileSummary: summarizeMcpProfile({ ...profile, mcpServers: normalized }),
    };
  }
  return {
    mcpServers: expandMcpServersEnvRefs(validateMcpServers(mcpServers)),
    profileName: '',
    profileSummary: null,
  };
}

function summarizeMcpDiscoveredTools(tools = []) {
  if (!Array.isArray(tools)) return [];
  return tools.slice(0, 80).map((tool) => ({
    server: tool?.server || '',
    tool: tool?.tool || '',
    agent_tool: tool?.agent_tool || '',
    description: String(tool?.description || '').slice(0, 300),
    required_inputs: Array.isArray(tool?.required_inputs) ? tool.required_inputs.slice(0, 20) : [],
  }));
}

async function updateMcpProfileLastTest(workspaceDir, profileName, lastTest) {
  if (!profileName) return null;
  const safeName = safeMcpProfileName(profileName);
  const profile = await loadMcpProfile(workspaceDir, safeName);
  const updated = {
    ...profile,
    updatedAt: profile.updatedAt || new Date().toISOString(),
    lastTest,
  };
  await writeJsonAtomic(mcpProfilePath(workspaceDir, safeName), updated);
  return summarizeMcpProfile(updated);
}

function buildMcpProfileExport(profile) {
  return {
    schema: 'maze_mcp_profile_export',
    schema_version: 1,
    exportedAt: new Date().toISOString(),
    name: String(profile?.name || ''),
    description: String(profile?.description || ''),
    redacted: true,
    mcpServers: (Array.isArray(profile?.mcpServers) ? profile.mcpServers : []).map(redactMcpServerConfig),
    profile: summarizeMcpProfile(profile),
  };
}

function rejectRedactedMcpPlaceholders(servers = []) {
  const serialized = JSON.stringify(servers || []);
  if (serialized.includes('"<hidden>"')) {
    const error = new Error('Replace <hidden> values before importing this MCP profile');
    error.status = 400;
    throw error;
  }
}

function sameJsonValue(left, right) {
  return JSON.stringify(left ?? null) === JSON.stringify(right ?? null);
}

function withStaticRunWriteQueue(workspaceDir, runId, operation) {
  const key = `${workspaceDir}::${runId}`;
  const previous = staticRunWriteQueues.get(key) || Promise.resolve();
  const current = previous
    .catch(() => {})
    .then(operation);
  const tail = current.then(
    () => undefined,
    () => undefined,
  );

  staticRunWriteQueues.set(key, tail);
  tail.finally(() => {
    if (staticRunWriteQueues.get(key) === tail) {
      staticRunWriteQueues.delete(key);
    }
  });

  return current;
}

function withSystemWorkflowLoadQueue(workspaceDir, operation) {
  const key = path.resolve(workspaceDir);
  const previous = systemWorkflowLoadQueues.get(key) || Promise.resolve();
  const current = previous
    .catch(() => {})
    .then(operation);
  const tail = current.then(
    () => undefined,
    () => undefined,
  );

  systemWorkflowLoadQueues.set(key, tail);
  tail.finally(() => {
    if (systemWorkflowLoadQueues.get(key) === tail) {
      systemWorkflowLoadQueues.delete(key);
    }
  });

  return current;
}

async function saveStaticRun(workspaceDir, snapshot) {
  await writeJsonAtomic(
    staticRunPath(workspaceDir, snapshot.run_id, { write: true }),
    {
      ...snapshot,
      workspace_id: snapshot.workspace_id || workspaceIdFromDir(workspaceDir),
    },
    isGaiaTrace(snapshot) ? { mode: 0o600 } : {},
  );
}

async function loadStaticRun(workspaceDir, runId) {
  const raw = await fs.readFile(staticRunPath(workspaceDir, runId), 'utf-8');
  const snapshot = JSON.parse(raw);
  const events = await loadStaticRunEvents(workspaceDir, runId);
  return replayStaticRunSnapshot(snapshot, events);
}

function staticRunSummary(snapshot) {
  const summary = {
    schema: snapshot.schema || 'static_workflow_run',
    schema_version: snapshot.schema_version || 1,
    kind: 'static',
    summary: true,
    run_id: snapshot.run_id,
    workflow_id: snapshot.workflow_id,
    workflow_name: snapshot.workflow_name || 'Workflow Run',
    workspace_dir: snapshot.workspace_dir,
    workspace_id: snapshot.workspace_id,
    workspace_manifest_version: snapshot.workspace_manifest_version,
    status: snapshot.status,
    created_time: snapshot.created_time,
    updated_time: snapshot.updated_time,
    finished_time: snapshot.finished_time,
    task_counts: snapshot.task_counts || {},
    events: snapshot.events || { count: 0, last_seq: 0 },
    error: snapshot.error || null,
    maze_run_id: snapshot.maze_run_id || null,
    metadata: snapshot.metadata || {},
    final_result: snapshot.final_result && typeof snapshot.final_result === 'object'
      ? {
          status: snapshot.final_result.status,
          answer: snapshot.final_result.answer,
          stop_reason: snapshot.final_result.stop_reason,
        }
      : snapshot.final_result,
  };
  return publicStaticRunSnapshot(summary);
}

async function appendStaticRunEvent(workspaceDir, runId, event) {
  const runDir = staticRunDir(workspaceDir, runId);
  await fs.mkdir(runDir, { recursive: true });
  const eventsPath = staticRunEventsPath(workspaceDir, runId);
  await fs.appendFile(
    eventsPath,
    `${JSON.stringify(event)}\n`,
    { encoding: 'utf-8', mode: 0o600 },
  );
  await fs.chmod(eventsPath, 0o600);
}

async function loadStaticRunEvents(workspaceDir, runId, after = null) {
  const filePath = staticRunEventsPath(workspaceDir, runId);
  const raw = await fs.readFile(filePath, 'utf-8').catch((error) => {
    if (error.code === 'ENOENT') return '';
    throw error;
  });
  return raw
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => JSON.parse(line))
    .filter((event) => after === null || Number(event.seq || 0) > Number(after));
}

function replayStaticRunSnapshot(snapshot, events = []) {
  let count = Number(snapshot.events?.count || 0);
  let lastSeq = Number(snapshot.events?.last_seq || 0);
  for (const event of events) {
    const eventSeq = Number(event.seq || 0);
    if (!eventSeq || eventSeq <= lastSeq) continue;
    applyStaticRunEvent(snapshot, event);
    count += 1;
    lastSeq = eventSeq;
    snapshot.updated_time = Math.max(
      Number(snapshot.updated_time || 0),
      Date.parse(event.timestamp || '') / 1000 || 0,
    );
  }
  snapshot.events = { count, last_seq: lastSeq };
  return snapshot;
}

async function appendAndApplyStaticRunEventUnlocked(workspaceDir, runId, incomingEvent) {
  const snapshot = await loadStaticRun(workspaceDir, runId);
  const nextSeq = Number(snapshot.events?.last_seq || 0) + 1;
  const event = {
    ...incomingEvent,
    schema_version: 1,
    seq: incomingEvent.seq || nextSeq,
    timestamp: incomingEvent.timestamp || new Date().toISOString(),
  };
  event.data = {
    ...(event.data || {}),
    workflow_run_id: runId,
  };

  applyStaticRunEvent(snapshot, event);
  snapshot.events = {
    count: Number(snapshot.events?.count || 0) + 1,
    last_seq: Number(event.seq || nextSeq),
  };
  snapshot.updated_time = nowEpochSeconds();

  await appendStaticRunEvent(workspaceDir, runId, event);
  await saveStaticRun(workspaceDir, snapshot);
  return { snapshot, event };
}

async function appendAndApplyStaticRunEvent(workspaceDir, runId, incomingEvent) {
  return withStaticRunWriteQueue(
    workspaceDir,
    runId,
    () => appendAndApplyStaticRunEventUnlocked(workspaceDir, runId, incomingEvent),
  );
}

function applyStaticRunEvent(snapshot, event) {
  const data = event.data || {};
  const nodeId = data.node_id;
  const node = nodeId ? snapshot.task_nodes?.[nodeId] : null;
  const eventTime = Date.parse(event.timestamp || '') / 1000 || nowEpochSeconds();

  if (event.type === 'workflow_started') {
    snapshot.status = 'running';
  } else if (event.type === 'workflow_completed') {
    if (snapshot.status !== 'failed') {
      snapshot.status = 'completed';
      snapshot.finished_time = snapshot.finished_time || eventTime;
      snapshot.final_result = data.results ?? snapshot.final_result;
    }
  } else if (event.type === 'workflow_failed') {
    snapshot.status = 'failed';
    snapshot.finished_time = snapshot.finished_time || eventTime;
    snapshot.error = compactAgentDiagnosticText(data.error || 'Workflow failed', 2000);
  } else if (event.type === 'workflow_canceled') {
    snapshot.status = 'canceled';
    snapshot.finished_time = snapshot.finished_time || eventTime;
    snapshot.error = data.error || data.message || 'Workflow run was canceled';
  } else if (event.type === 'workflow_interrupted') {
    snapshot.status = 'interrupted';
    snapshot.finished_time = snapshot.finished_time || eventTime;
    snapshot.error = data.error || data.message || 'Workflow run was interrupted';
  } else if (event.type === 'start_task' && node) {
    node.status = 'running';
    node.started_time = node.started_time || eventTime;
    node.maze_task_id = data.maze_task_id || data.task_id || node.maze_task_id;
    if (data.node_ip) {
      node.node_ip = data.node_ip;
    }
    if (data.node_id) {
      node.node_id_runtime = data.node_id;
    }
    if (data.gpu_id !== undefined && data.gpu_id !== null) {
      node.gpu_id = data.gpu_id;
    }
  } else if (event.type === 'finish_task' && node) {
    node.status = 'completed';
    node.finished_time = eventTime;
    node.maze_task_id = data.maze_task_id || data.task_id || node.maze_task_id;
    if (data.node_ip) {
      node.node_ip = data.node_ip;
    }
    if (data.node_id) {
      node.node_id_runtime = data.node_id;
    }
    if (data.gpu_id !== undefined && data.gpu_id !== null) {
      node.gpu_id = data.gpu_id;
    }
    node.result_summary = data.result ?? node.result_summary;
    if (data.file_manifest) {
      node.file_manifest = data.file_manifest;
      node.artifacts = data.file_manifest.files || [];
    }
  } else if (event.type === 'task_exception' && node) {
    node.status = 'failed';
    node.finished_time = eventTime;
    node.error = compactAgentDiagnosticText(data.error || data.result || 'Task failed', 2000);
    node.maze_task_id = data.maze_task_id || data.task_id || node.maze_task_id;
    snapshot.status = 'failed';
    snapshot.finished_time = snapshot.finished_time || eventTime;
    snapshot.error = node.error;
  } else if (event.type === 'benchmark_submission_started') {
    if (snapshot.gaia_private) {
      snapshot.gaia_private.submission_state = 'submitting';
    }
  } else if (event.type === 'maze_run_created') {
    snapshot.maze_run_id = data.maze_run_id || snapshot.maze_run_id;
    if (snapshot.metadata?.benchmark === 'gaia') {
      snapshot.metadata.maze_run_id = snapshot.maze_run_id;
      if (snapshot.gaia_private) {
        snapshot.gaia_private.submission_state = 'bound';
      }
    }
  } else if (
    Object.values(GAIA_TERMINAL_EVENTS).some(({ type }) => type === event.type)
    && !TERMINAL_STATIC_RUN_STATUSES.has(snapshot.status)
  ) {
    const terminal = Object.values(GAIA_TERMINAL_EVENTS).find(({ type }) => type === event.type);
    snapshot.status = terminal.status;
    snapshot.finished_time = snapshot.finished_time || eventTime;
  } else if (event.type === 'benchmark_submission_failed' && !TERMINAL_STATIC_RUN_STATUSES.has(snapshot.status)) {
    snapshot.status = 'failed';
    snapshot.finished_time = snapshot.finished_time || eventTime;
    if (snapshot.gaia_private) {
      snapshot.gaia_private.submission_state = 'failed';
    }
  }

  recomputeStaticRunTaskCounts(snapshot);
}

async function listStaticRunFiles(dir, options = {}) {
  const summary = Boolean(options.summary);
  const entries = await fs.readdir(dir, { withFileTypes: true }).catch(() => []);
  const runs = [];
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const runPath = path.join(dir, entry.name, 'run.json');
    try {
      const raw = await fs.readFile(runPath, 'utf-8');
      const eventsRaw = await fs.readFile(path.join(dir, entry.name, 'events.jsonl'), 'utf-8').catch((error) => {
        if (error.code === 'ENOENT') return '';
        throw error;
      });
      const events = eventsRaw
        .split(/\r?\n/)
        .filter(Boolean)
        .map((line) => JSON.parse(line));
      const snapshot = replayStaticRunSnapshot(JSON.parse(raw), events);
      runs.push(summary ? staticRunSummary(snapshot) : snapshot);
    } catch {
      // Ignore malformed run records in the list view.
    }
  }
  return runs;
}

async function listStaticRunFilesForWorkspace(workspaceDir, options = {}) {
  const seen = new Set();
  const runs = [];
  for (const dir of staticRunSearchDirs(workspaceDir)) {
    const items = await listStaticRunFiles(dir, options);
    for (const item of items) {
      if (!item.run_id || seen.has(item.run_id)) {
        continue;
      }
      seen.add(item.run_id);
      runs.push(item);
    }
  }
  return runs;
}

function defaultArtifactStoreRoot() {
  return path.resolve(process.env.MAZE_ARTIFACT_STORE_DIR || path.join(os.homedir(), '.maze', 'artifacts'));
}

function artifactBlobPath(sha256) {
  const sha = String(sha256 || '').trim().toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(sha)) {
    throw new Error(`Invalid sha256: ${sha256}`);
  }
  return path.join(defaultArtifactStoreRoot(), 'blobs', sha.slice(0, 2), sha.slice(2, 4), sha);
}

function collectRunReferencedSha256(run) {
  const referenced = new Set();
  for (const node of Object.values(run?.task_nodes || {})) {
    for (const artifact of node?.file_manifest?.files || []) {
      if (artifact?.sha256) {
        referenced.add(String(artifact.sha256).toLowerCase());
      }
    }
    for (const artifact of node?.artifacts || []) {
      if (artifact?.sha256) {
        referenced.add(String(artifact.sha256).toLowerCase());
      }
    }
  }
  return referenced;
}

async function collectWorkspaceReferencedSha256(workspaceDir) {
  const referenced = new Set();
  for (const run of await listStaticRunFilesForWorkspace(workspaceDir)) {
    for (const sha of collectRunReferencedSha256(run)) {
      referenced.add(sha);
    }
  }
  return referenced;
}

async function listServiceWorkspaceDirs() {
  const dirs = new Set([DEFAULT_WORKSPACE_DIR, LEGACY_WORKSPACE_DIR]);
  const entries = await fs.readdir(WORKSPACES_DIR, { withFileTypes: true }).catch((error) => {
    if (error.code === 'ENOENT') return [];
    throw error;
  });
  for (const entry of entries) {
    if (entry.isDirectory()) {
      dirs.add(path.join(WORKSPACES_DIR, entry.name));
    }
  }
  return Array.from(dirs);
}

async function collectAllReferencedSha256() {
  const referenced = new Set();
  for (const workspaceDir of await listServiceWorkspaceDirs()) {
    for (const sha of await collectWorkspaceReferencedSha256(workspaceDir)) {
      referenced.add(sha);
    }
  }
  return referenced;
}

async function listArtifactBlobFiles() {
  const blobsDir = path.join(defaultArtifactStoreRoot(), 'blobs');
  const files = [];

  async function visit(dir) {
    const entries = await fs.readdir(dir, { withFileTypes: true }).catch((error) => {
      if (error.code === 'ENOENT') return [];
      throw error;
    });
    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        await visit(fullPath);
      } else if (entry.isFile() && /^[0-9a-f]{64}$/.test(entry.name)) {
        files.push(fullPath);
      }
    }
  }

  await visit(blobsDir);
  return files;
}

async function cleanupWorkspaceArtifacts(workspaceDir, options = {}) {
  const dryRun = options.dryRun !== false;
  if (!dryRun) {
    const error = new Error(
      'Destructive artifact cleanup is disabled; Maze Core owns CAS references and deletion',
    );
    error.status = 403;
    error.code = 'CORE_OWNED_ARTIFACT_CLEANUP';
    throw error;
  }
  const olderThanDays = options.olderThanDays === null || options.olderThanDays === undefined
    ? 7
    : Number(options.olderThanDays);
  const cutoffMs = Number.isFinite(olderThanDays) && olderThanDays >= 0
    ? Date.now() - olderThanDays * 86400 * 1000
    : null;
  const referenced = await collectAllReferencedSha256();
  const candidates = [];

  for (const fullPath of await listArtifactBlobFiles()) {
    const sha = path.basename(fullPath).toLowerCase();
    if (referenced.has(sha)) {
      continue;
    }
    const stat = await fs.stat(fullPath);
    if (cutoffMs !== null && stat.mtimeMs > cutoffMs) {
      continue;
    }
    candidates.push({
      sha256: sha,
      size: stat.size,
      path: fullPath,
      storage_uri: `maze://artifacts/sha256/${sha}`,
      updatedAt: stat.mtime.toISOString(),
    });
  }

  const deletedSha256 = [];

  return {
    dry_run: dryRun,
    older_than_days: olderThanDays,
    scope: 'global-orphan-cas',
    deletion_owner: 'maze-core',
    destructive_cleanup_enabled: false,
    referenced_count: referenced.size,
    matched_count: candidates.length,
    deleted_count: deletedSha256.length,
    artifacts: candidates,
    deleted_sha256: deletedSha256,
  };
}

async function recoverStaticRunsInWorkspace(workspaceDir) {
  const runs = await listStaticRunFilesForWorkspace(workspaceDir);
  const staleRuns = runs.filter((run) => (
    run.status === 'running' && run.metadata?.benchmark !== 'gaia'
  ));
  let recoveryError = null;
  for (const run of staleRuns) {
    try {
      await appendAndApplyStaticRunEvent(workspaceDir, run.run_id, {
        type: 'workflow_interrupted',
        data: {
          reason: 'backend_restarted',
          message: 'Backend restarted before this workflow run finished.',
        },
        timestamp: new Date().toISOString(),
      });
      console.log(`↯ Static workflow run interrupted after backend restart: ${run.run_id}`);
    } catch (error) {
      console.error(`❌ 恢复 static workflow run 失败: ${run.run_id}`, error);
      recoveryError = recoveryError || error;
    }
  }

  const allGaiaRuns = runs.filter((run) => run.metadata?.benchmark === 'gaia');
  for (const run of allGaiaRuns) {
    try {
      await cleanupRecoveredGaiaStaging(workspaceDir, run.run_id);
    } catch (error) {
      console.error(`Failed to clean recovered GAIA staging for ${run.run_id}:`, error.message);
      recoveryError = recoveryError || error;
    }
  }

  const activeGaiaRuns = allGaiaRuns.filter((run) => !TERMINAL_STATIC_RUN_STATUSES.has(run.status));
  if (activeGaiaRuns.length) {
    const coreRuns = await listCoreStaticRuns();
    for (const run of activeGaiaRuns) {
      try {
        await withStaticRunWriteQueue(
          workspaceDir,
          run.run_id,
          () => reconcileGaiaTraceUnlocked(
            workspaceDir,
            run.run_id,
            coreRuns,
            { markMissing: run.gaia_private?.submission_state === 'bound' },
          ),
        );
      } catch (error) {
        console.error(`Failed to reconcile GAIA Playground run ${run.run_id}:`, error);
        recoveryError = recoveryError || error;
      }
    }
  }

  if (recoveryError) {
    throw recoveryError;
  }
}

function recoverInterruptedStaticRuns(workspaceDir) {
  const key = path.resolve(workspaceDir);
  const existing = recoveredStaticRunWorkspaces.get(key);
  if (existing) {
    return existing;
  }

  let recovery;
  recovery = recoverStaticRunsInWorkspace(key).catch((error) => {
    if (recoveredStaticRunWorkspaces.get(key) === recovery) {
      recoveredStaticRunWorkspaces.delete(key);
    }
    console.error(`Static workflow recovery remains retryable for ${key}:`, error.message);
  });
  recoveredStaticRunWorkspaces.set(key, recovery);
  return recovery;
}

async function reconcileActiveGaiaRunsOnRead(workspaceDir, runs) {
  const activeRuns = (runs || []).filter((run) => (
    isGaiaTrace(run) && !TERMINAL_STATIC_RUN_STATUSES.has(run.status)
  ));
  if (!activeRuns.length) return false;

  let coreRuns;
  try {
    coreRuns = await listCoreStaticRuns();
  } catch (error) {
    console.error('GAIA read reconciliation remains retryable:', error.message);
    return false;
  }
  for (const run of activeRuns) {
    try {
      await withStaticRunWriteQueue(
        workspaceDir,
        run.run_id,
        () => reconcileGaiaTraceUnlocked(
          workspaceDir,
          run.run_id,
          coreRuns,
          { markMissing: run.gaia_private?.submission_state === 'bound' },
        ),
      );
    } catch (error) {
      console.error(`GAIA read reconciliation failed for ${run.run_id}:`, error.message);
    }
  }
  return true;
}

async function migrateLegacyStaticRuns(workspaceDir, { dryRun = true } = {}) {
  const migrated = [];
  const skipped = [];
  for (const legacyDir of legacyStaticRunsDirs(workspaceDir)) {
    const entries = await fs.readdir(legacyDir, { withFileTypes: true }).catch(() => []);
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      const sourceDir = path.join(legacyDir, entry.name);
      const sourceRunJson = path.join(sourceDir, 'run.json');
      if (!await fileExists(sourceRunJson)) {
        continue;
      }
      const targetDir = path.join(staticRunsDir(workspaceDir), entry.name);
      if (await fileExists(path.join(targetDir, 'run.json'))) {
        skipped.push({
          run_id: entry.name,
          source: sourceDir,
          target: targetDir,
          reason: 'target-exists',
        });
        continue;
      }
      migrated.push({
        run_id: entry.name,
        source: sourceDir,
        target: targetDir,
      });
      if (!dryRun) {
        await fs.mkdir(path.dirname(targetDir), { recursive: true });
        await fs.rename(sourceDir, targetDir);
      }
    }
  }
  return {
    dry_run: dryRun,
    migrated_count: dryRun ? 0 : migrated.length,
    matched_count: migrated.length,
    skipped_count: skipped.length,
    migrated,
    skipped,
  };
}

function stripNodeTaskCode(node, workspaceDir = null) {
  if (node?.data?.category !== 'workspace') {
    return node;
  }

  const relativePath = normalizeTaskRelativePath(node.data.taskPath || node.data.relativePath);
  const { customCode, relativePath: _relativePath, ...data } = node.data;

  return {
    ...node,
    type: 'taskNode',
    data: {
      ...data,
      workspaceDir: workspaceDir || data.workspaceDir,
      taskPath: relativePath,
    },
  };
}

function collectTaskDefinitions(nodes, explicitDefinitions = []) {
  const definitions = new Map();

  const upsert = (definition) => {
    const relativePath = definition?.relativePath || definition?.taskPath || definition?.sourcePath;
    if (!relativePath) {
      return;
    }

    const normalizedPath = normalizeTaskRelativePath(relativePath);
    const existing = definitions.get(normalizedPath);
    const incomingCode = definition?.code ?? '';
    const code = String(incomingCode).trim() ? incomingCode : existing?.code ?? '';
    definitions.set(normalizedPath, {
      type: 'workspace',
      ...(existing || {}),
      ...definition,
      relativePath: normalizedPath,
      code,
    });
  };

  explicitDefinitions.forEach(upsert);

  nodes.forEach((node) => {
    if (node?.data?.category !== 'workspace') {
      return;
    }

    upsert({
      relativePath: node.data.taskPath || node.data.relativePath,
      functionName: node.data.functionName,
      displayName: node.data.label,
      code: node.data.customCode || '',
      inputs: node.data.inputs || [],
      outputs: node.data.outputs || [],
      resources: node.data.resources,
    });
  });

  return Array.from(definitions.values());
}

async function nextImportedTaskPath(workspaceDir, workflowName, relativePath, code) {
  const normalized = normalizeTaskRelativePath(relativePath);
  const parsed = path.posix.parse(normalized);
  const importDir = `tasks/imported/${safeFileName(workflowName, 'workflow')}`;
  let suffix = 0;

  while (true) {
    const fileName = suffix === 0 ? parsed.base : `${parsed.name}-${suffix + 1}${parsed.ext}`;
    const candidate = path.posix.join(importDir, fileName);
    const { fullPath } = resolveTaskDefinitionFile(workspaceDir, candidate);

    if (!await fileExists(fullPath)) {
      return { relativePath: candidate, existsSame: false };
    }

    const existingCode = await fs.readFile(fullPath, 'utf-8');
    if (hashTaskCode(existingCode) === hashTaskCode(code)) {
      return { relativePath: candidate, existsSame: true };
    }

    suffix += 1;
  }
}

async function saveImportedTaskDefinition(workspaceDir, relativePath, definition, { parse = true } = {}) {
  if (!parse) {
    const { relativePath: targetRelativePath, fullPath } = resolveTaskDefinitionFile(workspaceDir, relativePath);
    await writeTextAtomic(fullPath, definition.code);
    clearWorkspaceTasksCache(workspaceDir);
    return {
      success: true,
      workspaceDir,
      tasksDir: path.join(workspaceDir, 'tasks'),
      relativePath: targetRelativePath,
    };
  }

  const result = await callPython('save_workspace_task', {
    workspaceDir,
    relativePath,
    code: definition.code,
    parse: true,
  });

  if (result.error || result.success === false) {
    throw new Error(result.error || `Failed to import task: ${relativePath}`);
  }

  const parsedTask = Array.isArray(result.tasks)
    ? result.tasks.find((task) => !definition.functionName || task.functionName === definition.functionName)
    : result.task;

  if (definition.functionName && parsedTask?.functionName !== definition.functionName) {
    throw new Error(
      `Imported task ${relativePath} defines ${parsedTask?.functionName || 'no task'} instead of ${definition.functionName}`,
    );
  }

  return result;
}

async function importTaskDefinitions(
  workspaceDir,
  taskDefinitions = [],
  workflowName = 'imported-workflow',
  { parse = true } = {},
) {
  const imported = [];
  const skipped = [];
  const remapped = [];
  const taskPathMap = new Map();

  for (const definition of collectTaskDefinitions([], taskDefinitions)) {
    if (!definition.code || !String(definition.code).trim()) {
      skipped.push({ relativePath: definition.relativePath, reason: 'empty-code' });
      continue;
    }

    const { relativePath, fullPath } = resolveTaskDefinitionFile(workspaceDir, definition.relativePath);
    let targetRelativePath = relativePath;

    if (await fileExists(fullPath)) {
      const existingCode = await fs.readFile(fullPath, 'utf-8');
      if (hashTaskCode(existingCode) === hashTaskCode(definition.code)) {
        skipped.push({ relativePath, reason: 'exists-same' });
      } else {
        const target = await nextImportedTaskPath(workspaceDir, workflowName, relativePath, definition.code);
        targetRelativePath = target.relativePath;
        if (target.existsSame) {
          skipped.push({
            relativePath: targetRelativePath,
            sourceRelativePath: relativePath,
            reason: 'exists-same',
          });
        } else {
          await saveImportedTaskDefinition(workspaceDir, targetRelativePath, definition, { parse });
          imported.push({ relativePath: targetRelativePath, sourceRelativePath: relativePath });
        }
        remapped.push({
          from: relativePath,
          to: targetRelativePath,
          reason: target.existsSame ? 'conflict-reused' : 'conflict',
        });
      }
    } else {
      await saveImportedTaskDefinition(workspaceDir, targetRelativePath, definition, { parse });
      imported.push({ relativePath: targetRelativePath });
    }

    const mapValue = {
      relativePath: targetRelativePath,
      code: definition.code,
    };
    taskPathMap.set(taskDefinitionKey(relativePath, definition.functionName), mapValue);
    taskPathMap.set(relativePath, mapValue);
  }

  return { imported, skipped, remapped, taskPathMap };
}

async function hydrateWorkspaceWorkflowNodes(nodes, workspaceDir, taskDefinitions = [], taskPathMap = new Map()) {
  const definitionsByPath = new Map(
    collectTaskDefinitions([], taskDefinitions).map((definition) => [definition.relativePath, definition])
  );

  return Promise.all(nodes.map(async (node) => {
    if (node?.data?.category !== 'workspace') {
      return node;
    }

    const relativePath = normalizeTaskRelativePath(node.data.taskPath || node.data.relativePath);
    const functionName = node.data.functionName;
    const mapped = taskPathMap.get(taskDefinitionKey(relativePath, functionName)) || taskPathMap.get(relativePath);
    const taskPath = mapped?.relativePath || relativePath;
    const definition = mapped || definitionsByPath.get(relativePath) || definitionsByPath.get(taskPath);
    const code = definition?.code || node.data.customCode || await readWorkspaceTaskCode(workspaceDir, taskPath);

    return {
      ...node,
      type: 'taskNode',
      data: {
        ...node.data,
        workspaceDir,
        taskPath,
        customCode: code,
      },
    };
  }));
}

function createClientDisconnectAbort(req, res) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  const abortOnResponseClose = () => {
    if (!res.writableEnded) {
      controller.abort();
    }
  };

  if (req.aborted || res.destroyed) {
    controller.abort();
  } else {
    req.once('aborted', abort);
    res.once('close', abortOnResponseClose);
  }

  return {
    signal: controller.signal,
    dispose() {
      req.off('aborted', abort);
      res.off('close', abortOnResponseClose);
    },
  };
}

async function fetchMazeCoreBody(pathname, options = {}) {
  const controller = new AbortController();
  const externalSignal = options.signal;
  const abortFromExternal = () => controller.abort();
  if (externalSignal) {
    if (externalSignal.aborted) {
      controller.abort();
    } else {
      externalSignal.addEventListener('abort', abortFromExternal, { once: true });
    }
  }

  let timedOut = false;
  const timeout = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, MAZE_CORE_REQUEST_TIMEOUT_MS);
  timeout.unref?.();

  try {
    const response = await fetch(`${MAZE_CORE_URL}${pathname}`, {
      signal: controller.signal,
    });
    const body = Buffer.from(await response.arrayBuffer());
    return { response, body };
  } catch (error) {
    if (controller.signal.aborted) {
      const requestError = new Error(
        timedOut
          ? `Maze Core request timed out after ${MAZE_CORE_REQUEST_TIMEOUT_MS}ms`
          : 'Maze Core request was canceled',
      );
      requestError.status = timedOut ? 504 : 499;
      requestError.code = timedOut ? 'MAZE_CORE_TIMEOUT' : 'MAZE_CORE_ABORTED';
      throw requestError;
    }
    throw error;
  } finally {
    clearTimeout(timeout);
    if (externalSignal) {
      externalSignal.removeEventListener('abort', abortFromExternal);
    }
  }
}

async function callMazeCore(pathname, options = {}) {
  const url = `${MAZE_CORE_URL}${pathname}`;
  const timeoutMs = Math.min(
    MAZE_CORE_REQUEST_TIMEOUT_MS,
    Math.max(100, Number(options.timeoutMs) || MAZE_CORE_REQUEST_TIMEOUT_MS),
  );
  const controller = new AbortController();
  const externalSignal = options.signal;
  const abortFromExternal = () => controller.abort();
  if (externalSignal) {
    if (externalSignal.aborted) {
      controller.abort();
    } else {
      externalSignal.addEventListener('abort', abortFromExternal, { once: true });
    }
  }
  let timedOut = false;
  const timeout = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  timeout.unref?.();

  try {
    const response = await fetch(url, {
      method: options.method || 'GET',
      headers: {
        'Content-Type': 'application/json',
        ...(options.headers || {}),
      },
      body: options.body ? JSON.stringify(options.body) : undefined,
      signal: controller.signal,
    });

    const text = await response.text();
    let payload = {};
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = { error: text };
      }
    }

    if (!response.ok) {
      const message = payload?.detail || payload?.error || `Maze core request failed: ${response.status}`;
      const error = new Error(message);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }

    return payload;
  } catch (error) {
    if (controller.signal.aborted) {
      const requestError = new Error(
        timedOut
          ? `Maze Core request timed out after ${timeoutMs}ms`
          : 'Maze Core request was canceled',
      );
      requestError.status = timedOut ? 504 : 499;
      requestError.code = timedOut ? 'MAZE_CORE_TIMEOUT' : 'MAZE_CORE_ABORTED';
      throw requestError;
    }
    throw error;
  } finally {
    clearTimeout(timeout);
    if (externalSignal) {
      externalSignal.removeEventListener('abort', abortFromExternal);
    }
  }
}

function requirePublicCoreRun(coreRun) {
  if (coreRun?.metadata?.benchmark === 'gaia') {
    const error = new Error('Run not found');
    error.status = 404;
    throw error;
  }
  return coreRun;
}

async function requirePublicCoreRunId(runId) {
  return requirePublicCoreRun(await loadCoreRun(runId));
}

// ========== Python 桥接函数 ==========

function callPython(action, params = {}) {
  return new Promise((resolve, reject) => {
    const bridgePath = path.join(__dirname, '../maze_bridge.py');

    const python = spawn(PYTHON_BIN, [bridgePath, action, JSON.stringify(params)], {
      env: {
        ...process.env,
        MAZE_WORKSPACE_ROOT_DIR: WORKSPACE_ROOT_DIR,
        MAZE_WORKSPACES_DIR: WORKSPACES_DIR,
        MAZE_DEFAULT_WORKSPACE_DIR: DEFAULT_WORKSPACE_DIR,
        MAZE_SYSTEM_CATALOG_DIR: SYSTEM_CATALOG_DIR,
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1'
      }
    });

    let output = '';
    let error = '';

    python.stdout.setEncoding('utf8');
    python.stdout.on('data', (data) => {
      output += data;
    });

    python.stderr.setEncoding('utf8');
    python.stderr.on('data', (data) => {
      error += data;
    });

    python.on('close', (code) => {
      if (code === 0) {
        try {
          if (error.trim()) console.error('Python stderr:', error.trim());
          const result = JSON.parse(output);
          resolve(result);
        } catch (e) {
          console.error('解析Python输出失败:', output);
          reject(new Error('解析Python输出失败: ' + output));
        }
      } else {
        console.error('Python执行失败 (code ' + code + '):', error);
        reject(new Error('Python执行失败: ' + error));
      }
    });

    python.on('error', (err) => {
      console.error('Python进程错误:', err);
      reject(err);
    });
  });
}

async function parseWorkflowTaskDefinition(code, label) {
  const parsed = await callPython('parse_custom_function', { code });
  if (parsed?.error) {
    throw badRequestError(`${label}: ${parsed.error}`);
  }
  return parsed;
}

async function resolveWorkflowDefinitions(workflow, workspaceDir) {
  const definitions = new Map();
  const nodes = Array.isArray(workflow?.nodes) ? workflow.nodes : [];

  if (nodes.some((node) => node?.data?.category === 'workspace')) {
    const result = await callPython('get_workspace_tasks', { workspaceDir });
    if (result?.error) {
      throw badRequestError(result.error);
    }
    for (const task of result.tasks || []) {
      const relativePath = normalizeTaskRelativePath(task.relativePath);
      definitions.set(relativePath, task);
      if (task.functionName) {
        definitions.set(taskDefinitionKey(relativePath, task.functionName), task);
      }
    }
  }

  for (const node of nodes) {
    if (node?.data?.category !== 'custom') continue;
    const parsed = await parseWorkflowTaskDefinition(
      String(node.data.customCode || ''),
      `Custom task ${node.id}`,
    );
    definitions.set(`custom:${node.id}`, parsed);
  }

  const builtinRefs = new Set(
    nodes
      .filter((node) => node?.data?.category === 'builtin')
      .map((node) => String(node.data.taskRef || '')),
  );
  for (const taskRef of builtinRefs) {
    const relativePath = BUILTIN_TASK_ALIASES[taskRef];
    if (!relativePath) continue;
    const functionName = taskRef.split('.').at(-1);
    const code = await fs.readFile(path.join(SYSTEM_CATALOG_DIR, relativePath), 'utf-8');
    const parsed = await parseWorkflowTaskDefinition(code, `Builtin task ${taskRef}`);
    if (parsed.functionName !== functionName) {
      throw new Error(`Builtin task ${taskRef} resolved to ${parsed.functionName || 'no function'}`);
    }
    definitions.set(relativePath, parsed);
    definitions.set(taskDefinitionKey(relativePath, functionName), parsed);
  }

  return definitions;
}

async function findCoreWorkflowSubmission(submissionId) {
  const runs = await listCoreStaticRuns();
  const matches = runs.filter((run) => (
    run?.metadata?.submission_id === submissionId
    && run?.metadata?.source === 'maze_playground'
  ));
  if (matches.length > 1) {
    const error = new Error(`Multiple Core runs use Playground submission ${submissionId}`);
    error.status = 409;
    throw error;
  }
  return matches[0] ? requirePublicCoreRun(matches[0]) : null;
}

async function submitPlaygroundWorkflow({
  workflow,
  context,
  playgroundWorkflowId,
  workflowPath = null,
  draftId = null,
}) {
  const submissionId = uuidv4();
  const definitions = await resolveWorkflowDefinitions(workflow, context.workspaceDir);
  let spec;
  try {
    spec = compileWorkflowToDagSpec(workflow, {
      workspaceDir: context.workspaceDir,
      workspaceId: context.workspaceId,
      workspaceManifestVersion: context.workspaceManifestVersion,
      artifactMode: true,
      tags: draftId ? ['playground', 'workspace-agent'] : ['playground'],
      metadata: {
        source: 'maze_playground',
        submission_id: submissionId,
        playground_workflow_id: playgroundWorkflowId,
        ...(workflowPath ? { workflow_path: workflowPath } : {}),
        ...(draftId ? { draft_id: draftId } : {}),
      },
    }, definitions);
  } catch (error) {
    throw badRequestError(error);
  }

  let receipt;
  try {
    receipt = await callMazeCore('/workflows/submit', { method: 'POST', body: spec });
  } catch (error) {
    const ambiguous = error.status === 504
      || ['MAZE_CORE_TIMEOUT', 'MAZE_CORE_ABORTED'].includes(error.code);
    if (!ambiguous) throw error;
    const run = await findCoreWorkflowSubmission(submissionId).catch(() => null);
    if (!run) {
      error.message = `${error.message}; Core submission outcome is unknown (${submissionId})`;
      throw error;
    }
    receipt = { workflow_id: run.workflow_id, run_id: run.run_id };
  }

  if (!receipt?.run_id || !receipt?.workflow_id) {
    const run = await findCoreWorkflowSubmission(submissionId);
    if (!run) {
      const error = new Error('Maze Core returned a malformed workflow submission receipt');
      error.status = 502;
      throw error;
    }
    receipt = { workflow_id: run.workflow_id, run_id: run.run_id };
  }

  return {
    runId: String(receipt.run_id),
    coreWorkflowId: String(receipt.workflow_id),
    submissionId,
  };
}

function startReactWorkflowProcess(params = {}, extraEnv = {}, onExit = null) {
  return new Promise((resolve, reject) => {
    const bridgePath = path.join(__dirname, '../maze_bridge.py');
    const python = spawn(PYTHON_BIN, [bridgePath, 'run_react_workflow', JSON.stringify(params)], {
      env: {
        ...process.env,
        ...extraEnv,
        MAZE_WORKSPACE_ROOT_DIR: WORKSPACE_ROOT_DIR,
        MAZE_WORKSPACES_DIR: WORKSPACES_DIR,
        MAZE_DEFAULT_WORKSPACE_DIR: DEFAULT_WORKSPACE_DIR,
        MAZE_SYSTEM_CATALOG_DIR: SYSTEM_CATALOG_DIR,
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1',
      },
    });

    let output = '';
    let error = '';
    let stderrLineBuffer = '';
    let settled = false;
    let runId = null;
    let exitHandled = false;
    const handleExit = () => {
      if (exitHandled) return;
      exitHandled = true;
      onExit?.();
    };

    const startupTimer = setTimeout(() => {
      if (!settled) {
        settled = true;
        python.kill('SIGTERM');
        reject(new Error('Timed out waiting for ReAct run id'));
      }
    }, 15000);

    const settleStarted = (payload) => {
      if (settled) return;
      runId = payload?.data?.run_id;
      if (!runId) return;
      settled = true;
      clearTimeout(startupTimer);
      resolve({
        success: true,
        runId,
        status: 'running',
        mode: payload?.data?.mode || params.mode || 'local',
      });
    };

    python.stdout.setEncoding('utf8');
    python.stdout.on('data', (data) => {
      output += data;
    });

    python.stderr.setEncoding('utf8');
    python.stderr.on('data', (data) => {
      error += data;
      stderrLineBuffer += data;
      const lines = stderrLineBuffer.split(/\r?\n/);
      stderrLineBuffer = lines.pop() || '';

      lines.forEach((line) => {
        if (line.startsWith('__MAZE_PROGRESS__')) {
          const raw = line.slice('__MAZE_PROGRESS__'.length);
          try {
            const progress = JSON.parse(raw);
            if (progress.type === 'react_run_created') {
              settleStarted(progress);
            }
          } catch {
            console.error('Failed to parse ReAct progress message');
          }
        } else if (line.trim()) {
          console.error('Python stderr:', line);
        }
      });
    });

    python.on('close', (code) => {
      clearTimeout(startupTimer);
      handleExit();

      if (!settled) {
        settled = true;
        try {
          const result = parseBridgeJsonOutput(output);
          if (result.runId || result.error || result.success === false) {
            const bridgeError = new Error(result.error || 'ReAct process exited before returning a run id');
            bridgeError.bridgePayload = result;
            reject(bridgeError);
            return;
          }
        } catch {
          // Fall through to the generic process error below.
        }
        const message = code === 0
          ? 'ReAct process exited before returning a run id'
          : `ReAct process failed before returning a run id: ${error}`;
        reject(new Error(message));
        return;
      }

      if (code !== 0) {
        console.error(`ReAct process failed after run start (${runId || 'unknown'}):`, error);
        return;
      }

      try {
        const result = parseBridgeJsonOutput(output);
        if (result.error || result.success === false) {
          console.error(`ReAct process returned an error (${runId || result.runId || 'unknown'}):`, result.error);
        }
      } catch {
        console.error('Failed to parse ReAct process output:', output);
      }
    });

    python.on('error', (err) => {
      clearTimeout(startupTimer);
      handleExit();
      if (!settled) {
        settled = true;
        reject(err);
        return;
      }
      console.error('ReAct process error:', err);
    });
  });
}

function parseBridgeJsonOutput(output) {
  const text = String(output || '').trim();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    for (let index = lines.length - 1; index >= 0; index -= 1) {
      if (!lines[index].startsWith('{')) continue;
      try {
        return JSON.parse(lines[index]);
      } catch {
        // Keep scanning older lines.
      }
    }
    throw new Error(`Failed to parse bridge JSON output: ${text}`);
  }
}

function runMcpDiscoveryProcess(params = {}) {
  return new Promise((resolve, reject) => {
    const bridgePath = path.join(__dirname, '../maze_bridge.py');
    const python = spawn(PYTHON_BIN, [bridgePath, 'discover_mcp_tools', JSON.stringify(params)], {
      env: {
        ...process.env,
        MAZE_WORKSPACE_ROOT_DIR: WORKSPACE_ROOT_DIR,
        MAZE_WORKSPACES_DIR: WORKSPACES_DIR,
        MAZE_DEFAULT_WORKSPACE_DIR: DEFAULT_WORKSPACE_DIR,
        MAZE_SYSTEM_CATALOG_DIR: SYSTEM_CATALOG_DIR,
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1',
      },
    });

    let output = '';
    let error = '';
    python.stdout.setEncoding('utf8');
    python.stderr.setEncoding('utf8');
    python.stdout.on('data', (data) => {
      output += data;
    });
    python.stderr.on('data', (data) => {
      error += data;
    });
    python.on('close', (code) => {
      try {
        const result = parseBridgeJsonOutput(output);
        if (code !== 0) {
          reject(new Error(result.error || error || `MCP discovery process failed with code ${code}`));
          return;
        }
        resolve(result);
      } catch {
        reject(new Error(error || `Failed to parse MCP discovery output: ${output}`));
      }
    });
    python.on('error', reject);
  });
}

const MCP_TRANSPORTS = new Set(['stdio', 'streamable_http', 'sse']);

function sanitizeNamePart(value, fallback = 'mcp') {
  const safe = String(value || '')
    .trim()
    .replace(/[^a-zA-Z0-9_.-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80);
  return safe || fallback;
}

function normalizeStringList(value, fieldName) {
  if (value === undefined || value === null || value === '') return [];
  if (!Array.isArray(value)) {
    throw new Error(`MCP ${fieldName} must be an array`);
  }
  return value.map((item) => String(item));
}

function normalizeStringMap(value, fieldName) {
  if (value === undefined || value === null || value === '') return undefined;
  if (typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`MCP ${fieldName} must be an object`);
  }
  const result = {};
  Object.entries(value).forEach(([key, entryValue]) => {
    const normalizedKey = String(key || '').trim();
    if (!normalizedKey) {
      throw new Error(`MCP ${fieldName} contains an empty key`);
    }
    result[normalizedKey] = String(entryValue ?? '');
  });
  return result;
}

function normalizeMcpTimeout(value, serverLabel) {
  if (value === undefined || value === null || value === '') return 30;
  const timeout = Number(value);
  if (!Number.isFinite(timeout)) {
    throw new Error(`MCP server "${serverLabel}" timeout must be a number`);
  }
  return Math.min(Math.max(timeout, 1), 300);
}

function validateMcpServers(value) {
  if (value === undefined || value === null || value === '') return [];
  if (!Array.isArray(value)) {
    throw new Error('mcpServers must be an array');
  }
  if (value.length > 8) {
    throw new Error('mcpServers supports at most 8 servers per run');
  }

  return value.map((server, index) => {
    if (!server || typeof server !== 'object' || Array.isArray(server)) {
      throw new Error(`MCP server #${index + 1} must be an object`);
    }

    const transport = String(server.transport || 'stdio').trim();
    if (!MCP_TRANSPORTS.has(transport)) {
      throw new Error(`MCP server #${index + 1} has unsupported transport: ${transport}`);
    }

    const name = sanitizeNamePart(server.name || `mcp-${index + 1}`, `mcp-${index + 1}`);
    const toolPrefix = server.tool_prefix || server.toolPrefix
      ? sanitizeNamePart(server.tool_prefix || server.toolPrefix, name)
      : undefined;
    const timeout = normalizeMcpTimeout(server.timeout, name);
    const normalized = {
      name,
      transport,
      args: normalizeStringList(server.args, 'args'),
      env: normalizeStringMap(server.env, 'env'),
      cwd: server.cwd ? String(server.cwd) : undefined,
      headers: normalizeStringMap(server.headers, 'headers'),
      timeout,
      tool_prefix: toolPrefix,
    };

    if (transport === 'stdio') {
      const command = String(server.command || '').trim();
      if (!command) {
        throw new Error(`MCP stdio server "${name}" requires command`);
      }
      normalized.command = command;
    } else {
      const url = String(server.url || '').trim();
      if (!url) {
        throw new Error(`MCP ${transport} server "${name}" requires url`);
      }
      try {
        const parsed = new URL(url);
        if (!['http:', 'https:'].includes(parsed.protocol)) {
          throw new Error('URL must use http or https');
        }
      } catch (error) {
        throw new Error(`MCP ${transport} server "${name}" has invalid url: ${error.message}`);
      }
      normalized.url = url;
    }

    return normalized;
  });
}

function summarizeMcpServers(servers = []) {
  return servers.map((server) => {
    const summary = {
      name: server.name,
      transport: server.transport,
      tool_prefix: server.tool_prefix,
      timeout: server.timeout,
      has_env: Boolean(server.env && Object.keys(server.env).length),
      has_headers: Boolean(server.headers && Object.keys(server.headers).length),
    };
    if (server.transport === 'stdio') {
      summary.command = server.command;
      summary.args_count = Array.isArray(server.args) ? server.args.length : 0;
      summary.cwd = server.cwd || null;
    } else if (server.url) {
      const parsed = new URL(server.url);
      summary.url_scheme = parsed.protocol.replace(':', '');
      summary.url_host = parsed.host;
    }
    return summary;
  });
}

function mcpApiErrorStatus(error) {
  if (Number.isInteger(error?.status)) return error.status;
  const message = String(error?.message || '');
  if (message.startsWith('MCP ') || message.startsWith('mcpServers ')) return 400;
  return 500;
}

function catalogTypeDir(type) {
  const normalized = String(type || '').trim().toLowerCase();
  if (!['workflows', 'tasks', 'skills'].includes(normalized)) {
    throw new Error(`Unsupported system catalog type: ${type}`);
  }
  return { type: normalized, dir: path.join(SYSTEM_CATALOG_DIR, normalized) };
}

async function listCatalogItems(type) {
  await ensureSystemCatalogDirs();
  const { type: normalizedType, dir } = catalogTypeDir(type);
  const entries = await fs.readdir(dir, { withFileTypes: true }).catch(() => []);
  const items = [];
  for (const entry of entries) {
    if (entry.name.startsWith('.')) {
      continue;
    }
    if (entry.name === '__pycache__' || entry.name.startsWith('__')) {
      continue;
    }
    if (normalizedType === 'tasks' && (!entry.isFile() || !entry.name.endsWith('.py'))) {
      continue;
    }
    if (normalizedType === 'workflows' && (!entry.isFile() || !entry.name.endsWith('.json'))) {
      continue;
    }
    const fullPath = path.join(dir, entry.name);
    const stat = await fs.stat(fullPath);
    const metadata = await catalogItemMetadata(normalizedType, fullPath, entry);
    items.push({
      type: normalizedType,
      id: entry.name,
      name: entry.name,
      path: entry.name,
      kind: entry.isDirectory() ? 'directory' : 'file',
      size: entry.isFile() ? stat.size : null,
      updatedAt: stat.mtime.toISOString(),
      ...metadata,
    });
  }
  items.sort((a, b) => a.name.localeCompare(b.name));
  return items;
}

function resolveCatalogSource(type, sourceId) {
  const { type: normalizedType, dir } = catalogTypeDir(type);
  const normalizedSource = normalizeWorkspaceFileRelativePath(sourceId);
  if (!normalizedSource) {
    throw new Error('sourceId is required');
  }
  const sourcePath = path.resolve(dir, normalizedSource);
  if (sourcePath !== dir && !sourcePath.startsWith(dir + path.sep)) {
    throw new Error('System catalog source must stay inside the catalog directory');
  }
  return { type: normalizedType, sourceId: normalizedSource, sourcePath };
}

function badRequestError(errorOrMessage) {
  const error = errorOrMessage instanceof Error
    ? errorOrMessage
    : new Error(String(errorOrMessage || 'Bad request'));
  error.status = 400;
  return error;
}

function resolveSystemWorkflowSource(sourceId) {
  const rawSourceId = String(sourceId || '').trim();
  const posixSourceId = rawSourceId.replace(/\\/g, '/');
  if (!rawSourceId || path.posix.isAbsolute(posixSourceId) || isWindowsDrivePath(rawSourceId)) {
    throw badRequestError('System workflow sourceId must be a relative catalog path');
  }

  try {
    return resolveCatalogSource('workflows', rawSourceId);
  } catch (error) {
    throw badRequestError(error);
  }
}

async function readSystemWorkflow(sourcePath) {
  let raw;
  try {
    raw = await fs.readFile(sourcePath, 'utf-8');
  } catch (error) {
    if (error?.code === 'EISDIR') {
      throw badRequestError(error);
    }
    throw error;
  }

  let payload;
  try {
    payload = JSON.parse(raw);
  } catch (error) {
    throw badRequestError(error);
  }

  try {
    return normalizeWorkflowPayload(payload);
  } catch (error) {
    throw badRequestError(error);
  }
}

async function copyCatalogItemToWorkspace({ workspaceDir, type, sourceId, targetPath }) {
  await ensureSystemCatalogDirs();
  const { type: normalizedType, sourceId: normalizedSourceId, sourcePath } = resolveCatalogSource(type, sourceId);
  const stat = await fs.stat(sourcePath);
  let resolvedTargetPath;
  let targetRelativePath;

  if (normalizedType === 'workflows') {
    const target = resolveWorkflowFile(workspaceDir, targetPath || path.basename(normalizedSourceId), path.basename(normalizedSourceId, '.json'));
    resolvedTargetPath = target.fullPath;
    targetRelativePath = target.relativePath;
  } else if (normalizedType === 'tasks') {
    const target = resolveTaskDefinitionFile(workspaceDir, targetPath || path.basename(normalizedSourceId));
    resolvedTargetPath = target.fullPath;
    targetRelativePath = target.relativePath;
  } else {
    const skillName = safeWorkspaceId(targetPath || path.basename(normalizedSourceId), path.basename(normalizedSourceId));
    targetRelativePath = path.posix.join('skills', skillName);
    resolvedTargetPath = path.resolve(workspaceDir, targetRelativePath);
    const skillsDir = path.resolve(workspaceDir, 'skills');
    if (!resolvedTargetPath.startsWith(skillsDir + path.sep)) {
      throw new Error('Skill import target must stay inside workspace skills directory');
    }
  }

  await fs.mkdir(path.dirname(resolvedTargetPath), { recursive: true });
  if (stat.isDirectory()) {
    await fs.cp(sourcePath, resolvedTargetPath, { recursive: true, force: false, errorOnExist: false });
  } else {
    await fs.copyFile(sourcePath, resolvedTargetPath);
  }

  const manifest = await recordWorkspaceImport(workspaceDir, {
    type: normalizedType.slice(0, -1),
    source: 'system_catalog',
    source_id: normalizedSourceId,
    workspace_path: targetRelativePath,
  });

  return {
    workspaceDir,
    workspaceId: manifest.workspace_id,
    manifest,
    import: {
      type: normalizedType,
      sourceId: normalizedSourceId,
      targetPath: targetRelativePath,
    },
  };
}

// ========== API 路由 ==========

app.post('/api/workspaces', async (req, res) => {
  try {
    const workspace = await createWorkspace({
      workspaceId: req.body?.workspaceId,
      name: req.body?.name,
      mode: req.body?.mode || 'session',
    });
    res.json({ success: true, ...workspace });
  } catch (error) {
    console.error('❌ 创建 workspace 失败:', error);
    res.status(500).json({ error: error.message });
  }
});

app.get('/api/workspaces/current', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    res.json({
      success: true,
      workspaceId: context.workspaceId,
      workspaceDir: context.workspaceDir,
      workspaceManifestVersion: context.workspaceManifestVersion,
      manifest: context.manifest,
    });
  } catch (error) {
    console.error('❌ 获取当前 workspace 失败:', error);
    res.status(500).json({ error: error.message });
  }
});

app.get('/api/workspaces/:workspaceId', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext({ workspaceId: req.params.workspaceId });
    res.json({
      success: true,
      workspaceId: context.workspaceId,
      workspaceDir: context.workspaceDir,
      workspaceManifestVersion: context.workspaceManifestVersion,
      manifest: context.manifest,
    });
  } catch (error) {
    console.error('❌ 获取 workspace 失败:', error);
    res.status(500).json({ error: error.message });
  }
});

app.get('/api/system-catalog', async (req, res) => {
  try {
    await ensureSystemCatalogDirs();
    const requestedType = req.query.type ? String(req.query.type) : '';
    const types = requestedType ? [requestedType] : ['workflows', 'tasks', 'skills'];
    const catalog = {};
    for (const type of types) {
      const { type: normalizedType } = catalogTypeDir(type);
      catalog[normalizedType] = await listCatalogItems(normalizedType);
    }
    res.json({ success: true, catalogDir: SYSTEM_CATALOG_DIR, catalog });
  } catch (error) {
    console.error('❌ 获取 system catalog 失败:', error);
    res.status(500).json({ error: error.message });
  }
});

app.post('/api/system-catalog/import', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir,
      type,
      sourceId,
      targetPath,
    } = req.body || {};
    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir });
    const result = await copyCatalogItemToWorkspace({
      workspaceDir: context.workspaceDir,
      type,
      sourceId,
      targetPath,
    });
    res.json({ success: true, ...result });
  } catch (error) {
    console.error('❌ 导入 system catalog 失败:', error);
    res.status(500).json({ error: error.message });
  }
});

app.post('/api/system-catalog/workflows/load', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      sourceId,
    } = req.body || {};

    if (!sourceId) {
      return res.status(400).json({ error: 'sourceId is required' });
    }

    await ensureSystemCatalogDirs();
    const { sourceId: normalizedSourceId, sourcePath } = resolveSystemWorkflowSource(sourceId);
    const workflow = await readSystemWorkflow(sourcePath);
    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const workspaceDir = context.workspaceDir;
    const result = await withSystemWorkflowLoadQueue(workspaceDir, async () => {
      const lockedContext = await resolveWorkspaceContext({ workspaceDir });
      const importResult = await importTaskDefinitions(
        workspaceDir,
        workflow.includedTasks,
        workflow.name,
        { parse: false },
      );
      workflow.nodes = await hydrateWorkspaceWorkflowNodes(
        workflow.nodes,
        workspaceDir,
        workflow.includedTasks,
        importResult.taskPathMap,
      );

      let manifest = lockedContext.manifest;
      if (importResult.imported.length > 0) {
        manifest = await recordWorkspaceMutation(workspaceDir, 'system_workflow_template_loaded', {
          source_id: normalizedSourceId,
          workflow_name: workflow.name,
          imported_count: importResult.imported.length,
          remapped_count: importResult.remapped.length,
        });
      }

      return {
        success: true,
        workspaceId: manifest.workspace_id,
        workspaceDir,
        workspaceManifestVersion: Number(manifest.manifest_version || lockedContext.workspaceManifestVersion),
        sourceId: normalizedSourceId,
        workflow,
        importedTaskDefinitions: {
          imported: importResult.imported,
          skipped: importResult.skipped,
          remapped: importResult.remapped,
        },
      };
    });
    res.json(result);
  } catch (error) {
    console.error('❌ 加载 system workflow 失败:', error);
    res.status(statusForFileError(error)).json({ error: error.message });
  }
});

app.get('/api/workspace-policy', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const policyPath = workspacePolicyPath(context.workspaceDir);
    const policy = await readJsonFile(policyPath, null);
    res.json({
      success: true,
      ...workspaceResponseFields(context),
      policyPath: path.relative(context.workspaceDir, policyPath).split(path.sep).join('/'),
      policy: policy || {},
    });
  } catch (error) {
    console.error('❌ 获取 workspace policy 失败:', error);
    res.status(500).json({ error: error.message });
  }
});

app.put('/api/workspace-policy', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir,
      policy,
    } = req.body || {};
    if (!policy || typeof policy !== 'object' || Array.isArray(policy)) {
      return res.status(400).json({ error: 'policy must be a JSON object' });
    }

    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir });
    const policyPath = workspacePolicyPath(context.workspaceDir);
    await writeJsonAtomic(policyPath, policy);
    const manifest = await recordWorkspaceMutation(context.workspaceDir, 'policy_updated', {
      path: 'policies/sandbox_policy.json',
    });
    res.json({
      success: true,
      workspaceId: manifest.workspace_id,
      workspaceDir: context.workspaceDir,
      workspaceManifestVersion: Number(manifest.manifest_version || context.workspaceManifestVersion),
      policyPath: 'policies/sandbox_policy.json',
      policy,
    });
  } catch (error) {
    console.error('❌ 更新 workspace policy 失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// 1.1 获取工作目录任务列表
app.get('/api/workspace-tasks', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const workspaceDir = context.workspaceDir;
    const cacheKey = path.resolve(workspaceDir);
    const signature = await workspaceTasksSignature(workspaceDir);
    const cached = workspaceTasksCache.get(cacheKey);

    if (cached?.signature === signature) {
      res.json({ ...cached.result, ...workspaceResponseFields(context) });
      return;
    }

    console.log(`📁 扫描工作目录任务: ${workspaceDir}`);

    const result = await callPython('get_workspace_tasks', { workspaceDir });

    if (result.error) {
      console.error('❌ 扫描工作目录失败:', result.error);
      return res.status(400).json({ error: result.error, traceback: result.traceback });
    }

    console.log(`✅ 成功获取 ${result.tasks.length} 个工作区任务`);
    workspaceTasksCache.set(cacheKey, { signature, result });
    res.json({ ...result, ...workspaceResponseFields(context) });
  } catch (error) {
    console.error('❌ 获取工作区任务失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// 1.1b 获取工作目录 skills 列表
app.get('/api/workspace-skills', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const workspaceDir = context.workspaceDir;
    console.log(`📚 扫描工作目录 Skills: ${workspaceDir}`);

    const result = await callPython('list_workspace_skills', { workspaceDir });

    if (result.error) {
      console.error('❌ 扫描工作目录 Skills 失败:', result.error);
      return res.status(400).json({ error: result.error, traceback: result.traceback, errors: result.errors || [] });
    }

    console.log(`✅ 成功获取 ${(result.skills || []).length} 个工作区 Skill`);
    res.json({ ...result, ...workspaceResponseFields(context) });
  } catch (error) {
    console.error('❌ 获取工作区 Skills 失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// 1.2 保存工作目录任务
app.post('/api/workspace-tasks', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      relativePath = 'tasks/custom_task.py',
      code,
      parse = true,
    } = req.body;
    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const workspaceDir = context.workspaceDir;

    console.log(`💾 保存工作区任务: ${workspaceDir}/${relativePath}`);

    if ((!code || !code.trim()) && parse) {
      return res.status(400).json({ error: 'Code cannot be empty' });
    }

    const result = await callPython('save_workspace_task', {
      workspaceDir,
      relativePath,
      code,
      parse,
    });

    if (result.error || result.success === false) {
      console.error('❌ 保存工作区任务失败:', result.error);
      return res.status(400).json({ error: result.error, traceback: result.traceback });
    }

    clearWorkspaceTasksCache(workspaceDir);
    console.log('✅ 工作区任务保存成功');
    const manifest = await recordWorkspaceMutation(workspaceDir, 'task_saved', {
      path: result.relativePath || relativePath,
    });
    res.json({
      ...result,
      workspaceId: manifest.workspace_id,
      workspaceManifestVersion: Number(manifest.manifest_version || context.workspaceManifestVersion),
    });
  } catch (error) {
    console.error('❌ 保存工作区任务失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// 1.2.1 删除工作目录任务
app.delete('/api/workspace-tasks', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      relativePath,
    } = req.body;

    if (!relativePath) {
      return res.status(400).json({ error: 'relativePath is required' });
    }

    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const workspaceDir = context.workspaceDir;
    console.log(`🗑️ 删除工作区任务: ${workspaceDir}/${relativePath}`);

    const result = await callPython('delete_workspace_task', {
      workspaceDir,
      relativePath,
    });

    if (result.error || result.success === false) {
      console.error('❌ 删除工作区任务失败:', result.error);
      return res.status(400).json({ error: result.error, traceback: result.traceback });
    }

    clearWorkspaceTasksCache(workspaceDir);
    console.log('✅ 工作区任务删除成功');
    const manifest = await recordWorkspaceMutation(workspaceDir, 'task_deleted', {
      path: result.relativePath || relativePath,
    });
    res.json({
      ...result,
      workspaceId: manifest.workspace_id,
      workspaceManifestVersion: Number(manifest.manifest_version || context.workspaceManifestVersion),
    });
  } catch (error) {
    console.error('❌ 删除工作区任务失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// 1.2.2 重命名工作目录任务
app.patch('/api/workspace-tasks/rename', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      relativePath,
      oldFunctionName,
      newName,
    } = req.body;

    if (!relativePath || !oldFunctionName || !newName) {
      return res.status(400).json({ error: 'relativePath, oldFunctionName, and newName are required' });
    }

    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const workspaceDir = context.workspaceDir;
    console.log(`✏️ 重命名工作区任务: ${relativePath} ${oldFunctionName} -> ${newName}`);

    const result = await callPython('rename_workspace_task', {
      workspaceDir,
      relativePath,
      oldFunctionName,
      newName,
    });

    if (result.error || result.success === false) {
      console.error('❌ 重命名工作区任务失败:', result.error);
      return res.status(400).json({ error: result.error, traceback: result.traceback });
    }

    clearWorkspaceTasksCache(workspaceDir);
    console.log('✅ 工作区任务重命名成功');
    const manifest = await recordWorkspaceMutation(workspaceDir, 'task_renamed', {
      path: result.relativePath || relativePath,
      oldFunctionName,
      newFunctionName: result.newFunctionName,
    });
    res.json({
      ...result,
      workspaceId: manifest.workspace_id,
      workspaceManifestVersion: Number(manifest.manifest_version || context.workspaceManifestVersion),
    });
  } catch (error) {
    console.error('❌ 重命名工作区任务失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// 1.3 Workspace files
app.get('/api/workspace-files', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const workspaceDir = context.workspaceDir;
    const { fullPath, filesDir, relativePath } = resolveWorkspaceFilePath(workspaceDir, req.query.path || '');
    const stat = await fs.stat(fullPath).catch((error) => {
      if (error.code === 'ENOENT') return null;
      throw error;
    });

    if (!stat) {
      return res.status(404).json({ error: 'Workspace file path not found' });
    }
    if (!stat.isDirectory()) {
      return res.status(400).json({ error: 'Workspace file path is not a directory' });
    }

    const entries = await fs.readdir(fullPath, { withFileTypes: true });
    const files = await Promise.all(entries.map((entry) => describeWorkspaceFile(filesDir, path.join(fullPath, entry.name))));
    files.sort((a, b) => {
      if (a.type !== b.type) return a.type === 'directory' ? -1 : 1;
      return a.name.localeCompare(b.name);
    });

    res.json({ success: true, ...workspaceResponseFields(context), filesDir, path: relativePath, files });
  } catch (error) {
    console.error('❌ 获取 workspace files 失败:', error);
    res.status(500).json({ error: error.message });
  }
});

app.put('/api/local-workspaces/:workspaceId/manifest', async (req, res) => {
  try {
    const workspaceId = normalizeLocalWorkspaceId(req.params.workspaceId);
    const files = Array.isArray(req.body?.files) ? req.body.files : [];
    const normalizedFiles = [];
    const seen = new Set();

    for (const item of files) {
      const relativePath = normalizeWorkspaceFileRelativePath(item?.relativePath || item?.path || '');
      if (!relativePath || seen.has(relativePath)) {
        continue;
      }
      seen.add(relativePath);
      normalizedFiles.push({
        relativePath,
        name: path.posix.basename(relativePath),
        type: item?.type === 'directory' ? 'directory' : 'file',
        size: Number.isFinite(Number(item?.size)) ? Number(item.size) : null,
        updatedAt: item?.updatedAt ? String(item.updatedAt) : null,
        source: 'local',
      });
    }

    const manifest = {
      workspaceId,
      displayName: String(req.body?.displayName || workspaceId),
      version: String(req.body?.version || Date.now()),
      updatedAt: new Date().toISOString(),
      files: normalizedFiles,
    };
    localWorkspaceManifests.set(workspaceId, manifest);
    res.json({ success: true, manifest });
  } catch (error) {
    console.error('❌ 更新 local workspace manifest 失败:', error);
    res.status(500).json({ error: error.message });
  }
});

app.get('/api/local-workspaces/:workspaceId/manifest', (req, res) => {
  const workspaceId = normalizeLocalWorkspaceId(req.params.workspaceId);
  res.json({
    success: true,
    manifest: localWorkspaceManifests.get(workspaceId) || {
      workspaceId,
      displayName: workspaceId,
      version: null,
      updatedAt: null,
      files: [],
    },
  });
});

app.post('/api/workspace-files/missing', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      paths = [],
    } = req.body || {};
    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const workspaceDir = context.workspaceDir;
    const normalizedPaths = [];
    const seen = new Set();

    for (const rawPath of Array.isArray(paths) ? paths : []) {
      const relativePath = normalizeWorkspaceFileRelativePath(rawPath);
      if (!relativePath || seen.has(relativePath)) {
        continue;
      }
      seen.add(relativePath);
      normalizedPaths.push(relativePath);
    }

    const missing = [];
    const present = [];
    for (const relativePath of normalizedPaths) {
      const { fullPath } = resolveWorkspaceFilePath(workspaceDir, relativePath);
      const stat = await fs.stat(fullPath).catch((error) => {
        if (error.code === 'ENOENT') return null;
        throw error;
      });
      if (stat && stat.isFile()) {
        present.push(relativePath);
      } else {
        missing.push(relativePath);
      }
    }

    res.json({ success: true, ...workspaceResponseFields(context), present, missing });
  } catch (error) {
    console.error('❌ 检查 workspace file 缺失失败:', error);
    res.status(500).json({ error: error.message });
  }
});

app.post('/api/workspace-files/upload', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      relativePath,
      contentBase64,
    } = req.body || {};

    if (!relativePath) {
      return res.status(400).json({ error: 'relativePath is required' });
    }
    if (typeof contentBase64 !== 'string') {
      return res.status(400).json({ error: 'contentBase64 is required' });
    }

    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const workspaceDir = context.workspaceDir;
    const { fullPath, filesDir } = resolveWorkspaceFilePath(workspaceDir, relativePath);
    await fs.mkdir(path.dirname(fullPath), { recursive: true });
    await fs.writeFile(fullPath, Buffer.from(contentBase64, 'base64'));
    const file = await describeWorkspaceFile(filesDir, fullPath);
    const manifest = await recordWorkspaceMutation(workspaceDir, 'file_uploaded', {
      path: file.relativePath,
    });
    res.json({
      success: true,
      workspaceId: manifest.workspace_id,
      workspaceDir,
      workspaceManifestVersion: Number(manifest.manifest_version || context.workspaceManifestVersion),
      file,
    });
  } catch (error) {
    console.error('❌ 上传 workspace file 失败:', error);
    res.status(500).json({ error: error.message });
  }
});

app.post('/api/artifacts/promote', async (req, res) => {
  const clientRequest = createClientDisconnectAbort(req, res);
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
    } = req.body || {};
    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    res.json(await promoteArtifactIntoWorkspace(
      context,
      req.body || {},
      { signal: clientRequest.signal },
    ));
  } catch (error) {
    console.error('❌ Promote artifact 失败:', error);
    if (res.destroyed) return;
    res.status(error.status || 500).json({ error: error.message });
  } finally {
    clientRequest.dispose();
  }
});

app.post('/api/artifacts/cleanup', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      older_than_days: olderThanDays,
      dry_run: dryRun = true,
    } = req.body || {};
    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const cleanup = await cleanupWorkspaceArtifacts(context.workspaceDir, {
      olderThanDays,
      dryRun,
    });
    res.json({
      success: true,
      ...workspaceResponseFields(context),
      cleanup,
    });
  } catch (error) {
    console.error('❌ 清理 artifacts 失败:', error);
    res.status(error.status || 500).json({ error: error.message });
  }
});

app.post('/api/workspace-files/mkdir', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      relativePath,
    } = req.body || {};

    if (!relativePath) {
      return res.status(400).json({ error: 'relativePath is required' });
    }

    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const workspaceDir = context.workspaceDir;
    const { fullPath, filesDir } = resolveWorkspaceFilePath(workspaceDir, relativePath);
    await fs.mkdir(fullPath, { recursive: true });
    const file = await describeWorkspaceFile(filesDir, fullPath);
    const manifest = await recordWorkspaceMutation(workspaceDir, 'folder_created', {
      path: file.relativePath,
    });
    res.json({
      success: true,
      workspaceId: manifest.workspace_id,
      workspaceDir,
      workspaceManifestVersion: Number(manifest.manifest_version || context.workspaceManifestVersion),
      file,
    });
  } catch (error) {
    console.error('❌ 创建 workspace folder 失败:', error);
    res.status(500).json({ error: error.message });
  }
});

app.delete('/api/workspace-files', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      relativePath,
    } = req.body || {};

    if (!relativePath) {
      return res.status(400).json({ error: 'relativePath is required' });
    }

    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const workspaceDir = context.workspaceDir;
    const { fullPath } = resolveWorkspaceFilePath(workspaceDir, relativePath);
    await fs.rm(fullPath, { recursive: true, force: true });
    const manifest = await recordWorkspaceMutation(workspaceDir, 'file_deleted', {
      path: relativePath,
    });
    res.json({
      success: true,
      workspaceId: manifest.workspace_id,
      workspaceDir,
      workspaceManifestVersion: Number(manifest.manifest_version || context.workspaceManifestVersion),
      relativePath,
      deleted: true,
    });
  } catch (error) {
    console.error('❌ 删除 workspace file 失败:', error);
    res.status(500).json({ error: error.message });
  }
});

app.get('/api/workspace-files/preview', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const workspaceDir = context.workspaceDir;
    const { fullPath, relativePath } = resolveWorkspaceFilePath(workspaceDir, req.query.path || '');
    const stat = await fs.stat(fullPath);

    if (!stat.isFile()) {
      return res.status(400).json({ error: 'Workspace file path is not a file' });
    }
    if (stat.size > 512 * 1024) {
      return res.status(413).json({ error: 'File is too large to preview' });
    }

    const content = await fs.readFile(fullPath, 'utf-8');
    res.json({ success: true, ...workspaceResponseFields(context), relativePath, content });
  } catch (error) {
    console.error('❌ 预览 workspace file 失败:', error);
    res.status(statusForFileError(error)).json({ error: error.message });
  }
});

app.get('/api/workspace-files/download', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const workspaceDir = context.workspaceDir;
    const { fullPath } = resolveWorkspaceFilePath(workspaceDir, req.query.path || '');
    const stat = await fs.stat(fullPath);

    if (!stat.isFile()) {
      return res.status(400).json({ error: 'Workspace file path is not a file' });
    }

    res.download(fullPath);
  } catch (error) {
    console.error('❌ 下载 workspace file 失败:', error);
    res.status(statusForFileError(error)).json({ error: error.message });
  }
});

// 1.3.1 LLM helpers for workspace task generation
app.post('/api/llm/test', async (req, res) => {
  try {
    const {
      baseUrl,
      apiKey,
      model,
    } = req.body || {};

    const result = await callOpenAICompatibleChat({
      baseUrl,
      apiKey,
      model,
      messages: [
        { role: 'user', content: 'Reply with OK.' },
      ],
      temperature: 0,
      maxTokens: 16,
    });

    res.json({
      success: true,
      model,
      content: result.content || result.reasoningContent || '',
    });
  } catch (error) {
    console.error('❌ 测试 LLM 连接失败:', error.message);
    res.status(error.status || 500).json({ error: error.message });
  }
});

app.post('/api/llm/generate-task', async (req, res) => {
  try {
    const {
      baseUrl,
      apiKey,
      model,
      description,
      taskName,
      relativePath,
      taskContext,
    } = req.body || {};

    if (!description || !String(description).trim()) {
      return res.status(400).json({ error: 'Task description is required' });
    }

    const result = await callOpenAICompatibleChat({
      baseUrl,
      apiKey,
      model,
      messages: buildTaskGenerationMessages({
        description: String(description).trim(),
        taskName,
        relativePath,
        taskContext,
      }),
      temperature: 0.2,
      maxTokens: 4096,
    });

    const generated = extractGeneratedTask(result.content || result.reasoningContent, taskName, relativePath);
    if (!generated.code) {
      return res.status(502).json({ error: 'LLM response did not include task code' });
    }

    res.json({
      success: true,
      model,
      functionName: generated.functionName,
      relativePath: generated.relativePath,
      code: generated.code,
      notes: generated.notes,
      rawContent: result.content,
      warnings: generated.warnings,
    });
  } catch (error) {
    console.error('❌ 生成 workspace task 失败:', error.message);
    res.status(error.status || 500).json({ error: error.message });
  }
});

// 1.4 获取工作目录工作流列表
app.get('/api/workspace-workflows', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const workspaceDir = context.workspaceDir;
    const workflowsDir = path.join(workspaceDir, 'workflows');
    const files = await listWorkflowFiles(workflowsDir);
    const workflowItems = [];
    const errors = [];

    for (const filePath of files) {
      const relativePath = toPosixPath(path.relative(workspaceDir, filePath));
      try {
        const raw = await fs.readFile(filePath, 'utf-8');
        const payload = JSON.parse(raw);
        const workflow = normalizeWorkflowPayload(payload);
        const stat = await fs.stat(filePath);

        workflowItems.push({
          name: workflow.name,
          relativePath,
          nodeCount: workflow.nodes.length,
          edgeCount: workflow.edges.length,
          updatedAt: payload?.savedAt || payload?.exportedAt || stat.mtime.toISOString(),
          size: stat.size,
        });
      } catch (error) {
        errors.push({
          relativePath,
          error: error.message,
        });
      }
    }

    workflowItems.sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)));

    res.json({
      ...workspaceResponseFields(context),
      workflowsDir,
      workflows: workflowItems,
      errors,
    });
  } catch (error) {
    console.error('❌ 获取工作区工作流失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// 1.3.1 删除工作目录工作流
app.delete('/api/workspace-workflows', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      relativePath,
    } = req.body;

    if (!relativePath) {
      return res.status(400).json({ error: 'relativePath is required' });
    }

    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const workspaceDir = context.workspaceDir;
    const { relativePath: workflowPath, fullPath } = resolveWorkflowFile(workspaceDir, relativePath, 'workflow');
    await fs.unlink(fullPath);
    const manifest = await recordWorkspaceMutation(workspaceDir, 'workflow_deleted', {
      path: workflowPath,
    });

    console.log(`🗑️ 工作流已删除: ${workflowPath}`);
    res.json({
      success: true,
      workspaceId: manifest.workspace_id,
      workspaceDir,
      workspaceManifestVersion: Number(manifest.manifest_version || context.workspaceManifestVersion),
      relativePath: workflowPath,
    });
  } catch (error) {
    console.error('❌ 删除工作区工作流失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// 1.3.2 重命名工作目录工作流
app.patch('/api/workspace-workflows/rename', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      relativePath,
      name,
    } = req.body;

    if (!relativePath || !name || !String(name).trim()) {
      return res.status(400).json({ error: 'relativePath and name are required' });
    }

    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const workspaceDir = context.workspaceDir;
    const { relativePath: workflowPath, fullPath } = resolveWorkflowFile(workspaceDir, relativePath, name);
    const raw = await fs.readFile(fullPath, 'utf-8');
    const payload = JSON.parse(raw);
    const normalized = normalizeWorkflowPayload(payload);
    const workflowNodes = normalized.nodes.map((node) => stripNodeTaskCode(node, workspaceDir));
    const nextPayload = {
      schema: payload?.schema || 'maze-playground-workflow',
      version: Math.max(payload?.version || 1, 3),
      savedAt: new Date().toISOString(),
      workflow: {
        ...(payload?.workflow || {}),
        name: String(name).trim(),
        nodes: workflowNodes,
        edges: normalized.edges,
      },
    };

    await fs.writeFile(fullPath, JSON.stringify(nextPayload, null, 2), 'utf-8');
    const manifest = await recordWorkspaceMutation(workspaceDir, 'workflow_renamed', {
      path: workflowPath,
      name: String(name).trim(),
    });

    console.log(`✏️ 工作流已重命名: ${workflowPath}`);
    res.json({
      success: true,
      workspaceId: manifest.workspace_id,
      workspaceDir,
      workspaceManifestVersion: Number(manifest.manifest_version || context.workspaceManifestVersion),
      relativePath: workflowPath,
      workflow: nextPayload.workflow,
    });
  } catch (error) {
    console.error('❌ 重命名工作区工作流失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// 1.4 保存当前工作流到工作目录
app.post('/api/workspace-workflows/save', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      relativePath,
      name = 'Untitled Workflow',
      workflowId = null,
      nodes = [],
      edges = [],
    } = req.body;

    if (!Array.isArray(nodes) || !Array.isArray(edges)) {
      return res.status(400).json({ error: 'nodes and edges must be arrays' });
    }

    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const workspaceDir = context.workspaceDir;
    const workflowNodes = nodes.map((node) => stripNodeTaskCode(node, workspaceDir));
    const { relativePath: savedRelativePath, fullPath } = resolveWorkflowFile(workspaceDir, relativePath, name);
    const hydratedWorkflowNodes = await hydrateWorkspaceWorkflowNodes(workflowNodes, workspaceDir);
    const payload = {
      schema: 'maze-playground-workflow',
      version: 3,
      savedAt: new Date().toISOString(),
      workflow: {
        name,
        sourceWorkflowId: workflowId,
        nodes: workflowNodes,
        edges,
      },
    };

    await fs.mkdir(path.dirname(fullPath), { recursive: true });
    await fs.writeFile(fullPath, JSON.stringify(payload, null, 2), 'utf-8');
    const manifest = await recordWorkspaceMutation(workspaceDir, 'workflow_saved', {
      path: savedRelativePath,
      name,
    });

    console.log(`💾 工作流已保存到工作区: ${savedRelativePath}`);
    res.json({
      success: true,
      workspaceId: manifest.workspace_id,
      workspaceDir,
      workspaceManifestVersion: Number(manifest.manifest_version || context.workspaceManifestVersion),
      relativePath: savedRelativePath,
      workflow: {
        ...payload.workflow,
        nodes: hydratedWorkflowNodes,
      },
    });
  } catch (error) {
    console.error('❌ 保存工作区工作流失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// 1.5 从工作目录加载工作流
app.post('/api/workspace-workflows/load', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      relativePath,
    } = req.body;

    if (!relativePath) {
      return res.status(400).json({ error: 'relativePath is required' });
    }

    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const workspaceDir = context.workspaceDir;
    const { relativePath: loadedRelativePath, fullPath } = resolveWorkflowFile(workspaceDir, relativePath, 'workflow');
    const raw = await fs.readFile(fullPath, 'utf-8');
    const payload = JSON.parse(raw);
    const workflow = normalizeWorkflowPayload(payload);
    const importResult = await importTaskDefinitions(workspaceDir, workflow.includedTasks, workflow.name);
    workflow.nodes = await hydrateWorkspaceWorkflowNodes(
      workflow.nodes,
      workspaceDir,
      workflow.includedTasks,
      importResult.taskPathMap,
    );
    let manifest = context.manifest;
    if (importResult.imported.length > 0 || importResult.remapped.length > 0) {
      manifest = await recordWorkspaceMutation(workspaceDir, 'workflow_task_definitions_imported', {
        workflow_path: loadedRelativePath,
        imported_count: importResult.imported.length,
        remapped_count: importResult.remapped.length,
      });
    }

    res.json({
      success: true,
      workspaceId: manifest.workspace_id,
      workspaceDir,
      workspaceManifestVersion: Number(manifest.manifest_version || context.workspaceManifestVersion),
      relativePath: loadedRelativePath,
      workflow,
      importedTaskDefinitions: {
        imported: importResult.imported,
        skipped: importResult.skipped,
        remapped: importResult.remapped,
      },
    });
  } catch (error) {
    console.error('❌ 加载工作区工作流失败:', error);
    res.status(statusForFileError(error)).json({ error: error.message });
  }
});

// 1.6 导入外部工作流 payload，同时导入其任务定义
app.post('/api/workspace-workflows/import', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      payload,
    } = req.body;

    if (!payload) {
      return res.status(400).json({ error: 'payload is required' });
    }

    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const workspaceDir = context.workspaceDir;
    const workflow = normalizeWorkflowPayload(payload);
    const importResult = await importTaskDefinitions(workspaceDir, workflow.includedTasks, workflow.name);
    workflow.nodes = await hydrateWorkspaceWorkflowNodes(
      workflow.nodes,
      workspaceDir,
      workflow.includedTasks,
      importResult.taskPathMap,
    );
    const manifest = await recordWorkspaceMutation(workspaceDir, 'workflow_payload_imported', {
      workflow_name: workflow.name,
      imported_task_count: importResult.imported.length,
      remapped_task_count: importResult.remapped.length,
    });

    res.json({
      success: true,
      workspaceId: manifest.workspace_id,
      workspaceDir,
      workspaceManifestVersion: Number(manifest.manifest_version || context.workspaceManifestVersion),
      workflow,
      importedTaskDefinitions: {
        imported: importResult.imported,
        skipped: importResult.skipped,
        remapped: importResult.remapped,
      },
    });
  } catch (error) {
    console.error('❌ 导入工作区工作流失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// 1.7 Dynamic run inspector API proxy
app.get('/api/dynamic-runs', async (req, res) => {
  try {
    const params = new URLSearchParams();
    if (req.query.status) params.set('status', String(req.query.status));
    if (req.query.limit) params.set('limit', String(req.query.limit));
    const query = params.toString();
    const result = await callMazeCore(`/dynamic_runs${query ? `?${query}` : ''}`);
    res.json({
      success: true,
      runs: result.runs || [],
    });
  } catch (error) {
    console.error('❌ 获取 dynamic runs 失败:', error);
    res.status(error.status || 500).json({ error: error.message, payload: error.payload });
  }
});

app.get('/api/runs', async (req, res) => {
  try {
    const params = new URLSearchParams();
    if (req.query.status) params.set('status', String(req.query.status));
    if (req.query.kind) params.set('kind', String(req.query.kind));
    if (req.query.limit) params.set('limit', String(req.query.limit));
    if (req.query.detail !== undefined) params.set('detail', String(req.query.detail));
    const query = params.toString();
    const result = await callMazeCore(`/runs${query ? `?${query}` : ''}`);
    res.json({
      success: true,
      runs: (result.runs || []).filter((run) => run?.metadata?.benchmark !== 'gaia'),
    });
  } catch (error) {
    console.error('Failed to get runs:', error);
    res.status(error.status || 500).json({ error: error.message, payload: error.payload });
  }
});

app.get('/api/cluster/worker-profiles', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const payload = await loadWorkerProfiles(context.workspaceDir);
    res.json({
      success: true,
      workspaceId: context.workspaceId,
      workspaceDir: context.workspaceDir,
      profiles: payload.profiles.map((profile) => redactedWorkerProfile(profile, context.workspaceDir)),
    });
  } catch (error) {
    console.error('Failed to list worker profiles:', error);
    res.status(500).json({ error: error.message });
  }
});

app.post('/api/cluster/worker-profiles', async (req, res) => {
  try {
    const { workspaceId, workspaceDir: requestedWorkspaceDir, profile: rawProfile = {}, password } = req.body || {};
    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const payload = await loadWorkerProfiles(context.workspaceDir);
    const existing = payload.profiles.find((item) => item.id === rawProfile.id);
    const profile = sanitizeWorkerProfileInput(rawProfile, existing);
    const incomingPassword = password || rawProfile.auth?.password || rawProfile.password;
    rememberWorkerPassword(context.workspaceDir, profile.id, incomingPassword);
    const nextProfiles = [
      ...payload.profiles.filter((item) => item.id !== profile.id),
      profile,
    ].sort((left, right) => String(left.name).localeCompare(String(right.name)));
    await saveWorkerProfiles(context.workspaceDir, nextProfiles);
    res.json({
      success: true,
      workspaceId: context.workspaceId,
      workspaceDir: context.workspaceDir,
      profile: redactedWorkerProfile(profile, context.workspaceDir),
    });
  } catch (error) {
    console.error('Failed to save worker profile:', error);
    res.status(400).json({ error: error.message });
  }
});

app.post('/api/cluster/worker-profiles/test-draft', async (req, res) => {
  try {
    const { workspaceId, workspaceDir: requestedWorkspaceDir, profile: rawProfile = {}, password } = req.body || {};
    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const profile = sanitizeWorkerProfileInput(rawProfile, null);
    const incomingPassword = password || rawProfile.auth?.password || rawProfile.password || '';
    if (profile.auth?.method === 'password' && !incomingPassword) {
      return res.status(400).json({ error: 'password is required to test password auth' });
    }
    const test = await runWorkerDraftTest(profile, {
      password: incomingPassword,
      timeoutMs: req.body?.timeoutMs || 30000,
    });
    rememberWorkerPassword(context.workspaceDir, profile.id, incomingPassword);
    res.json({
      success: true,
      workspaceId: context.workspaceId,
      workspaceDir: context.workspaceDir,
      profile: redactedWorkerProfile(profile, context.workspaceDir),
      test,
    });
  } catch (error) {
    console.error('Failed to test worker profile draft:', error);
    res.status(500).json({ error: error.message, result: error.result || null });
  }
});

app.delete('/api/cluster/worker-profiles/:profileId', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const profileId = safeWorkerProfileId(req.params.profileId);
    const payload = await loadWorkerProfiles(context.workspaceDir);
    await saveWorkerProfiles(context.workspaceDir, payload.profiles.filter((item) => item.id !== profileId));
    activeWorkerProfileSecrets.delete(workerSecretKey(context.workspaceDir, profileId));
    res.json({ success: true, workspaceId: context.workspaceId, workspaceDir: context.workspaceDir, profileId });
  } catch (error) {
    console.error('Failed to delete worker profile:', error);
    res.status(500).json({ error: error.message });
  }
});

async function runWorkerProfileAction(context, profileId, action, options = {}) {
  const payload = await loadWorkerProfiles(context.workspaceDir);
  const profile = payload.profiles.find((item) => item.id === profileId);
  if (!profile) {
    throw new Error(`worker profile not found: ${profileId}`);
  }
  const password = getWorkerPassword(context.workspaceDir, profileId, options.password || '');
  if (options.password) {
    rememberWorkerPassword(context.workspaceDir, profileId, options.password);
  }
  const command = remoteWorkerCommand(profile, action);
  const result = await runSshCommand(profile, command, { password, timeoutMs: options.timeoutMs || 60000 });
  profile.lastAction = {
    action,
    ok: true,
    at: new Date().toISOString(),
    stdoutTail: String(result.stdout || '').split(/\r?\n/).slice(-20).join('\n'),
    stderrTail: String(result.stderr || '').split(/\r?\n/).slice(-20).join('\n'),
  };
  await saveWorkerProfiles(
    context.workspaceDir,
    payload.profiles.map((item) => (item.id === profile.id ? profile : item)),
  );
  return { profile: redactedWorkerProfile(profile, context.workspaceDir), result };
}

app.post('/api/cluster/worker-profiles/:profileId/:action', async (req, res) => {
  try {
    const action = String(req.params.action || '');
    if (req.params.profileId === 'bulk') {
      return res.status(404).json({ error: 'use /api/cluster/worker-profiles/bulk' });
    }
    if (!['test', 'start', 'restart', 'stop', 'logs'].includes(action)) {
      return res.status(400).json({ error: 'unsupported worker action' });
    }
    const context = await resolveWorkspaceContext(req.body || {});
    const profileId = safeWorkerProfileId(req.params.profileId);
    const output = await runWorkerProfileAction(context, profileId, action, {
      password: req.body?.password,
      timeoutMs: req.body?.timeoutMs,
    });
    res.json({
      success: true,
      workspaceId: context.workspaceId,
      workspaceDir: context.workspaceDir,
      action,
      profile: output.profile,
      result: output.result,
    });
  } catch (error) {
    console.error('Failed to run worker profile action:', error);
    const result = error.result || null;
    res.status(500).json({ error: error.message, result });
  }
});

app.post('/api/cluster/worker-profiles/bulk', async (req, res) => {
  try {
    const action = String(req.body?.action || '');
    if (!['test', 'start', 'restart', 'stop', 'logs'].includes(action)) {
      return res.status(400).json({ error: 'unsupported worker action' });
    }
    const context = await resolveWorkspaceContext(req.body || {});
    const ids = Array.isArray(req.body?.profileIds) ? req.body.profileIds.map((id) => safeWorkerProfileId(id)) : [];
    const passwordByProfileId = req.body?.passwordByProfileId || {};
    const results = [];
    for (const profileId of ids) {
      try {
        const output = await runWorkerProfileAction(context, profileId, action, {
          password: passwordByProfileId[profileId],
          timeoutMs: req.body?.timeoutMs,
        });
        results.push({ profileId, ok: true, profile: output.profile, result: output.result });
      } catch (error) {
        results.push({ profileId, ok: false, error: error.message, result: error.result || null });
      }
    }
    res.json({ success: true, workspaceId: context.workspaceId, workspaceDir: context.workspaceDir, action, results });
  } catch (error) {
    console.error('Failed to run worker profile bulk action:', error);
    res.status(500).json({ error: error.message });
  }
});

app.post('/api/cluster/console/run', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.body || {});
    const target = String(req.body?.target || 'head');
    const command = String(req.body?.command || '').trim();
    const timeoutMs = Math.min(Math.max(Number(req.body?.timeoutMs || 30000), 1000), 120000);
    const password = req.body?.password;
    if (!command) {
      return res.status(400).json({ error: 'command is required' });
    }
    if (command.length > 4000) {
      return res.status(400).json({ error: 'command is too long' });
    }
    if (target !== 'head') {
      return res.status(400).json({ error: 'console commands run on the head node only' });
    }
    let result = await runLocalCommand(command, { timeoutMs, cwd: PROJECT_ROOT });
    result = limitCommandResult(result);

    res.json({
      success: true,
      workspaceId: context.workspaceId,
      workspaceDir: context.workspaceDir,
      target: 'head',
      targetLabel: 'head',
      command,
      timeoutMs,
      result,
      ranAt: new Date().toISOString(),
    });
  } catch (error) {
    console.error('Failed to run cluster console command:', error);
    res.status(500).json({ error: error.message, result: error.result || null });
  }
});

app.get('/api/cluster/resources', async (req, res) => {
  try {
    const result = await callMazeCore('/cluster/resources');
    res.json(result);
  } catch (error) {
    console.error('Failed to get cluster resources:', error);
    res.status(error.status || 500).json({ error: error.message || 'Failed to get cluster resources' });
  }
});

app.get('/api/cluster/queues', async (req, res) => {
  try {
    const [result, coreRuns] = await Promise.all([
      callMazeCore('/cluster/queues'),
      listCoreStaticRuns(),
    ]);
    const gaiaRunIds = new Set(
      coreRuns
        .filter((run) => run?.metadata?.benchmark === 'gaia')
        .map((run) => String(run.run_id || ''))
        .filter(Boolean),
    );
    res.json(redactGaiaRunIdentifiers(result, gaiaRunIds));
  } catch (error) {
    console.error('Failed to get cluster queues:', error);
    res.status(error.status || 500).json({ error: error.message || 'Failed to get cluster queues' });
  }
});

app.get('/api/models', async (req, res) => {
  try {
    const result = await callMazeCore('/models');
    res.json(result);
  } catch (error) {
    console.error('Failed to get models:', error);
    res.status(error.status || 500).json({ error: error.message || 'Failed to get models' });
  }
});

app.get('/api/resource-history', async (req, res) => {
  try {
    const result = await callMazeCore('/resource-history');
    res.json(result);
  } catch (error) {
    console.error('Failed to get resource history:', error);
    res.status(error.status || 500).json({ error: error.message || 'Failed to get resource history' });
  }
});

app.post('/api/models/config', async (req, res) => {
  try {
    const result = await callMazeCore('/models/config', {
      method: 'POST',
      body: req.body || {},
    });
    res.json(result);
  } catch (error) {
    console.error('Failed to update model config:', error);
    res.status(error.status || 500).json({ error: error.message || 'Failed to update model config' });
  }
});

app.post('/api/models/test', async (req, res) => {
  try {
    const result = await callMazeCore('/models/test', {
      method: 'POST',
      body: req.body || {},
    });
    res.json(result);
  } catch (error) {
    console.error('Failed to test model:', error);
    res.status(error.status || 500).json({ error: error.message || 'Failed to test model' });
  }
});

app.post('/api/cluster/nodes/:nodeId/:action', async (req, res) => {
  try {
    const action = String(req.params.action || '');
    if (!['disable', 'enable'].includes(action)) {
      return res.status(400).json({ error: 'unsupported cluster node action' });
    }
    const nodeId = encodeURIComponent(req.params.nodeId);
    const result = await callMazeCore(`/cluster/nodes/${nodeId}/${action}`, { method: 'POST' });
    res.json(result);
  } catch (error) {
    console.error('Failed to control cluster node:', error);
    res.status(error.status || 500).json({ error: error.message || 'Failed to control cluster node' });
  }
});

app.get('/api/runs/:runId', async (req, res) => {
  try {
    const result = await callMazeCore(`/runs/${encodeURIComponent(req.params.runId)}`);
    res.json({
      success: true,
      run: requirePublicCoreRun(result.run),
    });
  } catch (error) {
    console.error('Failed to get run:', error);
    res.status(error.status || 500).json({ error: error.message, payload: error.payload });
  }
});

app.get('/api/runs/:runId/tasks', async (req, res) => {
  try {
    await requirePublicCoreRunId(req.params.runId);
    const result = await callMazeCore(`/runs/${encodeURIComponent(req.params.runId)}/tasks`);
    res.json({
      success: true,
      runId: result.run_id,
      tasks: result.tasks || [],
    });
  } catch (error) {
    console.error('Failed to get run tasks:', error);
    res.status(error.status || 500).json({ error: error.message, payload: error.payload });
  }
});

app.get('/api/runs/:runId/tasks/:taskId', async (req, res) => {
  try {
    await requirePublicCoreRunId(req.params.runId);
    const result = await callMazeCore(
      `/runs/${encodeURIComponent(req.params.runId)}/tasks/${encodeURIComponent(req.params.taskId)}`
    );
    res.json({
      success: true,
      runId: result.run_id,
      task: result.task,
    });
  } catch (error) {
    console.error('Failed to get run task:', error);
    res.status(error.status || 500).json({ error: error.message, payload: error.payload });
  }
});

app.get('/api/runs/:runId/events', async (req, res) => {
  try {
    await requirePublicCoreRunId(req.params.runId);
    const params = new URLSearchParams();
    if (req.query.after !== undefined) params.set('after', String(req.query.after));
    const query = params.toString();
    const result = await callMazeCore(`/runs/${encodeURIComponent(req.params.runId)}/events${query ? `?${query}` : ''}`);
    res.json({
      success: true,
      runId: result.run_id,
      events: result.events || [],
    });
  } catch (error) {
    console.error('Failed to get run events:', error);
    res.status(error.status || 500).json({ error: error.message, payload: error.payload });
  }
});

app.get('/api/runs/:runId/logs', async (req, res) => {
  try {
    await requirePublicCoreRunId(req.params.runId);
    const params = new URLSearchParams();
    if (req.query.tail !== undefined) params.set('tail', String(req.query.tail));
    if (req.query.taskId !== undefined) params.set('task_id', String(req.query.taskId));
    const query = params.toString();
    const result = await callMazeCore(`/runs/${encodeURIComponent(req.params.runId)}/logs${query ? `?${query}` : ''}`);
    res.json({
      success: true,
      runId: result.run_id,
      taskId: result.task_id,
      lineCount: result.line_count || 0,
      lines: result.lines || [],
    });
  } catch (error) {
    console.error('Failed to get run logs:', error);
    res.status(error.status || 500).json({ error: error.message, payload: error.payload });
  }
});

app.get('/api/runs/:runId/artifacts', async (req, res) => {
  try {
    await requirePublicCoreRunId(req.params.runId);
    const result = await callMazeCore(`/runs/${encodeURIComponent(req.params.runId)}/artifacts`);
    res.json({
      success: true,
      runId: result.run_id,
      artifacts: result.artifacts || [],
    });
  } catch (error) {
    console.error('Failed to get run artifacts:', error);
    res.status(error.status || 500).json({ error: error.message, payload: error.payload });
  }
});

app.get('/api/runs/:runId/tasks/:taskId/artifacts', async (req, res) => {
  try {
    await requirePublicCoreRunId(req.params.runId);
    const result = await callMazeCore(
      `/runs/${encodeURIComponent(req.params.runId)}/tasks/${encodeURIComponent(req.params.taskId)}/artifacts`
    );
    res.json({
      success: true,
      runId: result.run_id,
      taskId: result.task_id,
      artifacts: result.artifacts || [],
    });
  } catch (error) {
    console.error('Failed to get run task artifacts:', error);
    res.status(error.status || 500).json({ error: error.message, payload: error.payload });
  }
});

app.get('/api/artifacts/sha256/:sha256/metadata', async (req, res) => {
  try {
    const result = await callMazeCore(`/artifacts/sha256/${encodeURIComponent(req.params.sha256)}/metadata`);
    res.json({
      success: true,
      artifact: result.artifact,
    });
  } catch (error) {
    console.error('Failed to get artifact metadata:', error);
    res.status(error.status || 500).json({ error: error.message, payload: error.payload });
  }
});

app.get('/api/artifacts/sha256/:sha256', async (req, res) => {
  const clientRequest = createClientDisconnectAbort(req, res);
  try {
    const { response, body } = await fetchMazeCoreBody(
      `/artifacts/sha256/${encodeURIComponent(req.params.sha256)}`,
      { signal: clientRequest.signal },
    );
    if (!response.ok) {
      const message = body.toString('utf-8');
      return res.status(response.status).send(message || `Maze core request failed: ${response.status}`);
    }
    const contentType = response.headers.get('content-type') || 'application/octet-stream';
    const disposition = req.query.disposition === 'inline' ? 'inline' : 'attachment';
    res.setHeader('Content-Type', contentType);
    res.setHeader('Content-Disposition', `${disposition}; filename="${req.params.sha256}"`);
    res.send(body);
  } catch (error) {
    console.error('Failed to download artifact:', error);
    if (res.destroyed) return;
    res.status(error.status || 500).json({ error: error.message || 'Failed to download artifact' });
  } finally {
    clientRequest.dispose();
  }
});

app.post('/api/runs/:runId/cancel', async (req, res) => {
  try {
    await requirePublicCoreRunId(req.params.runId);
    const result = await callMazeCore(`/runs/${encodeURIComponent(req.params.runId)}/cancel`, {
      method: 'POST',
      body: req.body || {},
    });
    revokeWorkspaceAgentCapabilities(req.params.runId);
    res.json({
      success: true,
      runId: result.run_id,
      status: result.run_status,
    });
  } catch (error) {
    console.error('Failed to cancel run:', error);
    res.status(error.status || 500).json({ error: error.message, payload: error.payload });
  }
});

app.post('/api/runs/:runId/retry', async (req, res) => {
  try {
    await requirePublicCoreRunId(req.params.runId);
    const result = await callMazeCore(`/runs/${encodeURIComponent(req.params.runId)}/retry`, {
      method: 'POST',
      body: req.body || {},
    });
    res.json({
      success: true,
      runId: result.run_id,
      workflowId: result.workflow_id,
      retriedFromRunId: result.retried_from_run_id,
      spec: result.spec,
    });
  } catch (error) {
    console.error('Failed to retry run:', error);
    res.status(error.status || 500).json({ error: error.message, payload: error.payload });
  }
});

app.get('/api/dynamic-runs/:runId', async (req, res) => {
  try {
    const result = await callMazeCore(`/dynamic_runs/${encodeURIComponent(req.params.runId)}`);
    res.json({
      success: true,
      run: result.run,
    });
  } catch (error) {
    console.error('❌ 获取 dynamic run 失败:', error);
    res.status(error.status || 500).json({ error: error.message, payload: error.payload });
  }
});

app.get('/api/dynamic-runs/:runId/events', async (req, res) => {
  try {
    const params = new URLSearchParams();
    if (req.query.after !== undefined) params.set('after', String(req.query.after));
    const query = params.toString();
    const result = await callMazeCore(`/dynamic_runs/${encodeURIComponent(req.params.runId)}/events${query ? `?${query}` : ''}`);
    res.json({
      success: true,
      runId: result.run_id,
      events: result.events || [],
    });
  } catch (error) {
    console.error('❌ 获取 dynamic run events 失败:', error);
    res.status(error.status || 500).json({ error: error.message, payload: error.payload });
  }
});

app.post('/api/dynamic-runs/:runId/events', async (req, res) => {
  try {
    const result = await callMazeCore(`/dynamic_runs/${encodeURIComponent(req.params.runId)}/events`, {
      method: 'POST',
      body: req.body || {},
    });
    res.json({
      success: true,
      runId: result.run_id,
      event: result.event,
    });
  } catch (error) {
    console.error('Failed to write dynamic run event:', error);
    res.status(error.status || 500).json({ error: error.message, payload: error.payload });
  }
});

app.post('/api/dynamic-runs/:runId/permission-requests/:requestId/decision', async (req, res) => {
  try {
    const action = String(req.body?.action || req.body?.decision?.action || '').trim().toLowerCase();
    const reason = String(req.body?.reason || req.body?.decision?.reason || '').trim();
    if (!['allow', 'deny'].includes(action)) {
      res.status(400).json({ success: false, error: 'Permission decision action must be allow or deny' });
      return;
    }
    const result = await callMazeCore(
      `/dynamic_runs/${encodeURIComponent(req.params.runId)}/permission_requests/${encodeURIComponent(req.params.requestId)}/decision`,
      {
        method: 'POST',
        body: {
          decision: {
            action,
            reason,
            decided_by: 'playground',
          },
        },
      },
    );
    res.json({
      success: true,
      runId: result.run_id,
      request: result.request,
    });
  } catch (error) {
    console.error('Failed to decide dynamic run permission request:', error);
    res.status(error.status || 500).json({ success: false, error: error.message, payload: error.payload });
  }
});

app.delete('/api/dynamic-runs/:runId', async (req, res) => {
  try {
    const result = await callMazeCore(`/dynamic_runs/${encodeURIComponent(req.params.runId)}`, {
      method: 'DELETE',
    });
    res.json({
      success: true,
      runId: result.run_id,
      deleted: result.deleted,
    });
  } catch (error) {
    console.error('❌ 删除 dynamic run 失败:', error);
    res.status(error.status || 500).json({ error: error.message, payload: error.payload });
  }
});

app.post('/api/dynamic-runs/cleanup', async (req, res) => {
  try {
    const result = await callMazeCore('/dynamic_runs/cleanup', {
      method: 'POST',
      body: req.body || {},
    });
    res.json({
      success: true,
      cleanup: result.cleanup,
    });
  } catch (error) {
    console.error('❌ 清理 dynamic runs 失败:', error);
    res.status(error.status || 500).json({ error: error.message, payload: error.payload });
  }
});

app.get('/api/mcp/profiles', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const profiles = await listMcpProfiles(context.workspaceDir);
    res.json({ success: true, ...workspaceResponseFields(context), profiles });
  } catch (error) {
    console.error('Failed to list MCP profiles:', error);
    res.status(500).json({ success: false, error: error.message });
  }
});

app.post('/api/mcp/profiles', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      name,
      description = '',
      mcpServers,
	    } = req.body || {};
	    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
	    const profileName = safeMcpProfileName(name);
	    const normalizedMcpServers = validateMcpServers(mcpServers);
	    const now = new Date().toISOString();
	    const existing = await readJsonFile(mcpProfilePath(context.workspaceDir, profileName), null);
	    const keepLastTest = existing?.lastTest && sameJsonValue(existing?.mcpServers, normalizedMcpServers);
	    const profile = {
	      schema: 'maze_mcp_profile',
	      schema_version: 1,
	      name: profileName,
	      description: String(description || ''),
	      createdAt: existing?.createdAt || now,
	      updatedAt: now,
	      lastTest: keepLastTest ? existing.lastTest : null,
	      mcpServers: normalizedMcpServers,
	    };
    await writeJsonAtomic(mcpProfilePath(context.workspaceDir, profileName), profile);
    const manifest = await recordWorkspaceMutation(context.workspaceDir, 'mcp_profile_saved', {
      profile: profileName,
    });
    res.json({
      success: true,
      ...workspaceResponseFields({ ...context, workspaceManifestVersion: Number(manifest.manifest_version || context.workspaceManifestVersion) }),
      profile: summarizeMcpProfile(profile),
    });
  } catch (error) {
    console.error('Failed to save MCP profile:', error);
    res.status(mcpApiErrorStatus(error)).json({ success: false, error: error.message });
  }
});

app.delete('/api/mcp/profiles/:name', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const profileName = safeMcpProfileName(req.params.name);
    await fs.unlink(mcpProfilePath(context.workspaceDir, profileName)).catch((error) => {
      if (error.code !== 'ENOENT') throw error;
    });
    const manifest = await recordWorkspaceMutation(context.workspaceDir, 'mcp_profile_deleted', {
      profile: profileName,
    });
    res.json({
      success: true,
      ...workspaceResponseFields({ ...context, workspaceManifestVersion: Number(manifest.manifest_version || context.workspaceManifestVersion) }),
      profileName,
    });
  } catch (error) {
    console.error('Failed to delete MCP profile:', error);
    res.status(mcpApiErrorStatus(error)).json({ success: false, error: error.message });
  }
});

app.post('/api/mcp/profiles/:name/copy', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      name,
      description,
    } = req.body || {};
    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const sourceName = safeMcpProfileName(req.params.name);
    const targetName = safeMcpProfileName(name);
    if (sourceName === targetName) {
      const error = new Error('Copy target profile name must be different from the source profile name');
      error.status = 400;
      throw error;
    }
    if (await fileExists(mcpProfilePath(context.workspaceDir, targetName))) {
      const error = new Error(`MCP profile already exists: ${targetName}`);
      error.status = 409;
      throw error;
    }
    const source = await loadMcpProfile(context.workspaceDir, sourceName);
    const now = new Date().toISOString();
    const target = {
      ...source,
      name: targetName,
      description: description === undefined
        ? `${String(source.description || '').trim() || sourceName} copy`
        : String(description || ''),
      createdAt: now,
      updatedAt: now,
      lastTest: null,
    };
    await writeJsonAtomic(mcpProfilePath(context.workspaceDir, targetName), target);
    const manifest = await recordWorkspaceMutation(context.workspaceDir, 'mcp_profile_copied', {
      source: sourceName,
      profile: targetName,
    });
    res.json({
      success: true,
      ...workspaceResponseFields({ ...context, workspaceManifestVersion: Number(manifest.manifest_version || context.workspaceManifestVersion) }),
      profile: summarizeMcpProfile(target),
      sourceProfileName: sourceName,
    });
  } catch (error) {
    console.error('Failed to copy MCP profile:', error);
    res.status(mcpApiErrorStatus(error)).json({ success: false, error: error.message });
  }
});

app.get('/api/mcp/profiles/:name/export', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const profileName = safeMcpProfileName(req.params.name);
    const profile = await loadMcpProfile(context.workspaceDir, profileName);
    res.json({
      success: true,
      ...workspaceResponseFields(context),
      export: buildMcpProfileExport(profile),
    });
  } catch (error) {
    console.error('Failed to export MCP profile:', error);
    res.status(mcpApiErrorStatus(error)).json({ success: false, error: error.message });
  }
});

app.post('/api/mcp/profiles/import', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      name,
      description,
      mcpServers,
      redactedMcpServers,
      profile,
      export: exportBundle,
    } = req.body || {};
    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const bundle = profile || exportBundle || {};
    const targetName = safeMcpProfileName(name || bundle.name);
    const rawServers = mcpServers || redactedMcpServers || bundle.mcpServers || bundle.redactedMcpServers || bundle.profile?.redactedMcpServers;
    rejectRedactedMcpPlaceholders(rawServers);
    const normalizedMcpServers = validateMcpServers(rawServers);
    const now = new Date().toISOString();
    const existing = await readJsonFile(mcpProfilePath(context.workspaceDir, targetName), null);
    const imported = {
      schema: 'maze_mcp_profile',
      schema_version: 1,
      name: targetName,
      description: String(description ?? bundle.description ?? ''),
      createdAt: existing?.createdAt || now,
      updatedAt: now,
      lastTest: null,
      mcpServers: normalizedMcpServers,
    };
    await writeJsonAtomic(mcpProfilePath(context.workspaceDir, targetName), imported);
    const manifest = await recordWorkspaceMutation(context.workspaceDir, 'mcp_profile_imported', {
      profile: targetName,
    });
    res.json({
      success: true,
      ...workspaceResponseFields({ ...context, workspaceManifestVersion: Number(manifest.manifest_version || context.workspaceManifestVersion) }),
      profile: summarizeMcpProfile(imported),
    });
  } catch (error) {
    console.error('Failed to import MCP profile:', error);
    res.status(mcpApiErrorStatus(error)).json({ success: false, error: error.message });
  }
});

app.get('/api/agent/sessions', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const sessions = await listAgentSessions(context.workspaceDir);
    res.json({
      success: true,
      ...workspaceResponseFields(context),
      sessions,
    });
  } catch (error) {
    console.error('Failed to list Workspace Agent sessions:', error);
    res.status(error.status || 500).json({ success: false, error: error.message });
  }
});

app.post('/api/agent/sessions', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.body || {});
    const session = await createAgentSessionRecord(context, req.body || {});
    res.json({
      success: true,
      ...workspaceResponseFields(context),
      session: agentSessionSummary(session),
      messages: [],
    });
  } catch (error) {
    console.error('Failed to create Workspace Agent session:', error);
    res.status(error.status || 500).json({ success: false, error: error.message });
  }
});

app.patch('/api/agent/sessions/:id', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.body || {});
    const session = await updateAgentSessionRecord(context.workspaceDir, req.params.id, req.body || {});
    res.json({
      success: true,
      ...workspaceResponseFields(context),
      session: agentSessionSummary(session),
    });
  } catch (error) {
    console.error('Failed to update Workspace Agent session:', error);
    res.status(error.code === 'ENOENT' ? 404 : (error.status || 500)).json({ success: false, error: error.message });
  }
});

app.delete('/api/agent/sessions/:id', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query || {});
    await deleteAgentSessionRecord(context.workspaceDir, req.params.id);
    const sessions = await listAgentSessions(context.workspaceDir);
    res.json({
      success: true,
      ...workspaceResponseFields(context),
      deletedSessionId: req.params.id,
      sessions,
    });
  } catch (error) {
    console.error('Failed to delete Workspace Agent session:', error);
    res.status(error.code === 'ENOENT' ? 404 : (error.status || 500)).json({ success: false, error: error.message });
  }
});

app.get('/api/agent/sessions/:id/export', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query || {});
    const bundle = await buildAgentSessionExport(context, req.params.id);
    res.json({
      success: true,
      ...workspaceResponseFields(context),
      export: bundle,
    });
  } catch (error) {
    console.error('Failed to export Workspace Agent session:', error);
    res.status(error.code === 'ENOENT' ? 404 : (error.status || 500)).json({ success: false, error: error.message });
  }
});

app.get('/api/agent/sessions/:id/messages', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const session = await loadAgentSession(context.workspaceDir, req.params.id);
    const view = await loadAgentSessionView(context, session);
    res.json({
      success: true,
      ...workspaceResponseFields(context),
      session: agentSessionSummary(session),
      messages: view.messages,
      drafts: view.drafts,
      unavailableTurns: view.unavailableTurns,
    });
  } catch (error) {
    console.error('Failed to read Workspace Agent messages:', error);
    res.status(error.code === 'ENOENT' ? 404 : (error.status || 500)).json({ success: false, error: error.message });
  }
});

app.post('/api/internal/workspace-agent/tool', async (req, res) => {
  const capability = workspaceAgentCapability(req);
  if (!capability) {
    res.status(403).json({ ok: false, error: 'Workspace Agent capability denied' });
    return;
  }

  try {
    const name = String(req.body?.name || '').trim();
    const input = req.body?.input;
    if (!name || !input || typeof input !== 'object' || Array.isArray(input)) {
      res.status(400).json({ ok: false, error: 'Tool name and object input are required' });
      return;
    }
    if (!capability.tools.has(name)) {
      res.status(403).json({ ok: false, error: 'Workspace Agent tool is not allowed' });
      return;
    }
    const result = await executeAgentTool(
      capability.context,
      name,
      input,
      capability.runtime,
    );
    res.json(redactSecrets(result));
  } catch (error) {
    res.status(error.status || 500).json({
      ok: false,
      error: redactSecretText(error.message),
      code: error.code || undefined,
    });
  }
});

app.post('/api/agent/runs', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.body || {});
    const result = await runWorkspaceAgent(context, req.body || {});
    res.status(202).json({
      ...result,
      ...workspaceResponseFields(context),
    });
  } catch (error) {
    console.error('Failed to run Workspace Agent:', error);
    res.status(error.status || 500).json({ success: false, error: redactSecretText(error.message) });
  }
});


app.get('/api/agent/drafts/:id', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const draft = await loadAgentDraft(context.workspaceDir, req.params.id);
    if (draft.run?.runId) {
      const coreRun = await requirePublicCoreRunId(draft.run.runId).catch(() => null);
      if (coreRun) {
        draft.run = {
          ...draft.run,
          status: coreRun.status,
          finishedAt: coreRun.finished_time
            ? new Date(coreRun.finished_time * 1000).toISOString()
            : undefined,
          error: coreRun.error_summary || undefined,
        };
      }
    }
    res.json({
      success: true,
      ...workspaceResponseFields(context),
      draft: agentDraftPublic(draft),
    });
  } catch (error) {
    console.error('Failed to read Workspace Agent draft:', error);
    res.status(error.code === 'ENOENT' ? 404 : (error.status || 500)).json({ success: false, error: error.message });
  }
});

app.post('/api/agent/drafts/:id/validate', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.body || {});
    const draft = await validateAgentWorkflowDraft(context, req.params.id);
    res.json({
      success: true,
      ...workspaceResponseFields(context),
      draft,
    });
  } catch (error) {
    console.error('Failed to validate Workspace Agent draft:', error);
    res.status(error.status || 500).json({ success: false, error: error.message });
  }
});

app.post('/api/agent/drafts/:id/dismiss', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.body || {});
    const draft = await dismissAgentWorkflowDraft(context, req.params.id, {
      reason: req.body?.reason,
    });
    res.json({
      success: true,
      ...workspaceResponseFields(context),
      draft,
    });
  } catch (error) {
    console.error('Failed to dismiss Workspace Agent draft:', error);
    res.status(error.code === 'ENOENT' ? 404 : (error.status || 500)).json({ success: false, error: error.message });
  }
});

app.post('/api/agent/drafts/:id/save', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.body || {});
    const result = await saveAgentWorkflowDraft(context, req.params.id, {
      confirmed: req.body?.confirmed === true,
      relativePath: req.body?.relativePath,
      workflowId: req.body?.workflowId,
    });
    res.json({
      success: true,
      ...workspaceResponseFields(context),
      ...result,
    });
  } catch (error) {
    console.error('Failed to save Workspace Agent draft:', error);
    res.status(error.status || 500).json({ success: false, error: error.message, code: error.code });
  }
});

app.post('/api/agent/drafts/:id/run', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.body || {});
    const result = await runAgentWorkflowDraft(context, req.params.id, {
      confirmed: req.body?.confirmed === true,
    });
    res.json({
      success: true,
      ...workspaceResponseFields(context),
      ...result,
    });
  } catch (error) {
    console.error('Failed to run Workspace Agent draft:', error);
    res.status(error.status || 500).json({ success: false, error: error.message, code: error.code });
  }
});

app.post('/api/react-runs/start', async (req, res) => {
  try {
    const {
      mode = 'local',
      prompt,
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      maxSteps,
      maxTokens,
      timeoutSeconds,
      taskTimeout,
      llm,
      skills = [],
      skillDirs,
      maxSkillChars,
	      execBackend,
	      permissionPolicy,
	      permissionAskTimeoutSeconds,
	      mcpServers,
	      mcpProfileName,
	    } = req.body || {};
    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const workspaceDir = context.workspaceDir;
    const resolvedMcp = await resolveMcpServersForRequest(context, { mcpServers, mcpProfileName });
    const normalizedMcpServers = resolvedMcp.mcpServers;
    const mcpServerSummary = summarizeMcpServers(normalizedMcpServers);

    const extraEnv = {};
	    if (llm?.apiKey) {
	      extraEnv.MAZE_REACT_API_KEY = String(llm.apiKey);
	    }
	    const askTimeout = Number(permissionAskTimeoutSeconds);
	    if (Number.isFinite(askTimeout) && askTimeout > 0) {
	      extraEnv.MAZE_AGENT_PERMISSION_ASK_TIMEOUT_SECONDS = String(Math.min(Math.max(askTimeout, 1), 600));
	    }

    const started = await startReactWorkflowProcess(
      {
        mode,
        prompt,
        workspaceId: context.workspaceId,
        workspaceDir,
        workspaceManifestVersion: context.workspaceManifestVersion,
        maxSteps,
        maxTokens,
        timeoutSeconds,
        taskTimeout,
        baseUrl: llm?.baseUrl,
        model: llm?.model,
        skills,
        skillDirs,
        maxSkillChars,
        execBackend,
        permissionPolicy,
        mcpServers: normalizedMcpServers,
        mcpServerSummary,
        mcpProfileName: resolvedMcp.profileName,
        mcpProfileSummary: resolvedMcp.profileSummary,
      },
      extraEnv,
    );

    res.json({
      ...started,
      ...workspaceResponseFields(context),
      mcpServers: mcpServerSummary,
      mcpProfileName: resolvedMcp.profileName || undefined,
      mcpProfile: resolvedMcp.profileSummary || undefined,
    });
  } catch (error) {
    console.error('Failed to start ReAct workflow:', error);
    const bridgePayload = error.bridgePayload || null;
    res.status(mcpApiErrorStatus(error)).json({
      success: false,
      error: bridgePayload?.error || error.message,
      runId: bridgePayload?.runId,
      status: bridgePayload?.status,
    });
  }
});

app.post('/api/mcp/discover', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      mcpServers,
      mcpProfileName,
    } = req.body || {};
    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const resolvedMcp = await resolveMcpServersForRequest(context, { mcpServers, mcpProfileName });
    const normalizedMcpServers = resolvedMcp.mcpServers;
    const mcpServerSummary = summarizeMcpServers(normalizedMcpServers);
    const result = await runMcpDiscoveryProcess({ mcpServers: normalizedMcpServers });
    if (!result.success) {
      if (resolvedMcp.profileName) {
        await updateMcpProfileLastTest(context.workspaceDir, resolvedMcp.profileName, {
          status: 'failed',
          testedAt: new Date().toISOString(),
          serverCount: (result.servers || mcpServerSummary || []).length,
          toolCount: 0,
          tools: [],
          error: result.error || 'MCP discovery failed',
          errorType: result.errorType,
        }).catch((updateError) => {
          console.error('Failed to update MCP profile failed test status:', updateError);
        });
      }
      res.status(400).json({
        success: false,
        error: result.error || 'MCP discovery failed',
        errorType: result.errorType,
        servers: result.servers || mcpServerSummary,
        mcpProfileName: resolvedMcp.profileName || undefined,
      });
      return;
    }
    let updatedProfileSummary = resolvedMcp.profileSummary;
    if (resolvedMcp.profileName) {
      updatedProfileSummary = await updateMcpProfileLastTest(context.workspaceDir, resolvedMcp.profileName, {
        status: 'ok',
        testedAt: new Date().toISOString(),
        serverCount: result.serverCount ?? mcpServerSummary.length,
        toolCount: result.toolCount ?? (result.tools || []).length,
        tools: summarizeMcpDiscoveredTools(result.tools || []),
      }).catch((updateError) => {
        console.error('Failed to update MCP profile test status:', updateError);
        return resolvedMcp.profileSummary;
      });
    }
    res.json({
      success: true,
      ...workspaceResponseFields(context),
      servers: result.servers || mcpServerSummary,
      tools: result.tools || [],
      serverCount: result.serverCount ?? mcpServerSummary.length,
      toolCount: result.toolCount ?? (result.tools || []).length,
      mcpProfileName: resolvedMcp.profileName || undefined,
      mcpProfile: updatedProfileSummary || undefined,
    });
  } catch (error) {
    console.error('Failed to discover MCP tools:', error);
    const requestedProfileName = req.body?.mcpProfileName ? safeMcpProfileName(req.body.mcpProfileName) : '';
    if (requestedProfileName && error.status !== 404) {
      try {
        const context = await resolveWorkspaceContext({
          workspaceId: req.body?.workspaceId,
          workspaceDir: req.body?.workspaceDir,
        });
        await updateMcpProfileLastTest(context.workspaceDir, requestedProfileName, {
          status: 'failed',
          testedAt: new Date().toISOString(),
          serverCount: null,
          toolCount: 0,
          tools: [],
          error: error.message,
          errorType: error.missingEnv ? 'missing_env' : undefined,
        });
      } catch (updateError) {
        console.error('Failed to update MCP profile exception test status:', updateError);
      }
    }
    res.status(mcpApiErrorStatus(error)).json({ success: false, error: error.message });
  }
});

// 1.8 GAIA benchmark submission and public trace
app.post('/api/benchmarks/gaia/runs', async (req, res) => {
  const body = req.body || {};
  const workflow = String(body.workflow || '').trim();
  const sampleRef = String(body.sampleRef || body.sample_ref || '').trim();
  const mazeWorkflowId = String(body.mazeWorkflowId || body.maze_workflow_id || '').trim();
  const timeoutSeconds = Number(body.timeoutSeconds ?? body.timeout_seconds);
  const inputs = body.inputs;
  const finalOutputRefs = body.finalOutputRefs ?? body.final_output_refs;

  if (!GAIA_TRACE_WORKFLOWS.has(workflow)) {
    return res.status(400).json({ error: 'workflow must be reason or file' });
  }
  if (!GAIA_SAMPLE_REF_PATTERN.test(sampleRef)) {
    return res.status(400).json({ error: 'sampleRef must be an opaque GAIA sample reference' });
  }
  if (!mazeWorkflowId || mazeWorkflowId.length > 200) {
    return res.status(400).json({ error: 'mazeWorkflowId is required' });
  }
  if (!inputs || typeof inputs !== 'object' || Array.isArray(inputs)) {
    return res.status(400).json({ error: 'inputs must be an object' });
  }
  if (!finalOutputRefs || typeof finalOutputRefs !== 'object' || Array.isArray(finalOutputRefs)) {
    return res.status(400).json({ error: 'finalOutputRefs must be an object' });
  }
  if (!Number.isFinite(timeoutSeconds) || timeoutSeconds <= 0) {
    return res.status(400).json({ error: 'timeoutSeconds must be positive' });
  }
  let context;
  let submissionToken;
  let executionFile;
  try {
    context = await resolveGaiaWorkspaceContext(body, { includeExecutionWorkspace: true });
    submissionToken = normalizeGaiaSubmissionToken(
      body.submissionToken || body.submission_token,
    );
    executionFile = validateGaiaExecutionFile(
      workflow,
      body.executionFile ?? body.execution_file,
    );
    if (
      executionFile
      && String(inputs.supplementary_path || '') !== executionFile.name
    ) {
      const error = new Error('inputs.supplementary_path must match executionFile.name');
      error.status = 400;
      throw error;
    }
  } catch (error) {
    return res.status(error.status || 400).json({ error: error.message });
  }

  const submissionFingerprint = gaiaSubmissionFingerprint({
    workflow,
    sampleRef,
    workspaceId: context.workspaceId,
    mazeWorkflowId,
    timeoutSeconds,
    inputs,
    finalOutputRefs,
    executionFile,
  });
  const idempotencyKey = gaiaCoreIdempotencyKey(submissionToken);

  try {
    const outcome = await withStaticRunWriteQueue(
      context.workspaceDir,
      `gaia-sample:${sampleRef}`,
      async () => {
        const existing = await findGaiaTraceBySampleRef(context.workspaceDir, sampleRef);
        let snapshot = existing;
        let isExisting = Boolean(existing);
        if (existing) {
          requireGaiaSubmissionToken(existing, submissionToken);
          if (
            existing.gaia_private?.submission_fingerprint !== submissionFingerprint
            || existing.gaia_private?.idempotency_key !== idempotencyKey
          ) {
            const error = new Error('GAIA sample reference was already used for a different submission');
            error.status = 409;
            throw error;
          }

          if (!snapshot.maze_run_id && !TERMINAL_STATIC_RUN_STATUSES.has(snapshot.status)) {
            try {
              snapshot = await withStaticRunWriteQueue(
                context.workspaceDir,
                snapshot.run_id,
                async () => reconcileGaiaTraceUnlocked(
                  context.workspaceDir,
                  snapshot.run_id,
                  await listCoreStaticRuns(),
                  { markMissing: false },
                ),
              );
            } catch (error) {
              console.error(`GAIA pre-submit reconciliation remains retryable for ${snapshot.run_id}:`, error.message);
            }
          }
          if (snapshot.maze_run_id || TERMINAL_STATIC_RUN_STATUSES.has(snapshot.status)) {
            if (!snapshot.maze_run_id) {
              const error = new Error('Maze workflow submission previously failed');
              error.status = snapshot.status === 'failed' ? 502 : 409;
              error.playgroundRunId = snapshot.run_id;
              error.mazeRunId = null;
              throw error;
            }
            return {
              statusCode: 200,
              payload: {
                success: true,
                idempotent: true,
                ...gaiaTraceResponse(snapshot, { includeMazeRunId: true }),
              },
            };
          }
        } else {
          isExisting = false;
          const playgroundRunId = uuidv4();
          snapshot = createGaiaTraceSnapshot({
            runId: playgroundRunId,
            workflow,
            sampleRef,
            context,
            mazeWorkflowId,
            submissionTokenHash: sha256Text(submissionToken),
            idempotencyKey,
            submissionFingerprint,
          });
          try {
            await ensureGaiaRunDirectory(context.workspaceDir, playgroundRunId);
            await saveStaticRun(context.workspaceDir, snapshot);
            ({ snapshot } = await appendAndApplyStaticRunEvent(
              context.workspaceDir,
              playgroundRunId,
              {
                type: 'workflow_started',
                data: {},
                timestamp: new Date().toISOString(),
              },
            ));
          } catch (error) {
            console.error('Failed to persist GAIA Playground run before submission');
            const persistError = new Error('Failed to persist Playground run');
            persistError.status = 500;
            throw persistError;
          }
        }

        const submission = await submitGaiaTraceToCore({
          context,
          snapshot,
          inputs,
          finalOutputRefs,
          timeoutSeconds,
          executionFile,
        });
        return {
          statusCode: isExisting ? 200 : 201,
          payload: {
            success: true,
            ...(isExisting ? { idempotent: true } : {}),
            ...(submission.recovered ? { recovered: true } : {}),
            ...gaiaTraceResponse(submission.snapshot, { includeMazeRunId: true }),
          },
        };
      },
    );
    return res.status(outcome.statusCode).json(outcome.payload);
  } catch (error) {
    return res.status(error.status || 500).json({
      error: error.message,
      ...(error.playgroundRunId ? { playgroundRunId: error.playgroundRunId } : {}),
      ...(Object.prototype.hasOwnProperty.call(error, 'mazeRunId')
        ? { mazeRunId: error.mazeRunId }
        : {}),
    });
  }
});

app.post('/api/benchmarks/gaia/runs/lookup', async (req, res) => {
  const body = req.body || {};
  const sampleRef = String(body.sampleRef || body.sample_ref || '').trim();
  if (!GAIA_SAMPLE_REF_PATTERN.test(sampleRef)) {
    return res.status(400).json({ error: 'sampleRef must be an opaque GAIA sample reference' });
  }
  try {
    const context = await resolveGaiaWorkspaceContext(body);
    const submissionToken = normalizeGaiaSubmissionToken(
      body.submissionToken || body.submission_token,
    );
    const existing = await findGaiaTraceBySampleRef(context.workspaceDir, sampleRef);
    if (!existing) {
      return res.status(404).json({ error: 'GAIA Playground run not found' });
    }
    requireGaiaSubmissionToken(existing, submissionToken);
    let snapshot = existing;
    if (!TERMINAL_STATIC_RUN_STATUSES.has(snapshot.status)) {
      snapshot = await withStaticRunWriteQueue(
        context.workspaceDir,
        snapshot.run_id,
        async () => reconcileGaiaTraceUnlocked(
          context.workspaceDir,
          snapshot.run_id,
          await listCoreStaticRuns(),
          { markMissing: false },
        ),
      );
    }
    return res.json({
      success: true,
      ...gaiaTraceResponse(snapshot, { includeMazeRunId: true }),
    });
  } catch (error) {
    return res.status(error.status || statusForFileError(error)).json({ error: error.message });
  }
});

app.post('/api/benchmarks/gaia/runs/:runId/finish', async (req, res) => {
  try {
    const body = req.body || {};
    const context = await resolveGaiaWorkspaceContext(body);
    const expectedStatus = String(body.status || '').trim().toLowerCase();
    const expectedTerminal = expectedStatus ? GAIA_TERMINAL_EVENTS[expectedStatus] : null;
    if (expectedStatus && !expectedTerminal) {
      return res.status(400).json({ error: 'status is not a terminal Maze run status' });
    }
    const snapshot = await withStaticRunWriteQueue(
      context.workspaceDir,
      req.params.runId,
      async () => {
        let current = requireGaiaTraceRun(
          await loadStaticRun(context.workspaceDir, req.params.runId),
        );
        requireGaiaSubmissionToken(current, body.submissionToken || body.submission_token);
        current = await ensureGaiaTraceMappingUnlocked(context.workspaceDir, req.params.runId);
        const coreRun = requireMappedCoreGaiaRun(
          current,
          await loadCoreRun(current.maze_run_id),
        );
        const actualTerminal = gaiaTerminalForCoreStatus(coreRun.status);
        if (!actualTerminal) {
          const error = new Error('Maze run is not terminal');
          error.status = 409;
          throw error;
        }
        if (expectedTerminal && expectedTerminal.status !== actualTerminal.status) {
          const error = new Error('Maze run terminal status does not match the expected status');
          error.status = 409;
          throw error;
        }
        if (TERMINAL_STATIC_RUN_STATUSES.has(current.status)) {
          if (!gaiaLocalStatusMatchesCoreTerminal(current, actualTerminal)) {
            const error = new Error('Playground and Maze terminal statuses conflict');
            error.status = 409;
            throw error;
          }
          return current;
        }
        ({ snapshot: current } = await appendAndApplyStaticRunEventUnlocked(
          context.workspaceDir,
          current.run_id,
          {
            type: actualTerminal.type,
            data: {},
            timestamp: new Date().toISOString(),
          },
        ));
        return current;
      },
    );
    res.json({ success: true, ...gaiaTraceResponse(snapshot) });
  } catch (error) {
    res.status(error.status || statusForFileError(error)).json({ error: error.message });
  }
});

app.post('/api/benchmarks/gaia/runs/:runId/cancel', async (req, res) => {
  try {
    const body = req.body || {};
    const context = await resolveGaiaWorkspaceContext(body);
    const requestedOutcome = String(body.outcome || 'canceled').trim().toLowerCase();
    if (!['canceled', 'cancelled', 'timed_out'].includes(requestedOutcome)) {
      return res.status(400).json({ error: 'outcome must be canceled or timed_out' });
    }
    const outcome = await withStaticRunWriteQueue(
      context.workspaceDir,
      req.params.runId,
      async () => {
        let current = requireGaiaTraceRun(
          await loadStaticRun(context.workspaceDir, req.params.runId),
        );
        requireGaiaSubmissionToken(current, body.submissionToken || body.submission_token);
        current = await ensureGaiaTraceMappingUnlocked(context.workspaceDir, req.params.runId);
        let coreRun = requireMappedCoreGaiaRun(
          current,
          await loadCoreRun(current.maze_run_id),
        );
        let actualTerminal = gaiaTerminalForCoreStatus(coreRun.status);
        let cancellationRequested = false;

        if (TERMINAL_STATIC_RUN_STATUSES.has(current.status)) {
          if (!actualTerminal || !gaiaLocalStatusMatchesCoreTerminal(current, actualTerminal)) {
            const error = new Error('Playground and Maze terminal statuses conflict');
            error.status = 409;
            throw error;
          }
          return { snapshot: current, pending: false };
        }

        if (!actualTerminal) {
          cancellationRequested = true;
          let cancellationError = null;
          try {
            await callMazeCore(`/runs/${encodeURIComponent(current.maze_run_id)}/cancel`, {
              method: 'POST',
              body: {
                reason: requestedOutcome === 'timed_out'
                  ? 'GAIA validation timeout'
                  : 'GAIA validation canceled',
              },
            });
          } catch (error) {
            cancellationError = error;
            console.error(`Maze cancellation failed for Playground run ${current.run_id}`);
          }
          try {
            coreRun = requireMappedCoreGaiaRun(
              current,
              await loadCoreRun(current.maze_run_id),
            );
          } catch (error) {
            if (!cancellationError) throw error;
            const cancelError = new Error('Maze run cancellation could not be verified');
            cancelError.status = 502;
            throw cancelError;
          }
          actualTerminal = gaiaTerminalForCoreStatus(coreRun.status);
          if (!actualTerminal) {
            if (cancellationError) {
              const cancelError = new Error('Maze run cancellation failed');
              cancelError.status = 502;
              throw cancelError;
            }
            return { snapshot: current, pending: true };
          }
        }

        const terminal = (
          cancellationRequested
          && requestedOutcome === 'timed_out'
          && actualTerminal.status === 'canceled'
        ) ? GAIA_TERMINAL_EVENTS.timed_out : actualTerminal;
        ({ snapshot: current } = await appendAndApplyStaticRunEventUnlocked(
          context.workspaceDir,
          current.run_id,
          {
            type: terminal.type,
            data: {},
            timestamp: new Date().toISOString(),
          },
        ));
        return { snapshot: current, pending: false };
      },
    );
    res.status(outcome.pending ? 202 : 200).json({
      success: true,
      ...gaiaTraceResponse(outcome.snapshot),
    });
  } catch (error) {
    res.status(error.status || statusForFileError(error)).json({ error: error.message });
  }
});

// 1.9 Static workflow run history
app.get('/api/workflow-runs/static', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const workspaceDir = context.workspaceDir;
    const status = req.query.status ? String(req.query.status) : null;
    const limit = req.query.limit ? Number(req.query.limit) : null;
    const fullRuns = await listStaticRunFilesForWorkspace(workspaceDir);
    await reconcileActiveGaiaRunsOnRead(workspaceDir, fullRuns);
    let runs = await listStaticRunFilesForWorkspace(workspaceDir, { summary: true });
    if (status) {
      runs = runs.filter((run) => run.status === status);
    }
    runs.sort((a, b) => Number(b.created_time || 0) - Number(a.created_time || 0));
    if (Number.isFinite(limit)) {
      runs = runs.slice(0, Math.max(0, limit));
    }
    const responseWorkspaceFields = fullRuns.some(isGaiaTrace)
      ? {
          workspaceId: context.workspaceId,
          workspaceManifestVersion: context.workspaceManifestVersion,
        }
      : workspaceResponseFields(context);
    res.json({ success: true, ...responseWorkspaceFields, runs });
  } catch (error) {
    console.error('❌ 获取 static workflow runs 失败:', error);
    res.status(500).json({ error: error.message });
  }
});

app.get('/api/workflow-runs/static/:runId', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const workspaceDir = context.workspaceDir;
    let run = await loadStaticRun(workspaceDir, req.params.runId);
    if (await reconcileActiveGaiaRunsOnRead(workspaceDir, [run])) {
      run = await loadStaticRun(workspaceDir, req.params.runId);
    }
    res.json({
      success: true,
      ...publicStaticRunWorkspaceFields(context, run),
      run: publicStaticRunSnapshot(run),
    });
  } catch (error) {
    const status = statusForFileError(error);
    if (status === 404) {
      console.warn(`⚠️ static workflow run not found: ${req.params.runId}`);
    } else {
      console.error('❌ 获取 static workflow run 失败:', error);
    }
    res.status(status).json({ error: error.message });
  }
});

app.get('/api/workflow-runs/static/:runId/events', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const workspaceDir = context.workspaceDir;
    const after = req.query.after !== undefined ? Number(req.query.after) : null;
    let run = await loadStaticRun(workspaceDir, req.params.runId);
    if (await reconcileActiveGaiaRunsOnRead(workspaceDir, [run])) {
      run = await loadStaticRun(workspaceDir, req.params.runId);
    }
    const events = await loadStaticRunEvents(workspaceDir, req.params.runId, after);
    res.json({
      success: true,
      ...publicStaticRunWorkspaceFields(context, run),
      runId: req.params.runId,
      events: events.map((event) => publicStaticRunEvent(run, event)),
    });
  } catch (error) {
    const status = statusForFileError(error);
    if (status === 404) {
      console.warn(`⚠️ static workflow run events not found: ${req.params.runId}`);
    } else {
      console.error('❌ 获取 static workflow run events 失败:', error);
    }
    res.status(status).json({ error: error.message });
  }
});

app.get('/api/workflow-runs/static/:runId/artifacts/download', async (req, res) => {
  const clientRequest = createClientDisconnectAbort(req, res);
  try {
    const context = await resolveWorkspaceContext(req.query);
    const workspaceDir = context.workspaceDir;
    const run = await loadStaticRun(workspaceDir, req.params.runId);
    const taskId = String(req.query.taskId || '');
    const artifactPath = String(req.query.path || '');

    if (!taskId || !artifactPath) {
      return res.status(400).json({ error: 'taskId and path are required' });
    }

    const located = findStaticRunArtifact(run, { taskId, artifactPath });
    const artifact = located?.artifact || null;
    if (!artifact) {
      return res.status(404).json({ error: 'Artifact not found' });
    }

    if (artifact.storage_path) {
      const fullPath = path.resolve(String(artifact.storage_path || ''));
      const allowed = staticRunStorageRoots(workspaceDir, req.params.runId).some((root) => (
        fullPath === root || fullPath.startsWith(root + path.sep)
      ));
      if (!allowed) {
        return res.status(400).json({ error: 'Artifact path is outside this run' });
      }

      const disposition = req.query.disposition === 'inline' ? 'inline' : 'attachment';
      res.setHeader('Content-Type', artifact.mime || 'application/octet-stream');
      res.setHeader('Content-Disposition', `${disposition}; filename="${encodeURIComponent(artifact.name || path.basename(fullPath))}"`);
      return res.sendFile(fullPath);
    }

    if (!artifact.sha256) {
      return res.status(404).json({ error: 'Artifact storage path not found' });
    }

    const { response, body } = await fetchMazeCoreBody(
      `/artifacts/sha256/${encodeURIComponent(artifact.sha256)}`,
      { signal: clientRequest.signal },
    );
    if (!response.ok) {
      return res.status(response.status).json({ error: `Failed to download artifact: HTTP ${response.status}` });
    }
    const disposition = req.query.disposition === 'inline' ? 'inline' : 'attachment';
    res.setHeader('Content-Type', artifact.mime || 'application/octet-stream');
    res.setHeader('Content-Disposition', `${disposition}; filename="${encodeURIComponent(artifact.name || path.basename(artifact.path || 'artifact'))}"`);
    res.send(body);
  } catch (error) {
    console.error('❌ 下载 static workflow artifact 失败:', error);
    if (res.destroyed) return;
    res.status(statusForFileError(error)).json({ error: error.message });
  } finally {
    clientRequest.dispose();
  }
});

// 2. 解析自定义函数
app.post('/api/parse-custom-function', async (req, res) => {
  try {
    const { code } = req.body;
    console.log('🔍 解析自定义函数...');
    
    if (!code || !code.trim()) {
      return res.status(400).json({ error: 'Code cannot be empty' });
    }
    
    const result = await callPython('parse_custom_function', { code });
    
    if (result.error) {
      console.error('❌ 解析失败:', result.error);
      return res.status(400).json({ error: result.error, traceback: result.traceback });
    }
    
    console.log('✅ 解析成功:', result.name);
    res.json(result);
  } catch (error) {
    console.error('❌ 解析自定义函数失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// Submit a Playground workflow to Maze Core.
app.post('/api/workflows/:id/run', async (req, res) => {
  try {
    const { id } = req.params;
    const workflow = req.body?.workflow;
    if (!workflow) return res.status(400).json({ error: 'workflow is required' });
    if (!Array.isArray(workflow.nodes) || workflow.nodes.length === 0) {
      return res.status(400).json({ error: 'Workflow has no task nodes' });
    }
    const context = await resolveWorkspaceContext(req.body || {});
    const submission = await submitPlaygroundWorkflow({
      workflow,
      context,
      playgroundWorkflowId: id,
      workflowPath: req.body?.relativePath,
    });

    res.json({
      message: 'Workflow started running',
      workflowId: id,
      runId: submission.runId,
      coreWorkflowId: submission.coreWorkflowId,
      submissionId: submission.submissionId,
      ...workspaceResponseFields(context),
    });
  } catch (error) {
    console.error('❌ 运行工作流失败:', error);
    res.status(error.status || 500).json({ error: error.message, code: error.code });
  }
});

// ========== 健康检查 ==========

app.get('/health', (req, res) => {
  res.json({ 
    status: 'ok',
    timestamp: new Date().toISOString()
  });
});

// ========== 启动服务器 ==========

const PORT = process.env.PORT || 3001;

if (process.env.MAZE_PLAYGROUND_NO_LISTEN !== '1') {
  server.listen(PORT, () => {
    console.log('\n' + '='.repeat(60));
    console.log('  🚀 Maze Playground Backend Server');
    console.log('='.repeat(60));
    console.log(`\n✅ HTTP Server:   http://localhost:${PORT}`);
    console.log(`✅ API Endpoint:  http://localhost:${PORT}/api`);
    console.log(`✅ Health Check:  http://localhost:${PORT}/health`);
    console.log(`✅ Python Bridge: ${PYTHON_BIN}`);
    console.log('\n📡 等待前端连接...\n');
  });
}

export const __artifactSecurityTestHooks = Object.freeze({
  cleanupRecoveredGaiaStaging,
  cleanupWorkspaceArtifacts,
  ensureManagedGaiaWorkspaceContext,
  requirePrivateGaiaStagingRoot,
  stageGaiaExecutionFile,
});

export const __workspaceAgentTestHooks = Object.freeze({
  agentMessagesFromDynamicTurn,
  agentSessionSummary,
  agentToolDefinitions,
  appendAgentSessionTurn,
  buildAgentSessionExport,
  buildWorkspaceAgentPrompt,
  bindWorkspaceAgentCapability,
  collectAgentDraftIdsFromEvents,
  createAgentSessionRecord,
  createWorkspaceAgentCapability,
  loadAgentSession,
  revokeWorkspaceAgentCapabilities,
  workspaceAgentCapability,
});

// 优雅关闭
process.on('SIGINT', () => {
  console.log('\n\n👋 正在关闭服务器...');

  server.close(() => {
    console.log('✅ 服务器已关闭');
    process.exit(0);
  });
});
