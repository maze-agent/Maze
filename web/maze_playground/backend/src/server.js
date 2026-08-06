import express from 'express';
import cors from 'cors';
import { spawn } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';
import http from 'http';
import fs from 'fs/promises';
import fsSync from 'fs';
import crypto from 'crypto';
import { tmpdir } from 'os';
import { BUILTIN_TASK_ALIASES, compileWorkflowToDagSpec } from './workflow_dag_spec.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const app = express();
const server = http.createServer(app);

app.use(cors());
app.use(express.json({ limit: '50mb' }));

const PROJECT_ROOT = path.resolve(__dirname, '../../../..');
const WORKSPACE_ROOT_DIR = path.resolve(process.env.MAZE_WORKSPACE_ROOT_DIR || process.env.MAZE_WORKSPACE_DIR || path.join(PROJECT_ROOT, 'workspaces'));
const WORKSPACES_DIR = path.resolve(process.env.MAZE_WORKSPACES_DIR || WORKSPACE_ROOT_DIR);
const DEFAULT_WORKSPACE_ID = process.env.MAZE_DEFAULT_WORKSPACE_ID || 'default';
const DEFAULT_WORKSPACE_DIR = path.join(WORKSPACES_DIR, DEFAULT_WORKSPACE_ID);
const LEGACY_WORKSPACE_DIR = WORKSPACE_ROOT_DIR;
const SYSTEM_CATALOG_DIR = path.resolve(process.env.MAZE_SYSTEM_CATALOG_DIR || path.join(PROJECT_ROOT, 'system_catalog'));
const MAZE_CORE_URL = process.env.MAZE_CORE_URL || 'http://localhost:8000';
const MAZE_CORE_REQUEST_TIMEOUT_MS = Math.min(
  5 * 60 * 1000,
  Math.max(100, Number(process.env.MAZE_CORE_REQUEST_TIMEOUT_MS) || 30 * 1000),
);
const systemWorkflowLoadQueues = new Map();
const workspaceTaskSaveQueues = new Map();
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
    files_dir: 'files',
    workflows_dir: 'workflows',
    tasks_dir: 'tasks',
    runs_dir: 'runs',
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

