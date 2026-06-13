import express from 'express';
import cors from 'cors';
import { WebSocketServer } from 'ws';
import { spawn } from 'child_process';
import { v4 as uuidv4 } from 'uuid';
import path from 'path';
import { fileURLToPath } from 'url';
import http from 'http';
import fs from 'fs/promises';
import fsSync from 'fs';
import crypto from 'crypto';
import os from 'os';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const app = express();
const server = http.createServer(app);
const wss = new WebSocketServer({ noServer: true });

app.use(cors());
app.use(express.json({ limit: '50mb' }));

// 存储工作流状态
const workflows = new Map();
// 存储 WebSocket 连接
const wsConnections = new Map(); // workflowId -> Set<WebSocket>
const activeReactRunProcesses = new Map();
const activeAgentRuns = new Map();
const agentRunSseClients = new Map();
const localWorkspaceManifests = new Map();
const PROJECT_ROOT = path.resolve(__dirname, '../../../..');
const WORKSPACE_ROOT_DIR = path.resolve(process.env.MAZE_WORKSPACE_DIR || path.join(PROJECT_ROOT, 'workspace'));
const WORKSPACES_DIR = path.resolve(process.env.MAZE_WORKSPACES_DIR || path.join(WORKSPACE_ROOT_DIR, 'workspaces'));
const DEFAULT_WORKSPACE_ID = process.env.MAZE_DEFAULT_WORKSPACE_ID || 'default';
const DEFAULT_WORKSPACE_DIR = path.join(WORKSPACES_DIR, DEFAULT_WORKSPACE_ID);
const LEGACY_WORKSPACE_DIR = WORKSPACE_ROOT_DIR;
const SYSTEM_CATALOG_DIR = path.resolve(process.env.MAZE_SYSTEM_CATALOG_DIR || path.join(PROJECT_ROOT, 'system_catalog'));
const MAZE_CORE_URL = process.env.MAZE_CORE_URL || 'http://localhost:8000';
const TERMINAL_STATIC_RUN_STATUSES = new Set(['completed', 'failed', 'canceled', 'interrupted']);
const staticRunWriteQueues = new Map();
const recoveredStaticRunWorkspaces = new Set();

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
    return safeWorkspaceId(relative.split(path.sep)[0], DEFAULT_WORKSPACE_ID);
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

  if (!raw.includes('/') && !path.isAbsolute(raw)) {
    return path.join(WORKSPACES_DIR, safeWorkspaceId(raw, DEFAULT_WORKSPACE_ID));
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
    '@task(resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0})',
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
        'Use one @task(resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0}) decorator.',
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

async function writeJsonAtomic(filePath, payload) {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  const tmpPath = `${filePath}.${process.pid}.${Date.now()}.${Math.random().toString(16).slice(2)}.tmp`;
  await fs.writeFile(tmpPath, `${JSON.stringify(payload, null, 2)}\n`, 'utf-8');
  await fs.rename(tmpPath, filePath);
}

function mcpProfilesDir(workspaceDir) {
  return path.join(workspaceDir, 'mcp_profiles');
}

function mcpProfilePath(workspaceDir, name) {
  return path.join(mcpProfilesDir(workspaceDir), `${safeMcpProfileName(name)}.json`);
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

function agentRunsDir(workspaceDir) {
  return path.join(workspaceDir, 'agent_runs');
}

function agentSessionPath(workspaceDir, sessionId) {
  return path.join(agentSessionsDir(workspaceDir), `${safeAgentId(sessionId, 'session')}.json`);
}

function agentDraftPath(workspaceDir, draftId) {
  return path.join(agentDraftsDir(workspaceDir), `${safeAgentId(draftId, 'draft')}.json`);
}

function agentRunPath(workspaceDir, runId) {
  return path.join(agentRunsDir(workspaceDir), `${safeAgentId(runId, 'agent-run')}.json`);
}

function agentRunEventsPath(workspaceDir, runId) {
  return path.join(agentRunsDir(workspaceDir), `${safeAgentId(runId, 'agent-run')}.events.jsonl`);
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

function approximateAgentTokens(text) {
  return Math.ceil(String(text || '').length / 4);
}

function agentMessageApproxTokens(message) {
  return approximateAgentTokens(JSON.stringify(message?.parts || []));
}

function summarizeAgentMessageForCompaction(message) {
  const parts = (message.parts || []).map((part) => {
    if (part.type === 'text') return part.text;
    if (part.type === 'error') return `[error] ${part.message}`;
    if (part.type === 'tool_call') return `[tool_call ${part.name}] ${JSON.stringify(redactSecrets(part.input || {}))}`;
    if (part.type === 'tool_result') {
      const result = redactSecrets(part.result || {});
      return `[tool_result ${part.name}] ${JSON.stringify(result).slice(0, 1200)}`;
    }
    return JSON.stringify(redactSecrets(part));
  });
  return `${message.role} ${message.createdAt}:\n${parts.join('\n')}`;
}

function compactAgentMessages(messages, existingSummary = '', options = {}) {
  const maxApproxTokens = Number(options.maxApproxTokens || 12000);
  const keepRecentMessages = Number(options.keepRecentMessages || 16);
  const total = messages.reduce((sum, message) => sum + agentMessageApproxTokens(message), 0);
  if (total <= maxApproxTokens) {
    return { messages, summary: existingSummary || '', compactedCount: 0, recentApproxTokens: total };
  }

  const recent = messages.slice(-keepRecentMessages);
  const older = messages.slice(0, -keepRecentMessages);
  const recentApproxTokens = recent.reduce((sum, message) => sum + agentMessageApproxTokens(message), 0);
  const body = older
    .map((message) => summarizeAgentMessageForCompaction(message))
    .join('\n\n')
    .slice(0, maxApproxTokens * 2);
  const previous = String(existingSummary || '').trim();
  const nextSummary = [
    previous ? `Previous summary:\n${previous}` : '',
    `Compacted ${older.length} older messages. Recent approx tokens: ${recentApproxTokens}.`,
    body,
  ].filter(Boolean).join('\n\n');

  return {
    messages: recent,
    summary: nextSummary,
    compactedCount: older.length,
    recentApproxTokens,
  };
}

function createAgentMessage(sessionId, role, parts) {
  return {
    id: safeAgentId(`msg-${Date.now().toString(36)}-${crypto.randomBytes(3).toString('hex')}`, 'msg'),
    sessionId,
    role,
    createdAt: new Date().toISOString(),
    parts: redactSecrets(parts || []),
  };
}

function agentSessionSummary(session) {
  return {
    id: session.id,
    title: session.title,
    createdAt: session.createdAt,
    updatedAt: session.updatedAt,
    workspaceId: session.workspaceId,
    workspaceDir: session.workspaceDir,
    messageCount: Array.isArray(session.messages) ? session.messages.length : 0,
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

async function loadAgentDraftsForMessages(workspaceDir, messages = []) {
  const drafts = [];
  for (const draftId of collectAgentDraftIdsFromMessages(messages)) {
    try {
      drafts.push(agentDraftPublic(await loadAgentDraft(workspaceDir, draftId)));
    } catch (error) {
      if (error.code !== 'ENOENT') {
        console.error(`Failed to hydrate Workspace Agent draft ${draftId}:`, error);
      }
    }
  }
  return drafts;
}

async function createAgentSessionRecord(context, input = {}) {
  const now = new Date().toISOString();
  const session = {
    schema: 'maze_workspace_agent_session',
    schema_version: 1,
    id: safeAgentId(input.id, 'session'),
    title: String(input.title || input.message || 'Workspace Agent').slice(0, 80),
    workspaceId: context.workspaceId,
    workspaceDir: context.workspaceDir,
    createdAt: now,
    updatedAt: now,
    summary: '',
    compaction: {
      compactedCount: 0,
      recentApproxTokens: 0,
    },
    metadata: redactSecrets(input.metadata || {}),
    messages: [],
  };
  await writeJsonAtomic(agentSessionPath(context.workspaceDir, session.id), session);
  return session;
}

async function loadAgentSession(workspaceDir, sessionId) {
  const raw = await fs.readFile(agentSessionPath(workspaceDir, sessionId), 'utf-8');
  const session = JSON.parse(raw);
  session.messages = Array.isArray(session.messages) ? session.messages : [];
  return session;
}

async function saveAgentSession(workspaceDir, session) {
  session.updatedAt = new Date().toISOString();
  session.messages = Array.isArray(session.messages) ? session.messages.map(redactSecrets) : [];
  await writeJsonAtomic(agentSessionPath(workspaceDir, session.id), redactSecrets(session));
  return session;
}

async function updateAgentSessionRecord(workspaceDir, sessionId, updates = {}) {
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
}

async function deleteAgentSessionRecord(workspaceDir, sessionId) {
  await fs.unlink(agentSessionPath(workspaceDir, sessionId));
  return { id: sessionId };
}

async function buildAgentSessionExport(context, sessionId) {
  const session = await loadAgentSession(context.workspaceDir, sessionId);
  const drafts = await loadAgentDraftsForMessages(context.workspaceDir, session.messages);
  return redactSecrets({
    schema: 'maze_workspace_agent_session_export',
    schema_version: 1,
    exportedAt: new Date().toISOString(),
    workspaceId: context.workspaceId,
    workspaceManifestVersion: context.workspaceManifestVersion,
    session: agentSessionSummary(session),
    messages: session.messages,
    drafts,
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

async function appendAgentSessionMessage(workspaceDir, session, role, parts) {
  const message = createAgentMessage(session.id, role, parts);
  session.messages.push(message);
  await saveAgentSession(workspaceDir, session);
  return message;
}

function agentMessagesToLLM(messages) {
  return messages.map((message) => {
    const toolResult = (message.parts || []).find((part) => part.type === 'tool_result');
    const toolCalls = (message.parts || [])
      .filter((part) => part.type === 'tool_call')
      .map((part) => ({
        id: part.id,
        type: 'function',
        function: {
          name: part.name,
          arguments: JSON.stringify(part.input || {}),
        },
      }));
    const text = (message.parts || [])
      .filter((part) => part.type === 'text' || part.type === 'error')
      .map((part) => part.type === 'error' ? `[error] ${part.message}` : part.text)
      .join('\n');
    const llmMessage = {
      role: message.role === 'tool' ? 'tool' : message.role,
      content: toolResult ? JSON.stringify(redactSecrets(toolResult.result || {})) : text,
    };
    if (toolResult?.toolCallId) {
      llmMessage.tool_call_id = toolResult.toolCallId;
    }
    if (toolCalls.length) {
      llmMessage.tool_calls = toolCalls;
    }
    return llmMessage;
  });
}

async function callOpenAICompatibleToolChat({
  baseUrl,
  apiKey,
  model,
  messages,
  tools = [],
  temperature = 0.2,
  maxTokens = 2048,
  timeoutMs = 90000,
  signal,
}) {
  const apiKeys = getOpenAICompatibleApiKeys(apiKey);
  if (apiKeys.length === 0) {
    throw new Error('API key is required');
  }
  if (!model || !String(model).trim()) {
    throw new Error('Model is required');
  }

  const endpoint = normalizeOpenAIBaseUrl(baseUrl);
  const explicitApiKey = String(apiKey || '').trim();
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
        tools,
        tool_choice: tools.length ? 'auto' : undefined,
        temperature,
        max_tokens: maxTokens,
        stream: false,
      }),
    }, timeoutMs);

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

    return payload?.choices?.[0]?.message || {};
  }

  throw lastError || new Error('LLM request failed');
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
      resources: definition.resources || { cpu: 1, cpu_mem: 128, gpu: 0, gpu_mem: 0 },
    }))
    .filter((definition) => definition.relativePath && definition.code.trim());
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
          resources: node.data?.resources || node.resources || { cpu: 1, cpu_mem: 128, gpu: 0, gpu_mem: 0 },
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
  workflows.set(workflowId, workflow);

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
  const workflowRunId = uuidv4();
  const runSnapshot = createStaticRunSnapshot({
    runId: workflowRunId,
    workflow,
    workspaceDir: context.workspaceDir,
    workspaceContext: context,
  });
  await saveStaticRun(context.workspaceDir, runSnapshot);

  workflow.status = 'running';
  workflow.activeRunId = workflowRunId;
  workflows.set(workflow.id, workflow);

  await recordAndBroadcastStaticRun(workflow, context.workspaceDir, workflowRunId, {
    type: 'workflow_started',
    data: {
      workflow_id: workflow.id,
      workflow_run_id: workflowRunId,
      workspace_id: context.workspaceId,
      workspace_manifest_version: context.workspaceManifestVersion,
      source: 'workspace_agent',
      draft_id: draftId,
    },
    timestamp: new Date().toISOString(),
  });

  (async () => {
    try {
      await recordAndBroadcastStaticRun(workflow, context.workspaceDir, workflowRunId, {
        type: 'building',
        data: { message: 'Building workflow from Workspace Agent draft...' },
        timestamp: new Date().toISOString(),
      });
      const result = await callPython(
        'run_workflow',
        {
          workflowId: workflow.id,
          staticRunId: workflowRunId,
          workspaceId: context.workspaceId,
          workspaceDir: context.workspaceDir,
          workspaceManifestVersion: context.workspaceManifestVersion,
          nodes: workflow.nodes,
          edges: workflow.edges,
        },
        async (progress) => {
          await recordAndBroadcastStaticRun(workflow, context.workspaceDir, workflowRunId, {
            ...progress,
            timestamp: new Date().toISOString(),
          });
        },
      );

      if (!result.success) {
        workflow.status = 'failed';
        workflow.error = compactAgentDiagnosticText(result.error || 'Workflow failed', 2000);
        workflows.set(workflow.id, workflow);
        const failedDraft = await loadAgentDraft(context.workspaceDir, draftId).catch(() => null);
        if (failedDraft) {
          failedDraft.run = {
            ...(failedDraft.run || {}),
            runId: workflowRunId,
            workflowId: workflow.id,
            status: 'failed',
            error: workflow.error,
            finishedAt: new Date().toISOString(),
          };
          await writeAgentDraft(context.workspaceDir, failedDraft);
        }
        await recordAndBroadcastStaticRun(workflow, context.workspaceDir, workflowRunId, {
          type: 'workflow_failed',
          data: {
            error: workflow.error,
            traceback: result.traceback,
          },
          timestamp: new Date().toISOString(),
        });
        return;
      }

      const latestRunAfterPython = await loadStaticRun(context.workspaceDir, workflowRunId).catch(() => null);
      if (latestRunAfterPython?.status === 'failed') {
        workflow.status = 'failed';
        workflow.error = compactAgentDiagnosticText(latestRunAfterPython.error || 'Workflow task failed', 2000);
        workflow.lastRunId = workflowRunId;
        workflow.mazeRunId = result.mazeRunId;
        workflows.set(workflow.id, workflow);
        const failedDraft = await loadAgentDraft(context.workspaceDir, draftId).catch(() => null);
        if (failedDraft) {
          failedDraft.run = {
            ...(failedDraft.run || {}),
            runId: workflowRunId,
            workflowId: workflow.id,
            status: 'failed',
            error: workflow.error,
            finishedAt: new Date().toISOString(),
            mazeRunId: result.mazeRunId || null,
          };
          await writeAgentDraft(context.workspaceDir, failedDraft);
        }
        await recordAndBroadcastStaticRun(workflow, context.workspaceDir, workflowRunId, {
          type: 'workflow_failed',
          data: {
            error: workflow.error,
            traceback: result.traceback,
          },
          timestamp: new Date().toISOString(),
        });
        return;
      }

      workflow.status = 'completed';
      workflow.results = result.results;
      workflow.lastRunId = workflowRunId;
      workflow.mazeRunId = result.mazeRunId;
      workflows.set(workflow.id, workflow);
      const completedDraft = await loadAgentDraft(context.workspaceDir, draftId).catch(() => null);
      if (completedDraft) {
        completedDraft.run = {
          ...(completedDraft.run || {}),
          runId: workflowRunId,
          workflowId: workflow.id,
          status: 'completed',
          finishedAt: new Date().toISOString(),
          mazeRunId: result.mazeRunId || null,
        };
        await writeAgentDraft(context.workspaceDir, completedDraft);
      }

      if (result.mazeRunId) {
        await recordAndBroadcastStaticRun(workflow, context.workspaceDir, workflowRunId, {
          type: 'maze_run_created',
          data: { maze_run_id: result.mazeRunId },
          timestamp: new Date().toISOString(),
        });
      }
      await recordAndBroadcastStaticRun(workflow, context.workspaceDir, workflowRunId, {
        type: 'workflow_completed',
        data: { results: result.results },
        timestamp: new Date().toISOString(),
      });
    } catch (error) {
      workflow.status = 'failed';
      workflow.error = error.message;
      workflows.set(workflow.id, workflow);
      const failedDraft = await loadAgentDraft(context.workspaceDir, draftId).catch(() => null);
      if (failedDraft) {
        failedDraft.run = {
          ...(failedDraft.run || {}),
          runId: workflowRunId,
          workflowId: workflow.id,
          status: 'failed',
          error: error.message,
          finishedAt: new Date().toISOString(),
        };
        await writeAgentDraft(context.workspaceDir, failedDraft);
      }
      await recordAndBroadcastStaticRun(workflow, context.workspaceDir, workflowRunId, {
        type: 'workflow_failed',
        data: { error: error.message },
        timestamp: new Date().toISOString(),
      });
    }
  })();

  const draft = await loadAgentDraft(context.workspaceDir, draftId);
  draft.run = {
    runId: workflowRunId,
    workflowId: workflow.id,
    startedAt: new Date().toISOString(),
    status: 'running',
  };
  await writeAgentDraft(context.workspaceDir, draft);

  return {
    draft: agentDraftPublic(draft),
    workflow,
    run: runSnapshot,
    runId: workflowRunId,
    workflowId: workflow.id,
    workspaceId: context.workspaceId,
    workspaceDir: context.workspaceDir,
  };
}

