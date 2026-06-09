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
  if (process.env.CONDA_PREFIX) {
    return path.join(
      process.env.CONDA_PREFIX,
      process.platform === 'win32' ? 'python.exe' : 'bin/python'
    );
  }

  const defaultMazePython = '/root/miniconda3/envs/maze/bin/python';
  if (process.platform !== 'win32' && fsSync.existsSync(defaultMazePython)) {
    return defaultMazePython;
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
        },
        write: {
          '*': 'ask',
          '.env': 'deny',
          '.env.*': 'deny',
          '*secret*': 'deny',
          '*credential*': 'deny',
          '*token*': 'deny',
          'api_key*': 'deny',
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
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, {
      ...options,
      signal: controller.signal,
    });
  } finally {
    clearTimeout(timer);
  }
}

async function callOpenAICompatibleChat({
  baseUrl,
  apiKey,
  model,
  messages,
  temperature = 0.2,
  maxTokens = 2048,
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
    snapshot.status = 'completed';
    snapshot.finished_time = snapshot.finished_time || eventTime;
    snapshot.final_result = data.results ?? snapshot.final_result;
  } else if (event.type === 'workflow_failed') {
    snapshot.status = 'failed';
    snapshot.finished_time = snapshot.finished_time || eventTime;
    snapshot.error = data.error || 'Workflow failed';
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
    node.error = data.result || 'Task failed';
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
        const result = JSON.parse(output || '{}');
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
      targetPath,
      artifact = {},
      runId,
      taskId,
      path: artifactPath,
      sha256,
      storagePath,
      overwrite = true,
    } = req.body || {};

    const sourceSha = String(sha256 || artifact.sha256 || '').trim();
    const sourceStoragePath = String(storagePath || artifact.storage_path || '').trim();
    const sourceArtifactPath = String(artifactPath || artifact.path || artifact.name || sourceSha || '').trim();
    const destinationPath = targetPath || sourceArtifactPath;

    if (!destinationPath) {
      return res.status(400).json({ error: 'targetPath is required' });
    }
    if (!sourceSha && !sourceStoragePath) {
      return res.status(400).json({ error: 'artifact sha256 or storagePath is required' });
    }

    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const workspaceDir = context.workspaceDir;
    const { fullPath, filesDir, relativePath } = resolveWorkspaceFilePath(workspaceDir, destinationPath);
    if (!overwrite && await fileExists(fullPath)) {
      return res.status(409).json({ error: `Workspace file already exists: ${relativePath}` });
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
        return res.status(400).json({ error: 'Static artifact storage path is outside this workspace run directory' });
      }
      await fs.copyFile(resolvedStoragePath, fullPath);
    } else {
      const response = await fetch(`${MAZE_CORE_URL}/artifacts/sha256/${encodeURIComponent(sourceSha)}`);
      if (!response.ok) {
        return res.status(response.status).json({ error: `Failed to download artifact: HTTP ${response.status}` });
      }
      const data = Buffer.from(await response.arrayBuffer());
      await fs.writeFile(fullPath, data);
    }

    const file = await describeWorkspaceFile(filesDir, fullPath);
    const manifest = await recordWorkspaceMutation(workspaceDir, 'artifact_promoted', {
      path: file.relativePath,
      runId: runId || artifact.run_id || null,
      taskId: taskId || artifact.task_id || artifact.producer_task_id || null,
      sha256: sourceSha || artifact.sha256 || null,
    });

    res.json({
      success: true,
      workspaceId: manifest.workspace_id,
      workspaceDir,
      workspaceManifestVersion: Number(manifest.manifest_version || context.workspaceManifestVersion),
      file,
    });
  } catch (error) {
    console.error('❌ Promote artifact 失败:', error);
    res.status(500).json({ error: error.message });
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
    } = req.body || {};
    const context = await resolveWorkspaceContext({ workspaceId, workspaceDir: requestedWorkspaceDir });
    const workspaceDir = context.workspaceDir;

    const extraEnv = {};
    if (llm?.apiKey) {
      extraEnv.MAZE_REACT_API_KEY = String(llm.apiKey);
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
      },
      extraEnv,
    );

    res.json({ ...started, ...workspaceResponseFields(context) });
  } catch (error) {
    console.error('Failed to start ReAct workflow:', error);
    res.status(500).json({ success: false, error: error.message });
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

    const taskNode = Object.values(run.task_nodes || {}).find((node) => node.maze_task_id === taskId || node.node_id === taskId);
    const artifact = (taskNode?.artifacts || []).find((item) => item.path === artifactPath);
    if (!artifact) {
      return res.status(404).json({ error: 'Artifact not found' });
    }

    const artifactsDir = path.resolve(staticRunDir(workspaceDir, req.params.runId), 'artifacts');
    const fullPath = path.resolve(String(artifact.storage_path || ''));
    if (!fullPath.startsWith(artifactsDir + path.sep)) {
      return res.status(400).json({ error: 'Artifact path is outside this run' });
    }

    res.download(fullPath, artifact.name || path.basename(fullPath));
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
          workflow.error = result.error;
          workflows.set(id, workflow);
          
          await recordAndBroadcastStaticRun(workflow, workspaceDir, workflowRunId, {
            type: 'workflow_failed',
            data: {
              error: result.error,
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