async function ensureWorkspaceDirs(workspaceDir) {
  const resolved = resolveWorkspaceDirInput(workspaceDir);
  await fs.mkdir(resolved, { recursive: true });
  await fs.mkdir(path.join(resolved, 'tasks'), { recursive: true });
  await fs.mkdir(path.join(resolved, 'workflows'), { recursive: true });
  await fs.mkdir(path.join(resolved, 'files'), { recursive: true });
  await fs.mkdir(path.join(resolved, 'cluster_workers'), { recursive: true });
  await ensureWorkspaceManifest(resolved);
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
  for (const name of ['workflows', 'tasks']) {
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

async function catalogItemMetadata(type, fullPath, entry) {
  if (type === 'workflows' && entry.isFile() && entry.name.endsWith('.json')) {
    try {
      const payload = JSON.parse(await fs.readFile(fullPath, 'utf-8'));
      const workflow = payload?.workflow || payload || {};
      return {
        name: workflow.name || entry.name,
        description: workflow.description || payload.description || '',
        tags: Array.isArray(workflow.tags || payload.tags) ? (workflow.tags || payload.tags) : [],
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

function resolveWritableTaskDefinitionFile(workspaceDir, relativePath) {
  if (typeof relativePath !== 'string' || !relativePath.trim()) {
    throw badRequestError('relativePath must be a non-empty string');
  }
  const rawPath = relativePath.trim();
  if (
    rawPath.includes('\0')
    || rawPath.includes('\\')
    || path.posix.isAbsolute(rawPath)
    || isWindowsDrivePath(rawPath)
    || rawPath.split('/').some((part) => !part || part === '.' || part === '..')
  ) {
    throw badRequestError('Task path must be a POSIX relative path inside workspace/tasks');
  }
  return resolveTaskDefinitionFile(workspaceDir, rawPath);
}

async function requireSafeTaskWriteTarget(workspaceDir, fullPath) {
  const tasksDir = path.resolve(workspaceDir, 'tasks');
  const tasksStat = await fs.lstat(tasksDir);
  if (!tasksStat.isDirectory() || tasksStat.isSymbolicLink()) {
    throw badRequestError('Workspace tasks directory must not be a symbolic link');
  }

  const relativeParent = path.relative(tasksDir, path.dirname(fullPath));
  let currentDir = tasksDir;
  for (const part of relativeParent ? relativeParent.split(path.sep) : []) {
    currentDir = path.join(currentDir, part);
    let stat;
    try {
      stat = await fs.lstat(currentDir);
    } catch (error) {
      if (error?.code !== 'ENOENT') throw error;
      try {
        await fs.mkdir(currentDir);
      } catch (mkdirError) {
        if (mkdirError?.code !== 'EEXIST') throw mkdirError;
      }
      stat = await fs.lstat(currentDir);
    }
    if (!stat.isDirectory() || stat.isSymbolicLink()) {
      throw badRequestError('Task path parent directories must not be symbolic links');
    }
  }

  const targetStat = await fs.lstat(fullPath).catch((error) => {
    if (error?.code === 'ENOENT') return null;
    throw error;
  });
  if (targetStat && (!targetStat.isFile() || targetStat.isSymbolicLink())) {
    throw badRequestError('Task path must target a regular file, not a symbolic link');
  }
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

async function listCoreStaticRuns({ detail = true } = {}) {
  const payload = await callMazeCore(`/runs?kind=static&detail=${detail ? 'true' : 'false'}`);
  if (!Array.isArray(payload?.runs)) {
    const error = new Error('Maze Core returned a malformed static run list');
    error.status = 502;
    throw error;
  }
  return payload.runs;
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

async function writeTextAtomic(filePath, content, options = {}) {
  if (typeof content !== 'string') {
    throw badRequestError('code must be a string');
  }
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  const tmpPath = `${filePath}.${process.pid}.${Date.now()}.${Math.random().toString(16).slice(2)}.tmp`;
  const existing = await fs.lstat(filePath).catch((error) => {
    if (error?.code === 'ENOENT') return null;
    throw error;
  });
  if (existing && (!existing.isFile() || existing.isSymbolicLink())) {
    throw badRequestError('Atomic text writes require a regular file target');
  }

  try {
    await fs.writeFile(tmpPath, content, {
      encoding: 'utf-8',
      flag: 'wx',
      ...(existing ? { mode: existing.mode & 0o777 } : {}),
    });
    if (existing) {
      await fs.chmod(tmpPath, existing.mode & 0o777);
    }
    if (options.rootDir) {
      const [rootDir, parentDir] = await Promise.all([
        fs.realpath(options.rootDir),
        fs.realpath(path.dirname(filePath)),
      ]);
      if (parentDir !== rootDir && !parentDir.startsWith(rootDir + path.sep)) {
        throw badRequestError('Task path parent escaped the workspace tasks directory');
      }
    }
    await fs.rename(tmpPath, filePath);
  } catch (error) {
    await fs.rm(tmpPath, { force: true }).catch(() => {});
    throw error;
  }
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

async function currentClusterHeadUrl() {
  const result = await callMazeCore('/cluster/resources', { timeoutMs: 5000 });
  const headNodeIp = String(result.cluster?.head_node_ip || '').trim();
  if (!headNodeIp) {
    throw new Error('Maze Core did not report a head node address');
  }
  const url = new URL(MAZE_CORE_URL);
  url.hostname = headNodeIp;
  return url.toString().replace(/\/$/, '');
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
    'WORKER_PID=""',
    `if [ -f ${quotedPidPath} ]; then WORKER_PID="$(cat ${quotedPidPath})"; fi`,
    'if [ -n "$WORKER_PID" ] && kill -0 "$WORKER_PID" 2>/dev/null; then kill "$WORKER_PID" 2>/dev/null || true; fi',
    'for i in 1 2 3 4 5; do [ -z "$WORKER_PID" ] || ! kill -0 "$WORKER_PID" 2>/dev/null || sleep 1; done',
    'if [ -n "$WORKER_PID" ] && kill -0 "$WORKER_PID" 2>/dev/null; then kill -KILL "$WORKER_PID" 2>/dev/null || true; fi',
    `pkill -f "[p]ython -m maze.cli.cli start --worker --addr" 2>/dev/null || true`,
    'sleep 1',
    `pkill -KILL -f "[p]ython -m maze.cli.cli start --worker --addr" 2>/dev/null || true`,
    'timeout 20s ray stop --force >/dev/null 2>&1 || true',
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
    'WORKER_PID=$!',
    'echo "$WORKER_PID" > "$PWD/logs/maze_worker_remote.pid"',
    'for attempt in $(seq 1 60); do',
    `  if grep -q ${shellQuote('===Success to register worker===')} "$WORKER_LOG"; then`,
    '    printf "REMOTE_WORKER_PID=%s\\nREMOTE_WORKER_LOG=%s\\n" "$WORKER_PID" "$WORKER_LOG"',
    '    exit 0',
    '  fi',
    '  if ! kill -0 "$WORKER_PID" 2>/dev/null; then',
    '    echo "Remote Maze worker exited before registration" >&2',
    '    tail -80 "$WORKER_LOG" >&2 || true',
    '    exit 1',
    '  fi',
    '  sleep 1',
    'done',
    'echo "Timed out waiting for the remote Maze worker to register" >&2',
    'tail -80 "$WORKER_LOG" >&2 || true',
    'exit 1',
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

async function promoteArtifactIntoWorkspace(context, input = {}, options = {}) {
  const {
    targetPath,
    artifact = {},
    runId,
    taskId,
    path: artifactPath,
    sha256,
    overwrite = true,
  } = input || {};

  const sourceSha = String(sha256 || artifact.sha256 || '').trim().toLowerCase();
  const sourceArtifactPath = String(artifactPath || artifact.path || artifact.name || sourceSha || '').trim();
  const destinationPath = targetPath || sourceArtifactPath;

  if (!destinationPath) {
    const error = new Error('targetPath is required');
    error.status = 400;
    throw error;
  }
  if (!/^[0-9a-f]{64}$/.test(sourceSha)) {
    const error = new Error('artifact sha256 is required');
    error.status = 400;
    throw error;
  }

  const workspaceDir = context.workspaceDir;
  const { fullPath, filesDir, relativePath } = resolveWorkspaceFilePath(workspaceDir, destinationPath);
  if (!overwrite && await fileExists(fullPath)) {
    const error = new Error(`Workspace file already exists: ${relativePath}`);
    error.status = 409;
    throw error;
  }

  await fs.mkdir(path.dirname(fullPath), { recursive: true });
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

  const file = await describeWorkspaceFile(filesDir, fullPath);
  const manifest = await recordWorkspaceMutation(workspaceDir, 'artifact_promoted', {
    path: file.relativePath,
    runId: runId || artifact.run_id || null,
    taskId: taskId || artifact.taskId || artifact.task_id || artifact.producer_task_id || null,
    sha256: sourceSha,
  });

  return {
    success: true,
    workspaceId: manifest.workspace_id,
    workspaceDir,
    workspaceManifestVersion: Number(manifest.manifest_version || context.workspaceManifestVersion),
    file,
  };
}

function withKeyedQueue(queues, key, operation) {
  const previous = queues.get(key) || Promise.resolve();
  const current = previous
    .catch(() => {})
    .then(operation);
  const tail = current.then(
    () => undefined,
    () => undefined,
  );

  queues.set(key, tail);
  tail.finally(() => {
    if (queues.get(key) === tail) {
      queues.delete(key);
    }
  });

  return current;
}

function withSystemWorkflowLoadQueue(workspaceDir, operation) {
  return withKeyedQueue(systemWorkflowLoadQueues, path.resolve(workspaceDir), operation);
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

async function saveWorkspaceTaskSource(workspaceDir, relativePath, code) {
  if (typeof code !== 'string') {
    throw badRequestError('code must be a string');
  }
  const target = resolveWritableTaskDefinitionFile(workspaceDir, relativePath);
  // ponytail: One backend owns workspace authoring; serialize only the target file.
  return withKeyedQueue(workspaceTaskSaveQueues, target.fullPath, async () => {
    await requireSafeTaskWriteTarget(workspaceDir, target.fullPath);
    await writeTextAtomic(target.fullPath, code, {
      rootDir: path.resolve(workspaceDir, 'tasks'),
    });
    return {
      success: true,
      workspaceDir,
      tasksDir: path.join(workspaceDir, 'tasks'),
      relativePath: target.relativePath,
    };
  });
}

async function saveImportedTaskDefinition(workspaceDir, relativePath, definition, { parse = true } = {}) {
  if (!parse) {
    const result = await saveWorkspaceTaskSource(workspaceDir, relativePath, definition.code);
    clearWorkspaceTasksCache(workspaceDir);
    return result;
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
    5 * 60 * 1000,
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

function redactGaiaRunIdentifiers(value, runIds) {
  if (typeof value === 'string' && runIds.has(value)) {
    return `gaia-${crypto.createHash('sha256').update(value).digest('hex').slice(0, 32)}`;
  }
  if (Array.isArray(value)) {
    return value.map((item) => redactGaiaRunIdentifiers(item, runIds));
  }
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        redactGaiaRunIdentifiers(item, runIds),
      ]),
    );
  }
  return value;
}

function collectCurrentQueueWorkflowIds(value, workflowIds = new Set()) {
  if (Array.isArray(value)) {
    value.forEach((item) => collectCurrentQueueWorkflowIds(item, workflowIds));
    return workflowIds;
  }
  if (!value || typeof value !== 'object') {
    return workflowIds;
  }
  for (const [key, item] of Object.entries(value)) {
    if (key === 'stopped_workflow_ids') continue;
    if (key === 'workflow_id' && typeof item === 'string' && item) {
      workflowIds.add(item);
      continue;
    }
    collectCurrentQueueWorkflowIds(item, workflowIds);
  }
  return workflowIds;
}

async function publicClusterQueues(coreResponse, loadRun = loadCoreRun) {
  const queues = coreResponse?.queues;
  if (!queues || typeof queues !== 'object' || Array.isArray(queues)) {
    const error = new Error('Maze Core returned a malformed queue response');
    error.status = 502;
    throw error;
  }

  const result = { ...coreResponse, queues: { ...queues } };
  delete result.queues.stopped_workflow_ids;
  const privateRunIds = new Set();
  // ponytail: Active IDs only; caching privacy metadata creates a disclosure window.
  await Promise.all([...collectCurrentQueueWorkflowIds(result)].map(async (runId) => {
    try {
      const run = await loadRun(runId);
      if (run?.metadata?.benchmark === 'gaia') {
        privateRunIds.add(runId);
      }
    } catch {
      privateRunIds.add(runId);
    }
  }));
  return redactGaiaRunIdentifiers(result, privateRunIds);
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
}) {
  const submissionId = crypto.randomUUID();
  const definitions = await resolveWorkflowDefinitions(workflow, context.workspaceDir);
  let spec;
  try {
    spec = compileWorkflowToDagSpec(workflow, {
      workspaceDir: context.workspaceDir,
      workspaceId: context.workspaceId,
      workspaceManifestVersion: context.workspaceManifestVersion,
      artifactMode: true,
      tags: ['playground'],
      metadata: {
        source: 'maze_playground',
        submission_id: submissionId,
        playground_workflow_id: playgroundWorkflowId,
        ...(workflowPath ? { workflow_path: workflowPath } : {}),
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

function catalogTypeDir(type) {
  const normalized = String(type || '').trim().toLowerCase();
  if (!['workflows', 'tasks'].includes(normalized)) {
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
  } else {
    const target = resolveTaskDefinitionFile(workspaceDir, targetPath || path.basename(normalizedSourceId));
    resolvedTargetPath = target.fullPath;
    targetRelativePath = target.relativePath;
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
    const types = requestedType ? [requestedType] : ['workflows', 'tasks'];
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

// 1.2 保存工作目录任务
app.post('/api/workspace-tasks', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      relativePath = 'tasks/custom_task.py',
      code,
      parse = true,
    } = req.body || {};
    if (typeof parse !== 'boolean') {
      throw badRequestError('parse must be a boolean');
    }
    if (typeof code !== 'string') {
      throw badRequestError('code must be a string');
    }
    if (parse && !code.trim()) {
      throw badRequestError('Code cannot be empty');
    }
    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const workspaceDir = context.workspaceDir;

    console.log(`💾 保存工作区任务: ${workspaceDir}/${relativePath}`);

    const result = parse
      ? await withKeyedQueue(
          workspaceTaskSaveQueues,
          resolveTaskDefinitionFile(workspaceDir, relativePath).fullPath,
          () => callPython('save_workspace_task', {
            workspaceDir,
            relativePath,
            code,
            parse: true,
          }),
        )
      : await saveWorkspaceTaskSource(workspaceDir, relativePath, code);

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
    res.status(error.status || 500).json({ error: error.message });
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
  let result;
  try {
    if (action === 'start' || action === 'restart') {
      profile.headUrl = await currentClusterHeadUrl();
      profile.updatedAt = new Date().toISOString();
    }
    const command = remoteWorkerCommand(profile, action);
    result = await runSshCommand(profile, command, {
      password,
      timeoutMs: options.timeoutMs || 90000,
    });
  } catch (error) {
    profile.lastAction = {
      action,
      ok: false,
      at: new Date().toISOString(),
      stdoutTail: String(error.result?.stdout || '').split(/\r?\n/).slice(-20).join('\n'),
      stderrTail: String(error.result?.stderr || error.message || '').split(/\r?\n/).slice(-20).join('\n'),
    };
    await saveWorkerProfiles(
      context.workspaceDir,
      payload.profiles.map((item) => (item.id === profile.id ? profile : item)),
    );
    throw error;
  }
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
    const result = await callMazeCore('/cluster/queues');
    res.json(await publicClusterQueues(result));
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
      timeoutMs: 250 * 1000,
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

export {
  collectCurrentQueueWorkflowIds,
  publicClusterQueues,
  saveWorkspaceTaskSource,
  server,
  writeTextAtomic,
};

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

// 优雅关闭
process.on('SIGINT', () => {
  console.log('\n\n👋 正在关闭服务器...');

  server.close(() => {
    console.log('✅ 服务器已关闭');
    process.exit(0);
  });
});