async function appendAgentRunEvent(context, run, event) {
  const nextSeq = Number(run.lastSeq || 0) + 1;
  const clean = redactSecrets({
    ...event,
    seq: nextSeq,
    timestamp: event.timestamp || new Date().toISOString(),
  });
  run.lastSeq = nextSeq;
  run.updatedAt = clean.timestamp;
  run.events = Number(run.events || 0) + 1;
  await fs.mkdir(agentRunsDir(context.workspaceDir), { recursive: true });
  await fs.appendFile(agentRunEventsPath(context.workspaceDir, run.id), `${JSON.stringify(clean)}\n`, 'utf-8');
  await writeJsonAtomic(agentRunPath(context.workspaceDir, run.id), redactSecrets(run));
  broadcastAgentRunEvent(context.workspaceDir, run.id, clean);
  return clean;
}

function agentRunSseKey(workspaceDir, runId) {
  return `${workspaceDir}::${safeAgentId(runId, 'agent-run')}`;
}

function sendAgentSse(res, event) {
  res.write(`id: ${event.seq || Date.now()}\n`);
  res.write(`data: ${JSON.stringify(event)}\n\n`);
}

function broadcastAgentRunEvent(workspaceDir, runId, event) {
  const clients = agentRunSseClients.get(agentRunSseKey(workspaceDir, runId));
  if (!clients) return;
  for (const [res, client] of clients.entries()) {
    try {
      if (Number(event.seq || 0) <= Number(client.lastSeq || 0)) {
        continue;
      }
      sendAgentSse(res, event);
      client.lastSeq = Number(event.seq || client.lastSeq || 0);
    } catch {
      clients.delete(res);
    }
  }
  if (clients.size === 0) {
    agentRunSseClients.delete(agentRunSseKey(workspaceDir, runId));
  }
}

function isAgentRunTerminal(status) {
  return ['succeeded', 'failed', 'canceled', 'interrupted'].includes(String(status || ''));
}

function agentRunKey(workspaceDir, runId) {
  return `${workspaceDir}::${safeAgentId(runId, 'agent-run')}`;
}

function createAgentCanceledError(reason = 'Workspace Agent run was canceled') {
  const error = new Error(reason);
  error.code = 'AGENT_RUN_CANCELED';
  error.status = 499;
  return error;
}

async function latestAgentRun(workspaceDir, runId) {
  return readJsonFile(agentRunPath(workspaceDir, runId), null);
}

async function assertAgentRunNotCanceled(context, run, signal) {
  if (signal?.aborted) {
    throw createAgentCanceledError();
  }
  const latest = await latestAgentRun(context.workspaceDir, run.id);
  if (latest?.status === 'canceled') {
    throw createAgentCanceledError(latest.cancelReason || 'Workspace Agent run was canceled');
  }
}

async function cancelAgentRun(context, runId, reason = 'Canceled by user') {
  const run = await latestAgentRun(context.workspaceDir, runId);
  if (!run) {
    const error = new Error('Agent run not found');
    error.status = 404;
    throw error;
  }

  const key = agentRunKey(context.workspaceDir, runId);
  activeAgentRuns.get(key)?.controller?.abort();

  if (!isAgentRunTerminal(run.status)) {
    run.status = 'canceled';
    run.cancelReason = String(reason || 'Canceled by user').slice(0, 500);
    run.canceledAt = new Date().toISOString();
    run.updatedAt = run.canceledAt;
    await writeJsonAtomic(agentRunPath(context.workspaceDir, runId), redactSecrets(run));
    await appendAgentRunEvent(context, run, {
      type: 'canceled',
      reason: run.cancelReason,
      sessionId: run.sessionId,
      runId: run.id,
    });
    await appendAgentRunEvent(context, run, {
      type: 'finish',
      reason: 'canceled',
      sessionId: run.sessionId,
      runId: run.id,
    });
    if (run.sessionId) {
      const session = await loadAgentSession(context.workspaceDir, run.sessionId).catch(() => null);
      if (session) {
        await appendAgentSessionMessage(context.workspaceDir, session, 'assistant', [{
          type: 'text',
          text: `Canceled: ${run.cancelReason}`,
        }]);
      }
    }
  }

  return latestAgentRun(context.workspaceDir, runId);
}

async function markInterruptedAgentRunsOnStartup() {
  let interrupted = 0;
  for (const workspaceDir of await listServiceWorkspaceDirs()) {
    const entries = await fs.readdir(agentRunsDir(workspaceDir), { withFileTypes: true }).catch((error) => {
      if (error.code === 'ENOENT') return [];
      throw error;
    });
    for (const entry of entries) {
      if (!entry.isFile() || !entry.name.endsWith('.json') || entry.name.endsWith('.events.json')) continue;
      const runId = entry.name.replace(/\.json$/, '');
      const run = await latestAgentRun(workspaceDir, runId);
      if (!run || isAgentRunTerminal(run.status)) continue;
      run.status = 'interrupted';
      run.error = run.error || 'Workspace Agent run was interrupted by a backend restart';
      run.interruptedAt = new Date().toISOString();
      run.updatedAt = run.interruptedAt;
      await writeJsonAtomic(agentRunPath(workspaceDir, runId), redactSecrets(run));
      await appendAgentRunEvent({ workspaceDir }, run, {
        type: 'interrupted',
        reason: run.error,
        sessionId: run.sessionId,
        runId: run.id,
      });
      await appendAgentRunEvent({ workspaceDir }, run, {
        type: 'finish',
        reason: 'interrupted',
        sessionId: run.sessionId,
        runId: run.id,
      });
      interrupted += 1;
    }
  }
  return interrupted;
}

async function loadAgentRunEvents(workspaceDir, runId, after = null) {
  const raw = await fs.readFile(agentRunEventsPath(workspaceDir, runId), 'utf-8').catch((error) => {
    if (error.code === 'ENOENT') return '';
    throw error;
  });
  return raw
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => JSON.parse(line))
    .filter((event) => after === null || Number(event.seq || 0) > Number(after));
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
        name: 'save_workflow_draft',
        description: 'Save a workflow draft after explicit user confirmation.',
        parameters: {
          type: 'object',
          required: ['draftId', 'confirmed'],
          properties: {
            draftId: { type: 'string' },
            confirmed: { type: 'boolean' },
            relativePath: { type: 'string' },
          },
        },
      },
    },
    {
      type: 'function',
      function: {
        name: 'run_workflow_draft',
        description: 'Run a workflow draft after explicit user confirmation.',
        parameters: {
          type: 'object',
          required: ['draftId', 'confirmed'],
          properties: {
            draftId: { type: 'string' },
            confirmed: { type: 'boolean' },
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

async function promoteArtifactIntoWorkspace(context, input = {}) {
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
    const response = await fetch(`${MAZE_CORE_URL}/artifacts/sha256/${encodeURIComponent(sourceSha)}`);
    if (!response.ok) {
      const error = new Error(`Failed to download artifact: HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    const data = Buffer.from(await response.arrayBuffer());
    await fs.writeFile(fullPath, data);
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
        return { ok: true, run: await buildDynamicRunDiagnostic(detail.run || { run_id: runId }, options) };
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

  if (name === 'save_workflow_draft') {
    const saved = await saveAgentWorkflowDraft(context, input.draftId, {
      confirmed: input.confirmed === true,
      relativePath: input.relativePath,
    });
    return { ok: true, ...saved };
  }

  if (name === 'run_workflow_draft') {
    const started = await runAgentWorkflowDraft(context, input.draftId, {
      confirmed: input.confirmed === true,
    });
    return { ok: true, ...started };
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
    'If you say you will inspect, draft, validate, save, run, fix, promote, or update something, call the corresponding tool in the same assistant turn.',
    'Only answer with text alone when you are explaining, asking for genuinely missing information, or waiting for explicit confirmation for save/run.',
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
    'Do not save or run a draft unless the user has explicitly confirmed that action.',
    'When creating workspace task definitions, generate safe Python Maze tasks using `from maze import task`, one @task function per file, no secrets, no absolute paths, no subprocess, no shell, no package installation, and no network calls.',
    'Use only `@task` or `@task(resources={...})`; never use `@task(inputs=...)`, `@task(outputs=...)`, or inputs/outputs decorator arguments. Maze infers inputs from function parameters and outputs from returned dict keys.',
    'For workspace workflow nodes, use category="workspace", nodeType="task", taskPath, functionName, inputs, outputs, resources, configured=true.',
    'Use user input values by setting each input item as {name, dataType, source:"user", value:"..."} when appropriate.',
    'For downstream node inputs, use {name, dataType, source:"task", taskSource:{taskId:"upstream-node-id", outputKey:"upstream_output_key"}}.',
    'If a draft is created, explain what it contains and tell the user they can Preview, Save, or Run it from the draft card.',
    `Workspace: ${context.workspaceId} (${context.workspaceDir})`,
  ].join('\n');
}

function parseToolArguments(raw) {
  if (!raw) return {};
  if (typeof raw === 'object') return raw;
  try {
    return JSON.parse(raw);
  } catch {
    return {};
  }
}

async function runWorkspaceAgent(context, input = {}) {
  const message = String(input.message || '').trim();
  if (!message) {
    const error = new Error('message is required');
    error.status = 400;
    throw error;
  }

  let session = input.sessionId
    ? await loadAgentSession(context.workspaceDir, input.sessionId).catch(() => null)
    : null;
  if (!session) {
    session = await createAgentSessionRecord(context, {
      title: input.title || message.slice(0, 60),
      message,
    });
  }

  const run = {
    schema: 'maze_workspace_agent_run',
    schema_version: 1,
    id: safeAgentId(input.runId, 'agent-run'),
    sessionId: session.id,
    workspaceId: context.workspaceId,
    workspaceDir: context.workspaceDir,
    status: 'running',
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    lastSeq: 0,
    events: 0,
    finalMessageId: null,
    error: null,
  };
  await writeJsonAtomic(agentRunPath(context.workspaceDir, run.id), run);

  const abortController = input.abortController || new AbortController();
  const emittedEvents = [];
  const emit = async (event) => {
    const saved = await appendAgentRunEvent(context, run, event);
    emittedEvents.push(saved);
    return saved;
  };

  await emit({ type: 'session_started', sessionId: session.id, runId: run.id });
  await appendAgentSessionMessage(context.workspaceDir, session, 'user', [{ type: 'text', text: message }]);

  const compaction = compactAgentMessages(session.messages, session.summary, input.compaction || {});
  if (compaction.compactedCount > 0) {
    session.summary = compaction.summary;
    session.compaction = {
      compactedCount: Number(session.compaction?.compactedCount || 0) + compaction.compactedCount,
      recentApproxTokens: compaction.recentApproxTokens,
      compactedAt: new Date().toISOString(),
    };
    session.messages = compaction.messages;
    await saveAgentSession(context.workspaceDir, session);
    await emit({
      type: 'context_compacted',
      compactedCount: compaction.compactedCount,
      recentApproxTokens: compaction.recentApproxTokens,
    });
  }

  const llm = input.llm || {};
  const maxSteps = Math.min(Math.max(Number(input.maxSteps || 8), 1), 16);
  const llmTimeoutMs = Math.min(Math.max(Number(input.timeoutMs || 180000), 30000), 600000);
  const tools = agentToolDefinitions();
  let finalText = '';

  try {
    for (let step = 0; step < maxSteps; step += 1) {
      await assertAgentRunNotCanceled(context, run, abortController.signal);
      const llmMessages = [
        { role: 'system', content: workspaceAgentSystemPrompt(context) },
        ...(session.summary ? [{ role: 'system', content: `Conversation summary:\n${session.summary}` }] : []),
        ...(input.currentWorkflow ? [{
          role: 'system',
          content: `Current open workflow snapshot:\n${JSON.stringify(redactSecrets(input.currentWorkflow)).slice(0, 12000)}`,
        }] : []),
        ...agentMessagesToLLM(session.messages),
      ];
      await emit({
        type: 'context_usage',
        source: 'estimated',
        inputTokens: llmMessages.reduce((sum, item) => sum + approximateAgentTokens(`${item.role}\n${item.content || ''}`), 0),
        step,
      });
      await assertAgentRunNotCanceled(context, run, abortController.signal);

      await emit({
        type: 'llm_waiting',
        timeoutMs: llmTimeoutMs,
        step,
      });

      const assistant = await callOpenAICompatibleToolChat({
        baseUrl: llm.baseUrl,
        apiKey: llm.apiKey,
        model: llm.model,
        messages: llmMessages,
        tools,
        maxTokens: Number(input.maxTokens || 2048),
        temperature: Number(input.temperature ?? 0.2),
        timeoutMs: llmTimeoutMs,
        signal: abortController.signal,
      });
      await assertAgentRunNotCanceled(context, run, abortController.signal);

      const assistantText = String(assistant.content || '');
      const toolCalls = Array.isArray(assistant.tool_calls) ? assistant.tool_calls : [];
      const assistantParts = [
        ...(assistantText ? [{ type: 'text', text: assistantText }] : []),
        ...toolCalls.map((call, index) => ({
          type: 'tool_call',
          id: call.id || `tool-${step}-${index}`,
          name: call.function?.name || call.name,
          input: parseToolArguments(call.function?.arguments || call.arguments),
        })),
      ];

      if (toolCalls.length && assistantParts.length > 0) {
        await appendAgentSessionMessage(context.workspaceDir, session, 'assistant', assistantParts);
      }

      if (!toolCalls.length) {
        finalText = assistantText || 'Done.';
        break;
      }

      for (const call of toolCalls) {
        await assertAgentRunNotCanceled(context, run, abortController.signal);
        const toolName = call.function?.name || call.name;
        const toolInput = parseToolArguments(call.function?.arguments || call.arguments);
        const toolCallId = call.id || `tool-${step}-${toolName}`;
        await emit({
          type: 'tool_call',
          toolCallId,
          name: toolName,
          input: redactSecrets(toolInput),
        });
        let result;
        try {
          result = await executeAgentTool(context, toolName, toolInput, {
            currentWorkflow: input.currentWorkflow || null,
            sessionId: session.id,
            runId: run.id,
          });
        } catch (error) {
          result = {
            ok: false,
            error: error.message,
            code: error.code || undefined,
            status: error.status || undefined,
          };
        }
        await appendAgentSessionMessage(context.workspaceDir, session, 'tool', [{
          type: 'tool_result',
          toolCallId,
          name: toolName,
          result,
        }]);
        await emit({
          type: 'tool_result',
          toolCallId,
          name: toolName,
          ok: result?.ok !== false,
          result: redactSecrets(result),
        });
        await assertAgentRunNotCanceled(context, run, abortController.signal);
      }
    }

    await assertAgentRunNotCanceled(context, run, abortController.signal);
    if (!finalText) {
      finalText = `Stopped after maxSteps=${maxSteps}.`;
    }
    const finalMessage = await appendAgentSessionMessage(context.workspaceDir, session, 'assistant', [{ type: 'text', text: finalText }]);
    run.status = 'succeeded';
    run.finalMessageId = finalMessage.id;
    run.updatedAt = new Date().toISOString();
    await writeJsonAtomic(agentRunPath(context.workspaceDir, run.id), redactSecrets(run));
    await emit({ type: 'finish', reason: 'stop', sessionId: session.id, messageId: finalMessage.id });
  } catch (error) {
    const errorMessage = error.message || String(error);
    const canceledByUser = error.code === 'AGENT_RUN_CANCELED' || error.code === 'LLM_ABORTED' || abortController.signal.aborted;
    if (canceledByUser) {
      const latest = await latestAgentRun(context.workspaceDir, run.id);
      if (latest?.status !== 'canceled') {
        run.status = 'canceled';
        run.cancelReason = errorMessage;
        run.canceledAt = new Date().toISOString();
        run.updatedAt = run.canceledAt;
        await writeJsonAtomic(agentRunPath(context.workspaceDir, run.id), redactSecrets(run));
        await emit({ type: 'canceled', reason: errorMessage, sessionId: session.id, runId: run.id });
        await emit({ type: 'finish', reason: 'canceled', sessionId: session.id, runId: run.id });
        await appendAgentSessionMessage(context.workspaceDir, session, 'assistant', [{ type: 'text', text: `Canceled: ${errorMessage}` }]);
      } else {
        Object.assign(run, latest);
      }
      finalText = errorMessage;
      const savedSession = await loadAgentSession(context.workspaceDir, session.id);
      const events = await loadAgentRunEvents(context.workspaceDir, run.id);
      return {
        success: false,
        run: redactSecrets(latest || run),
        session: agentSessionSummary(savedSession),
        messages: savedSession.messages,
        events,
        finalText,
      };
    }
    await appendAgentSessionMessage(context.workspaceDir, session, 'assistant', [{ type: 'error', message: errorMessage }]);
    run.status = 'failed';
    run.error = errorMessage;
    run.updatedAt = new Date().toISOString();
    await writeJsonAtomic(agentRunPath(context.workspaceDir, run.id), redactSecrets(run));
    await emit({ type: 'error', message: errorMessage, code: error.code || undefined, status: error.status || undefined });
    await emit({ type: 'finish', reason: 'error', sessionId: session.id });
  }

  const savedSession = await loadAgentSession(context.workspaceDir, session.id);
  const events = await loadAgentRunEvents(context.workspaceDir, run.id);
  return {
    success: run.status === 'succeeded',
    run: redactSecrets(run),
    session: agentSessionSummary(savedSession),
    messages: savedSession.messages,
    events,
    finalText,
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

  const queued = current.finally(() => {
    if (staticRunWriteQueues.get(key) === queued) {
      staticRunWriteQueues.delete(key);
    }
  });

  staticRunWriteQueues.set(key, queued);

  return current;
}

async function saveStaticRun(workspaceDir, snapshot) {
  await writeJsonAtomic(staticRunPath(workspaceDir, snapshot.run_id, { write: true }), {
    ...snapshot,
    workspace_id: snapshot.workspace_id || workspaceIdFromDir(workspaceDir),
  });
}

async function loadStaticRun(workspaceDir, runId) {
  const raw = await fs.readFile(staticRunPath(workspaceDir, runId), 'utf-8');
  return JSON.parse(raw);
}

function staticRunSummary(snapshot) {
  return {
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
}

async function appendStaticRunEvent(workspaceDir, runId, event) {
  const runDir = staticRunDir(workspaceDir, runId);
  await fs.mkdir(runDir, { recursive: true });
  await fs.appendFile(staticRunEventsPath(workspaceDir, runId), `${JSON.stringify(event)}\n`, 'utf-8');
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

async function appendAndApplyStaticRunEvent(workspaceDir, runId, incomingEvent) {
  return withStaticRunWriteQueue(workspaceDir, runId, async () => {
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
  });
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
  } else if (event.type === 'maze_run_created') {
    snapshot.maze_run_id = data.maze_run_id || snapshot.maze_run_id;
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
      const snapshot = JSON.parse(raw);
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
  if (!dryRun) {
    for (const item of candidates) {
      await fs.unlink(artifactBlobPath(item.sha256)).catch((error) => {
        if (error.code !== 'ENOENT') throw error;
      });
      deletedSha256.push(item.sha256);
    }
  }

  return {
    dry_run: dryRun,
    older_than_days: olderThanDays,
    scope: 'global-orphan-cas',
    referenced_count: referenced.size,
    matched_count: candidates.length,
    deleted_count: deletedSha256.length,
    artifacts: candidates,
    deleted_sha256: deletedSha256,
  };
}

async function recoverInterruptedStaticRuns(workspaceDir) {
  if (recoveredStaticRunWorkspaces.has(workspaceDir)) {
    return;
  }
  recoveredStaticRunWorkspaces.add(workspaceDir);

  const runs = await listStaticRunFilesForWorkspace(workspaceDir);
  const staleRuns = runs.filter((run) => run.status === 'running');
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
    }
  }
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
      return candidate;
    }

    const existingCode = await fs.readFile(fullPath, 'utf-8');
    if (hashTaskCode(existingCode) === hashTaskCode(code)) {
      return candidate;
    }

    suffix += 1;
  }
}

async function saveImportedTaskDefinition(workspaceDir, relativePath, definition) {
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

async function importTaskDefinitions(workspaceDir, taskDefinitions = [], workflowName = 'imported-workflow') {
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
        targetRelativePath = await nextImportedTaskPath(workspaceDir, workflowName, relativePath, definition.code);
        await saveImportedTaskDefinition(workspaceDir, targetRelativePath, definition);
        imported.push({ relativePath: targetRelativePath, sourceRelativePath: relativePath });
        remapped.push({ from: relativePath, to: targetRelativePath, reason: 'conflict' });
      }
    } else {
      await saveImportedTaskDefinition(workspaceDir, targetRelativePath, definition);
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

async function callMazeCore(pathname, options = {}) {
  const url = `${MAZE_CORE_URL}${pathname}`;
  const response = await fetch(url, {
    method: options.method || 'GET',
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
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
}

// ========== Python 桥接函数 ==========

function callPython(action, params = {}, onProgress = null, extraEnv = {}) {
  return new Promise((resolve, reject) => {
    const bridgePath = path.join(__dirname, '../maze_bridge.py');
    const progressPromises = [];

    // 设置 Python 环境变量，强制使用 UTF-8 编码
    const python = spawn(PYTHON_BIN, [bridgePath, action, JSON.stringify(params)], {
      env: {
        ...process.env,
        ...extraEnv,
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
    let stderrLineBuffer = '';

    // 设置 stdout 编码为 utf8
    python.stdout.setEncoding('utf8');
    python.stdout.on('data', (data) => {
      output += data;
    });

    // 设置 stderr 编码为 utf8
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
            if (onProgress) {
              progressPromises.push(Promise.resolve(onProgress(progress)));
            }
          } catch (e) {
            console.error('解析进度消息失败:', raw);
          }
        } else if (line.trim()) {
          console.error('Python stderr:', line);
        }
      });
    });

    python.on('close', async (code) => {
      if (code === 0) {
        try {
          if (progressPromises.length > 0) {
            await Promise.allSettled(progressPromises);
          }
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

function startReactWorkflowProcess(params = {}, extraEnv = {}) {
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
      activeReactRunProcesses.set(runId, python);
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
      if (runId) {
        activeReactRunProcesses.delete(runId);
      }

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
      if (runId) {
        activeReactRunProcesses.delete(runId);
      }
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

// ========== WebSocket 辅助函数 ==========

function broadcastToWorkflow(workflowId, message) {
  const connections = wsConnections.get(workflowId);
  console.log(`[WebSocket] 尝试广播到工作流 ${workflowId}, 连接数: ${connections ? connections.size : 0}`);

  if (connections) {
    const data = JSON.stringify(message);
    let sentCount = 0;
    connections.forEach((ws) => {
      console.log(`  WebSocket 状态: ${ws.readyState} (1=OPEN)`);
      if (ws.readyState === 1) { // OPEN
        ws.send(data);
        sentCount++;
      }
    });
    console.log(`  ✅ 已发送给 ${sentCount} 个客户端`);
  } else {
    console.log(`  ⚠️  没有找到工作流的 WebSocket 连接`);
  }
}

async function recordAndBroadcastStaticRun(workflow, workspaceDir, runId, event) {
  const { snapshot, event: storedEvent } = await appendAndApplyStaticRunEvent(workspaceDir, runId, event);
  broadcastToWorkflow(workflow.id, {
    type: 'run_update',
    workflowId: workflow.id,
    runId,
    run: snapshot,
    event: storedEvent,
    timestamp: storedEvent.timestamp,
  });
  return { snapshot, event: storedEvent };
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

// 1. 获取内置任务列表
app.get('/api/builtin-tasks', async (req, res) => {
  try {
    console.log('📋 获取内置任务列表...');
    const result = await callPython('get_builtin_tasks');

    if (result.error) {
      console.error('❌ 获取内置任务失败:', result.error);
      return res.status(500).json({ error: result.error });
    }

    console.log(`✅ 成功获取 ${result.tasks.length} 个内置任务`);
    res.json(result.tasks || []);
  } catch (error) {
    console.error('❌ 获取内置任务失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// 1.1 获取工作目录任务列表
app.get('/api/workspace-tasks', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const workspaceDir = context.workspaceDir;
    console.log(`📁 扫描工作目录任务: ${workspaceDir}`);

    const result = await callPython('get_workspace_tasks', { workspaceDir });

    if (result.error) {
      console.error('❌ 扫描工作目录失败:', result.error);
      return res.status(400).json({ error: result.error, traceback: result.traceback });
    }

    console.log(`✅ 成功获取 ${result.tasks.length} 个工作区任务`);
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
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
    } = req.body || {};
    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    res.json(await promoteArtifactIntoWorkspace(context, req.body || {}));
  } catch (error) {
    console.error('❌ Promote artifact 失败:', error);
    res.status(error.status || 500).json({ error: error.message });
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
    res.status(500).json({ error: error.message });
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
    res.status(500).json({ error: error.message });
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
      runs: result.runs || [],
    });
  } catch (error) {
    console.error('Failed to get runs:', error);
    res.status(error.status || 500).json({ error: error.message, payload: error.payload });
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
    res.json(result);
  } catch (error) {
    console.error('Failed to get cluster queues:', error);
    res.status(error.status || 500).json({ error: error.message || 'Failed to get cluster queues' });
  }
});

app.get('/api/runs/:runId', async (req, res) => {
  try {
    const result = await callMazeCore(`/runs/${encodeURIComponent(req.params.runId)}`);
    res.json({
      success: true,
      run: result.run,
    });
  } catch (error) {
    console.error('Failed to get run:', error);
    res.status(error.status || 500).json({ error: error.message, payload: error.payload });
  }
});

app.get('/api/runs/:runId/tasks', async (req, res) => {
  try {
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
  try {
    const response = await fetch(`${MAZE_CORE_URL}/artifacts/sha256/${encodeURIComponent(req.params.sha256)}`);
    if (!response.ok) {
      const message = await response.text();
      return res.status(response.status).send(message || `Maze core request failed: ${response.status}`);
    }
    const contentType = response.headers.get('content-type') || 'application/octet-stream';
    res.setHeader('Content-Type', contentType);
    res.setHeader('Content-Disposition', `attachment; filename="${req.params.sha256}"`);
    const buffer = Buffer.from(await response.arrayBuffer());
    res.send(buffer);
  } catch (error) {
    console.error('Failed to download artifact:', error);
    res.status(error.status || 500).json({ error: error.message || 'Failed to download artifact' });
  }
});

app.post('/api/runs/:runId/cancel', async (req, res) => {
  try {
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
      messages: session.messages,
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
    const drafts = await loadAgentDraftsForMessages(context.workspaceDir, session.messages);
    res.json({
      success: true,
      ...workspaceResponseFields(context),
      session: agentSessionSummary(session),
      messages: session.messages,
      drafts,
    });
  } catch (error) {
    console.error('Failed to read Workspace Agent messages:', error);
    res.status(error.code === 'ENOENT' ? 404 : (error.status || 500)).json({ success: false, error: error.message });
  }
});

app.post('/api/agent/runs', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.body || {});
    if (req.body?.async === true) {
      const session = req.body.sessionId
        ? await loadAgentSession(context.workspaceDir, req.body.sessionId).catch(() => null)
        : await createAgentSessionRecord(context, {
          title: req.body.title || String(req.body.message || 'Workspace Agent').slice(0, 60),
          message: req.body.message,
        });
      const runId = safeAgentId(req.body.runId, 'agent-run');
      const run = {
        schema: 'maze_workspace_agent_run',
        schema_version: 1,
        id: runId,
        sessionId: session?.id,
        workspaceId: context.workspaceId,
        workspaceDir: context.workspaceDir,
        status: 'queued',
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
        lastSeq: 0,
        events: 0,
        finalMessageId: null,
        error: null,
      };
      await writeJsonAtomic(agentRunPath(context.workspaceDir, runId), run);
      const payload = {
        ...(req.body || {}),
        sessionId: session?.id,
        runId,
      };
      const abortController = new AbortController();
      payload.abortController = abortController;
      const promise = runWorkspaceAgent(context, payload)
        .catch((error) => {
          console.error(`Workspace Agent async run failed: ${runId}`, error);
        })
        .finally(() => {
          activeAgentRuns.delete(agentRunKey(context.workspaceDir, runId));
        });
      activeAgentRuns.set(agentRunKey(context.workspaceDir, runId), { promise, controller: abortController });
      return res.status(202).json({
        success: true,
        ...workspaceResponseFields(context),
        run,
        session: session ? agentSessionSummary(session) : null,
        messages: session?.messages || [],
        events: [],
      });
    }
    const result = await runWorkspaceAgent(context, req.body || {});
    res.status(result.success ? 200 : 500).json({
      ...result,
      ...workspaceResponseFields(context),
    });
  } catch (error) {
    console.error('Failed to run Workspace Agent:', error);
    res.status(error.status || 500).json({ success: false, error: error.message });
  }
});

app.get('/api/agent/runs/:id/events', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const run = await readJsonFile(agentRunPath(context.workspaceDir, req.params.id), null);
    if (!run) {
      return res.status(404).json({ success: false, error: 'Agent run not found' });
    }
    const after = req.query.after === undefined ? null : Number(req.query.after);
    const events = await loadAgentRunEvents(context.workspaceDir, req.params.id, Number.isFinite(after) ? after : null);
    res.json({
      success: true,
      ...workspaceResponseFields(context),
      run: redactSecrets(run),
      events,
    });
  } catch (error) {
    console.error('Failed to read Workspace Agent events:', error);
    res.status(error.status || 500).json({ success: false, error: error.message });
  }
});

app.post('/api/agent/runs/:id/cancel', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.body || {});
    const run = await cancelAgentRun(context, req.params.id, req.body?.reason || 'Canceled by user');
    res.json({
      success: true,
      ...workspaceResponseFields(context),
      run: redactSecrets(run),
    });
  } catch (error) {
    console.error('Failed to cancel Workspace Agent run:', error);
    res.status(error.status || 500).json({ success: false, error: error.message });
  }
});

app.get('/api/agent/runs/:id/stream', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const run = await readJsonFile(agentRunPath(context.workspaceDir, req.params.id), null);
    if (!run) {
      return res.status(404).json({ success: false, error: 'Agent run not found' });
    }

    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
      'X-Accel-Buffering': 'no',
    });
    res.write('\n');

    const after = req.query.after === undefined ? null : Number(req.query.after);
    const existingEvents = await loadAgentRunEvents(context.workspaceDir, req.params.id, Number.isFinite(after) ? after : null);
    existingEvents.forEach((event) => sendAgentSse(res, event));
    const lastSeq = existingEvents.reduce((max, event) => Math.max(max, Number(event.seq || 0)), Number.isFinite(after) ? after : 0);

    if (run.status === 'succeeded' || run.status === 'failed') {
      res.end();
      return;
    }

    const key = agentRunSseKey(context.workspaceDir, req.params.id);
    if (!agentRunSseClients.has(key)) {
      agentRunSseClients.set(key, new Map());
    }
    agentRunSseClients.get(key).set(res, { lastSeq });

    const heartbeat = setInterval(() => {
      try {
        res.write(': heartbeat\n\n');
      } catch {
        clearInterval(heartbeat);
      }
    }, 15000);

    req.on('close', () => {
      clearInterval(heartbeat);
      const clients = agentRunSseClients.get(key);
      if (!clients) return;
      clients.delete(res);
      if (clients.size === 0) {
        agentRunSseClients.delete(key);
      }
    });
  } catch (error) {
    console.error('Failed to stream Workspace Agent events:', error);
    if (!res.headersSent) {
      res.status(error.status || 500).json({ success: false, error: error.message });
    } else {
      res.end();
    }
  }
});

app.get('/api/agent/drafts/:id', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const draft = await loadAgentDraft(context.workspaceDir, req.params.id);
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

// 1.8 Static workflow run history
app.get('/api/workflow-runs/static', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const workspaceDir = context.workspaceDir;
    const status = req.query.status ? String(req.query.status) : null;
    const limit = req.query.limit ? Number(req.query.limit) : null;
    let runs = await listStaticRunFilesForWorkspace(workspaceDir, { summary: true });
    if (status) {
      runs = runs.filter((run) => run.status === status);
    }
    runs.sort((a, b) => Number(b.created_time || 0) - Number(a.created_time || 0));
    if (Number.isFinite(limit)) {
      runs = runs.slice(0, Math.max(0, limit));
    }
    res.json({ success: true, ...workspaceResponseFields(context), runs });
  } catch (error) {
    console.error('❌ 获取 static workflow runs 失败:', error);
    res.status(500).json({ error: error.message });
  }
});

app.get('/api/workflow-runs/static/:runId', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext(req.query);
    const workspaceDir = context.workspaceDir;
    const run = await loadStaticRun(workspaceDir, req.params.runId);
    res.json({ success: true, ...workspaceResponseFields(context), run });
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
    await loadStaticRun(workspaceDir, req.params.runId);
    const events = await loadStaticRunEvents(workspaceDir, req.params.runId, after);
    res.json({ success: true, ...workspaceResponseFields(context), runId: req.params.runId, events });
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
  try {
    const context = await resolveWorkspaceContext(req.query);
    const workspaceDir = context.workspaceDir;
    const run = await loadStaticRun(workspaceDir, req.params.runId);
    const taskId = String(req.query.taskId || '');
    const artifactPath = String(req.query.path || '');

    if (!taskId || !artifactPath) {
      return res.status(400).json({ error: 'taskId and path are required' });
    }

    const taskNode = Object.values(run.task_nodes || {}).find((node) => (
      node.maze_task_id === taskId
      || node.task_id === taskId
      || node.node_id === taskId
      || node.id === taskId
    ));
    const artifact = (taskNode?.artifacts || []).find((item) => item.path === artifactPath);
    if (!artifact) {
      return res.status(404).json({ error: 'Artifact not found' });
    }

    if (artifact.storage_path) {
      const artifactsDir = path.resolve(staticRunDir(workspaceDir, req.params.runId), 'artifacts');
      const fullPath = path.resolve(String(artifact.storage_path || ''));
      if (!fullPath.startsWith(artifactsDir + path.sep)) {
        return res.status(400).json({ error: 'Artifact path is outside this run' });
      }

      return res.download(fullPath, artifact.name || path.basename(fullPath));
    }

    if (!artifact.sha256) {
      return res.status(404).json({ error: 'Artifact storage path not found' });
    }

    const response = await fetch(`${MAZE_CORE_URL}/artifacts/sha256/${encodeURIComponent(artifact.sha256)}`);
    if (!response.ok) {
      return res.status(response.status).json({ error: `Failed to download artifact: HTTP ${response.status}` });
    }
    const data = Buffer.from(await response.arrayBuffer());
    res.setHeader('Content-Type', artifact.mime || 'application/octet-stream');
    res.setHeader('Content-Disposition', `attachment; filename="${encodeURIComponent(artifact.name || path.basename(artifact.path || 'artifact'))}"`);
    res.send(data);
  } catch (error) {
    console.error('❌ 下载 static workflow artifact 失败:', error);
    res.status(statusForFileError(error)).json({ error: error.message });
  }
});

app.delete('/api/workflow-runs/static/:runId', async (req, res) => {
  try {
    const context = await resolveWorkspaceContext({
      workspaceId: req.body?.workspaceId || req.query.workspaceId,
      workspaceDir: req.body?.workspaceDir || req.query.workspaceDir,
    });
    const workspaceDir = context.workspaceDir;
    const run = await loadStaticRun(workspaceDir, req.params.runId);
    if (!TERMINAL_STATIC_RUN_STATUSES.has(run.status)) {
      return res.status(400).json({ error: 'Only terminal workflow runs can be deleted' });
    }
    await fs.rm(staticRunDir(workspaceDir, req.params.runId), { recursive: true, force: true });
    res.json({ success: true, ...workspaceResponseFields(context), runId: req.params.runId, deleted: true });
  } catch (error) {
    const status = statusForFileError(error);
    if (status === 404) {
      console.warn(`⚠️ static workflow run not found for delete: ${req.params.runId}`);
    } else {
      console.error('❌ 删除 static workflow run 失败:', error);
    }
    res.status(status).json({ error: error.message });
  }
});

app.post('/api/workflow-runs/static/cleanup', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      statuses = ['completed', 'failed', 'canceled', 'interrupted'],
      older_than_days: olderThanDays = 7,
      keep_latest: keepLatestRaw,
      keepLatest,
      dry_run: dryRun = true,
    } = req.body || {};
    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const workspaceDir = context.workspaceDir;
    const statusSet = new Set(statuses);
    const cutoff = olderThanDays === null || olderThanDays === undefined
      ? null
      : nowEpochSeconds() - Number(olderThanDays) * 86400;
    let runs = (await listStaticRunFilesForWorkspace(workspaceDir)).filter((run) => (
      statusSet.has(run.status)
      && TERMINAL_STATIC_RUN_STATUSES.has(run.status)
      && (cutoff === null || Number(run.finished_time || run.updated_time || 0) <= cutoff)
    ));
    const keepLatestCount = Number(keepLatestRaw ?? keepLatest);
    if (Number.isFinite(keepLatestCount) && keepLatestCount > 0) {
      runs = runs
        .sort((a, b) => Number(b.finished_time || b.updated_time || b.created_time || 0) - Number(a.finished_time || a.updated_time || a.created_time || 0))
        .slice(Math.max(0, keepLatestCount));
    }

    const deletedRunIds = [];
    if (!dryRun) {
      for (const run of runs) {
        await fs.rm(staticRunDir(workspaceDir, run.run_id), { recursive: true, force: true });
        deletedRunIds.push(run.run_id);
      }
    }

    res.json({
      success: true,
      ...workspaceResponseFields(context),
      cleanup: {
        dry_run: dryRun,
        keep_latest: Number.isFinite(keepLatestCount) && keepLatestCount > 0 ? keepLatestCount : null,
        older_than_days: olderThanDays,
        matched_count: runs.length,
        deleted_count: deletedRunIds.length,
        runs,
        deleted_run_ids: deletedRunIds,
      },
    });
  } catch (error) {
    console.error('❌ 清理 static workflow runs 失败:', error);
    res.status(500).json({ error: error.message });
  }
});

app.post('/api/workflow-runs/static/migrate', async (req, res) => {
  try {
    const {
      workspaceId,
      workspaceDir: requestedWorkspaceDir,
      dry_run: dryRun = true,
    } = req.body || {};
    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const migration = await migrateLegacyStaticRuns(context.workspaceDir, { dryRun });
    res.json({
      success: true,
      ...workspaceResponseFields(context),
      migration,
    });
  } catch (error) {
    console.error('❌ 迁移 static workflow runs 失败:', error);
    res.status(500).json({ error: error.message });
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

// 3. 创建工作流
app.post('/api/workflows', async (req, res) => {
  try {
    const workflowId = uuidv4();
    const { name = 'Untitled Workflow' } = req.body;
    
    console.log(`📝 创建工作流: ${workflowId}`);
    
    // 调用 Python 创建 Maze 工作流
    const result = await callPython('create_workflow', {
      workflowId,
      serverUrl: 'http://localhost:8000'
    });
    
    if (!result.success) {
      console.error('❌ 创建 Maze 工作流失败:', result.error);
      return res.status(500).json({ error: result.error, traceback: result.traceback });
    }
    
    // 保存工作流信息
    workflows.set(workflowId, {
      id: workflowId,
      name,
      mazeWorkflowId: result.mazeWorkflowId,
      nodes: [],
      edges: [],
      createdAt: new Date().toISOString(),
      status: 'created'
    });
    
    console.log(`✅ 工作流创建成功 (Maze ID: ${result.mazeWorkflowId})`);
    res.json({ 
      workflowId, 
      name,
      mazeWorkflowId: result.mazeWorkflowId
    });
  } catch (error) {
    console.error('❌ 创建工作流失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// 4. 获取工作流详情
app.get('/api/workflows/:id', async (req, res) => {
  try {
    const { id } = req.params;
    const workflow = workflows.get(id);
    
    if (!workflow) {
      return res.status(404).json({ error: 'Workflow not found' });
    }
    
    res.json(workflow);
  } catch (error) {
    console.error('❌ 获取工作流失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// 5. 保存工作流（更新节点和边）
app.put('/api/workflows/:id', async (req, res) => {
  try {
    const { id } = req.params;
    const { name, nodes, edges } = req.body;
    
    const workflow = workflows.get(id);
    if (!workflow) {
      return res.status(404).json({ error: 'Workflow not found' });
    }
    
    console.log(`💾 保存工作流: ${id}`);
    if (typeof name === 'string' && name.trim()) {
      workflow.name = name.trim();
    }
    if (Array.isArray(nodes)) {
      workflow.nodes = nodes;
    }
    if (Array.isArray(edges)) {
      workflow.edges = edges;
    }
    console.log(`   名称: ${workflow.name}`);
    console.log(`   节点数: ${workflow.nodes.length}, 边数: ${workflow.edges.length}`);
    
    workflow.updatedAt = new Date().toISOString();
    workflows.set(id, workflow);
    
    console.log('✅ 工作流保存成功');
    res.json({ message: 'Saved successfully' });
  } catch (error) {
    console.error('❌ 保存工作流失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// 6. 运行工作流
app.post('/api/workflows/:id/run', async (req, res) => {
  try {
    const { id } = req.params;
    const workflow = workflows.get(id);
    
    if (!workflow) {
      return res.status(404).json({ error: 'Workflow not found' });
    }
    
    if (!workflow.nodes || workflow.nodes.length === 0) {
      return res.status(400).json({ error: 'Workflow has no task nodes' });
    }
    
    console.log(`\n🚀 开始运行工作流: ${id}`);
    console.log(`   名称: ${workflow.name}`);
    console.log(`   节点数: ${workflow.nodes.length}`);
    console.log(`   边数: ${workflow.edges.length}`);
    const context = await resolveWorkspaceContext(req.body || {});
    const workspaceDir = context.workspaceDir;
    const workflowRunId = uuidv4();
    const runSnapshot = createStaticRunSnapshot({
      runId: workflowRunId,
      workflow,
      workspaceDir,
      workspaceContext: context,
    });
    await saveStaticRun(workspaceDir, runSnapshot);
    
    // 更新状态
    workflow.status = 'running';
    workflow.activeRunId = workflowRunId;
    workflows.set(id, workflow);
    
    // 立即返回，异步执行工作流
    res.json({ 
      message: 'Workflow started running',
      workflowId: id,
      runId: workflowRunId,
      run: runSnapshot,
      ...workspaceResponseFields(context),
    });
    
    // 通知 WebSocket 客户端开始运行
    await recordAndBroadcastStaticRun(workflow, workspaceDir, workflowRunId, {
      type: 'workflow_started',
      data: {
        workflow_id: id,
        workflow_run_id: workflowRunId,
        workspace_id: context.workspaceId,
        workspace_manifest_version: context.workspaceManifestVersion,
      },
      timestamp: new Date().toISOString()
    });
    
    // 异步执行工作流
    (async () => {
      try {
        // 通知开始构建
        await recordAndBroadcastStaticRun(workflow, workspaceDir, workflowRunId, {
          type: 'building',
          data: {
            message: 'Building workflow...',
          },
          timestamp: new Date().toISOString()
        });
        
        console.log(`📦 准备执行工作流:`);
        console.log(`   节点: ${JSON.stringify(workflow.nodes.map(n => ({id: n.id, label: n.data.label})))}`);
        console.log(`   边: ${JSON.stringify(workflow.edges.map(e => ({from: e.source, to: e.target})))}`);
        
        // 调用 Python 运行工作流
        const result = await callPython(
          'run_workflow',
          {
            workflowId: id,
            staticRunId: workflowRunId,
            workspaceId: context.workspaceId,
            workspaceDir,
            workspaceManifestVersion: context.workspaceManifestVersion,
            nodes: workflow.nodes,
            edges: workflow.edges
          },
          async (progress) => {
            await recordAndBroadcastStaticRun(workflow, workspaceDir, workflowRunId, {
              ...progress,
              timestamp: new Date().toISOString(),
            });
          }
        );
        
        if (!result.success) {
          console.error('❌ 工作流执行失败:', result.error);
          workflow.status = 'failed';
          workflow.error = compactAgentDiagnosticText(result.error || 'Workflow failed', 2000);
          workflows.set(id, workflow);
          
          await recordAndBroadcastStaticRun(workflow, workspaceDir, workflowRunId, {
            type: 'workflow_failed',
            data: {
              error: workflow.error,
              traceback: result.traceback,
            },
            timestamp: new Date().toISOString()
          });
          return;
        }

        const latestRunAfterPython = await loadStaticRun(workspaceDir, workflowRunId).catch(() => null);
        if (latestRunAfterPython?.status === 'failed') {
          console.error('❌ 工作流任务失败:', latestRunAfterPython.error);
          workflow.status = 'failed';
          workflow.error = compactAgentDiagnosticText(latestRunAfterPython.error || 'Workflow task failed', 2000);
          workflow.lastRunId = workflowRunId;
          workflow.mazeRunId = result.mazeRunId;
          workflows.set(id, workflow);

          await recordAndBroadcastStaticRun(workflow, workspaceDir, workflowRunId, {
            type: 'workflow_failed',
            data: {
              error: workflow.error,
              traceback: result.traceback,
            },
            timestamp: new Date().toISOString()
          });
          return;
        }
        
        console.log('✅ 工作流执行成功');
        console.log('📊 结果数据:', JSON.stringify(result.results).substring(0, 200) + '...');
        workflow.status = 'completed';
        workflow.results = result.results;
        workflow.lastRunId = workflowRunId;
        workflow.mazeRunId = result.mazeRunId;
        workflows.set(id, workflow);

        if (result.mazeRunId) {
          await recordAndBroadcastStaticRun(workflow, workspaceDir, workflowRunId, {
            type: 'maze_run_created',
            data: {
              maze_run_id: result.mazeRunId,
            },
            timestamp: new Date().toISOString(),
          });
        }
        
        // 发送结果
        console.log(`📤 向工作流 ${id} 广播完成消息`);
        await recordAndBroadcastStaticRun(workflow, workspaceDir, workflowRunId, {
          type: 'workflow_completed',
          data: {
            results: result.results,
          },
          timestamp: new Date().toISOString()
        });
        console.log('✅ 完成消息已发送');
        
      } catch (error) {
        console.error('❌ 工作流执行异常:', error);
        workflow.status = 'failed';
        workflow.error = error.message;
        workflows.set(id, workflow);
        
        await recordAndBroadcastStaticRun(workflow, workspaceDir, workflowRunId, {
          type: 'workflow_failed',
          data: {
            error: error.message,
          },
          timestamp: new Date().toISOString()
        });
      }
    })();
    
  } catch (error) {
    console.error('❌ 运行工作流失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// 7. 获取工作流结果
app.get('/api/workflows/:id/results', async (req, res) => {
  try {
    const { id } = req.params;
    const workflow = workflows.get(id);
    
    if (!workflow) {
      return res.status(404).json({ error: 'Workflow not found' });
    }
    
    res.json({
      status: workflow.status,
      results: workflow.results || null,
      error: workflow.error || null
    });
  } catch (error) {
    console.error('❌ 获取结果失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// ========== WebSocket 处理 ==========

server.on('upgrade', (request, socket, head) => {
  const pathname = new URL(request.url, 'http://localhost').pathname;
  
  // 匹配 /ws/workflows/:id/results
  const match = pathname.match(/^\/ws\/workflows\/([^/]+)\/results$/);
  
  if (match) {
    wss.handleUpgrade(request, socket, head, (ws) => {
      const workflowId = match[1];
      
      console.log(`🔌 WebSocket 连接建立: ${workflowId}`);
      
      // 保存连接
      if (!wsConnections.has(workflowId)) {
        wsConnections.set(workflowId, new Set());
      }
      wsConnections.get(workflowId).add(ws);
      
      // 发送欢迎消息
      ws.send(JSON.stringify({
        type: 'connected',
        workflowId,
        message: 'Connected to workflow result stream',
        timestamp: new Date().toISOString()
      }));
      
      // 如果工作流已有结果，立即发送
      const workflow = workflows.get(workflowId);
      if (workflow) {
        if (workflow.status === 'completed' && workflow.results) {
          ws.send(JSON.stringify({
            type: 'workflow_completed',
            results: workflow.results,
            timestamp: new Date().toISOString()
          }));
        } else if (workflow.status === 'failed') {
          ws.send(JSON.stringify({
            type: 'workflow_failed',
            error: workflow.error,
            timestamp: new Date().toISOString()
          }));
        } else if (workflow.status === 'running') {
          ws.send(JSON.stringify({
            type: 'workflow_running',
            message: 'Workflow is running...',
            timestamp: new Date().toISOString()
          }));
        }
      }
      
      // 处理断开连接
      ws.on('close', () => {
        console.log(`🔌 WebSocket 连接断开: ${workflowId}`);
        const connections = wsConnections.get(workflowId);
        if (connections) {
          connections.delete(ws);
          if (connections.size === 0) {
            wsConnections.delete(workflowId);
          }
        }
      });
      
      ws.on('error', (error) => {
        console.error('WebSocket 错误:', error);
      });
    });
  } else {
    socket.destroy();
  }
});

// ========== 健康检查 ==========

app.get('/health', (req, res) => {
  res.json({ 
    status: 'ok',
    workflows: workflows.size,
    connections: wsConnections.size,
    timestamp: new Date().toISOString()
  });
});

// ========== 启动服务器 ==========

const PORT = process.env.PORT || 3001;

(async () => {
  try {
    const interrupted = await markInterruptedAgentRunsOnStartup();
    if (interrupted > 0) {
      console.log(`🧹 Marked ${interrupted} stale Workspace Agent run(s) as interrupted`);
    }
  } catch (error) {
    console.error('Failed to mark stale Workspace Agent runs:', error);
  }

  server.listen(PORT, () => {
    console.log('\n' + '='.repeat(60));
    console.log('  🚀 Maze Playground Backend Server');
    console.log('='.repeat(60));
    console.log(`\n✅ HTTP Server:   http://localhost:${PORT}`);
    console.log(`✅ API Endpoint:  http://localhost:${PORT}/api`);
    console.log(`✅ WebSocket:     ws://localhost:${PORT}/ws`);
    console.log(`✅ Health Check:  http://localhost:${PORT}/health`);
    console.log(`✅ Python Bridge: ${PYTHON_BIN}`);
    console.log('\n📡 等待前端连接...\n');
  });
})();

// 优雅关闭
process.on('SIGINT', () => {
  console.log('\n\n👋 正在关闭服务器...');
  
  // 关闭所有 WebSocket 连接
  wsConnections.forEach((connections) => {
    connections.forEach((ws) => {
      ws.close();
    });
  });
  
  server.close(() => {
    console.log('✅ 服务器已关闭');
    process.exit(0);
  });
});
