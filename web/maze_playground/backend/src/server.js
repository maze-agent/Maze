import express from 'express';
import cors from 'cors';
import { WebSocketServer } from 'ws';
import { spawn } from 'child_process';
import { v4 as uuidv4 } from 'uuid';
import path from 'path';
import { fileURLToPath } from 'url';
import http from 'http';
import fs from 'fs/promises';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const app = express();
const server = http.createServer(app);
const wss = new WebSocketServer({ noServer: true });

app.use(cors());
app.use(express.json({ limit: '10mb' }));

// 存储工作流状态
const workflows = new Map();
// 存储 WebSocket 连接
const wsConnections = new Map(); // workflowId -> Set<WebSocket>
const PROJECT_ROOT = path.resolve(__dirname, '../../../..');
const DEFAULT_WORKSPACE_DIR = process.env.MAZE_WORKSPACE_DIR || path.join(PROJECT_ROOT, 'workspace');

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

async function ensureWorkspaceDirs(workspaceDir) {
  const resolved = path.resolve(String(workspaceDir || DEFAULT_WORKSPACE_DIR));
  await fs.mkdir(resolved, { recursive: true });
  await fs.mkdir(path.join(resolved, 'tasks'), { recursive: true });
  await fs.mkdir(path.join(resolved, 'workflows'), { recursive: true });
  return resolved;
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
    const entryPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...await listWorkflowFiles(entryPath));
    } else if (entry.isFile() && entry.name.endsWith('.json')) {
      files.push(entryPath);
    }
  }

  return files;
}

function normalizeWorkflowPayload(payload) {
  const workflow = payload?.workflow || payload;
  const nodes = workflow?.nodes;
  const edges = workflow?.edges;
  const rawTaskDefinitions = workflow?.taskDefinitions || payload?.taskDefinitions || [];

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
    taskDefinitions: Array.isArray(rawTaskDefinitions) ? rawTaskDefinitions : [],
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

function collectTaskDefinitions(nodes, explicitDefinitions = []) {
  const definitions = new Map();

  const upsert = (definition) => {
    const relativePath = definition?.relativePath || definition?.taskPath;
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

async function importTaskDefinitions(workspaceDir, taskDefinitions = []) {
  const imported = [];
  const skipped = [];

  for (const definition of collectTaskDefinitions([], taskDefinitions)) {
    if (!definition.code || !String(definition.code).trim()) {
      skipped.push({ relativePath: definition.relativePath, reason: 'empty-code' });
      continue;
    }

    const { relativePath, fullPath } = resolveTaskDefinitionFile(workspaceDir, definition.relativePath);
    if (await fileExists(fullPath)) {
      skipped.push({ relativePath, reason: 'exists' });
      continue;
    }

    await fs.mkdir(path.dirname(fullPath), { recursive: true });
    await fs.writeFile(fullPath, definition.code, 'utf-8');
    imported.push({ relativePath });
  }

  return { imported, skipped };
}

function applyWorkspaceToWorkflowNodes(nodes, workspaceDir, taskDefinitions = []) {
  const definitionsByPath = new Map(
    collectTaskDefinitions([], taskDefinitions).map((definition) => [definition.relativePath, definition])
  );

  return nodes.map((node) => {
    if (node?.data?.category !== 'workspace') {
      return node;
    }

    const relativePath = normalizeTaskRelativePath(node.data.taskPath || node.data.relativePath);
    const definition = definitionsByPath.get(relativePath);

    return {
      ...node,
      data: {
        ...node.data,
        workspaceDir,
        taskPath: relativePath,
        customCode: definition?.code || node.data.customCode || '',
      },
    };
  });
}

// ========== Python 桥接函数 ==========

function callPython(action, params = {}, onProgress = null) {
  return new Promise((resolve, reject) => {
    const bridgePath = path.join(__dirname, '../maze_bridge.py');
    
    // 设置 Python 环境变量，强制使用 UTF-8 编码
    const python = spawn('python', [bridgePath, action, JSON.stringify(params)], {
      env: {
        ...process.env,
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
            if (onProgress) onProgress(progress);
          } catch (e) {
            console.error('解析进度消息失败:', raw);
          }
        } else if (line.trim()) {
          console.error('Python stderr:', line);
        }
      });
    });
    
    python.on('close', (code) => {
      if (code === 0) {
        try {
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

// ========== API 路由 ==========

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
    const workspaceDir = await ensureWorkspaceDirs(req.query.workspaceDir || DEFAULT_WORKSPACE_DIR);
    console.log(`📁 扫描工作目录任务: ${workspaceDir}`);

    const result = await callPython('get_workspace_tasks', { workspaceDir });

    if (result.error) {
      console.error('❌ 扫描工作目录失败:', result.error);
      return res.status(400).json({ error: result.error, traceback: result.traceback });
    }

    console.log(`✅ 成功获取 ${result.tasks.length} 个工作区任务`);
    res.json(result);
  } catch (error) {
    console.error('❌ 获取工作区任务失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// 1.2 保存工作目录任务
app.post('/api/workspace-tasks', async (req, res) => {
  try {
    const {
      workspaceDir: requestedWorkspaceDir = DEFAULT_WORKSPACE_DIR,
      relativePath = 'tasks/custom_task.py',
      code,
      parse = true,
    } = req.body;
    const workspaceDir = await ensureWorkspaceDirs(requestedWorkspaceDir);

    console.log(`💾 保存工作区任务: ${workspaceDir}/${relativePath}`);

    if ((!code || !code.trim()) && parse) {
      return res.status(400).json({ error: '代码不能为空' });
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
    res.json(result);
  } catch (error) {
    console.error('❌ 保存工作区任务失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// 1.2.1 删除工作目录任务
app.delete('/api/workspace-tasks', async (req, res) => {
  try {
    const {
      workspaceDir: requestedWorkspaceDir = DEFAULT_WORKSPACE_DIR,
      relativePath,
    } = req.body;

    if (!relativePath) {
      return res.status(400).json({ error: 'relativePath is required' });
    }

    const workspaceDir = await ensureWorkspaceDirs(requestedWorkspaceDir);
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
    res.json(result);
  } catch (error) {
    console.error('❌ 删除工作区任务失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// 1.2.2 重命名工作目录任务
app.patch('/api/workspace-tasks/rename', async (req, res) => {
  try {
    const {
      workspaceDir: requestedWorkspaceDir = DEFAULT_WORKSPACE_DIR,
      relativePath,
      oldFunctionName,
      newName,
    } = req.body;

    if (!relativePath || !oldFunctionName || !newName) {
      return res.status(400).json({ error: 'relativePath, oldFunctionName, and newName are required' });
    }

    const workspaceDir = await ensureWorkspaceDirs(requestedWorkspaceDir);
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
    res.json(result);
  } catch (error) {
    console.error('❌ 重命名工作区任务失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// 1.3 获取工作目录工作流列表
app.get('/api/workspace-workflows', async (req, res) => {
  try {
    const workspaceDir = await ensureWorkspaceDirs(req.query.workspaceDir || DEFAULT_WORKSPACE_DIR);
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
      workspaceDir,
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
      workspaceDir: requestedWorkspaceDir = DEFAULT_WORKSPACE_DIR,
      relativePath,
    } = req.body;

    if (!relativePath) {
      return res.status(400).json({ error: 'relativePath is required' });
    }

    const workspaceDir = await ensureWorkspaceDirs(requestedWorkspaceDir);
    const { relativePath: workflowPath, fullPath } = resolveWorkflowFile(workspaceDir, relativePath, 'workflow');
    await fs.unlink(fullPath);

    console.log(`🗑️ 工作流已删除: ${workflowPath}`);
    res.json({
      success: true,
      workspaceDir,
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
      workspaceDir: requestedWorkspaceDir = DEFAULT_WORKSPACE_DIR,
      relativePath,
      name,
    } = req.body;

    if (!relativePath || !name || !String(name).trim()) {
      return res.status(400).json({ error: 'relativePath and name are required' });
    }

    const workspaceDir = await ensureWorkspaceDirs(requestedWorkspaceDir);
    const { relativePath: workflowPath, fullPath } = resolveWorkflowFile(workspaceDir, relativePath, name);
    const raw = await fs.readFile(fullPath, 'utf-8');
    const payload = JSON.parse(raw);
    const normalized = normalizeWorkflowPayload(payload);
    const nextPayload = {
      schema: payload?.schema || 'maze-playground-workflow',
      version: Math.max(payload?.version || 1, 2),
      savedAt: new Date().toISOString(),
      workflow: {
        ...(payload?.workflow || {}),
        name: String(name).trim(),
        nodes: normalized.nodes,
        edges: normalized.edges,
        taskDefinitions: collectTaskDefinitions(normalized.nodes, normalized.taskDefinitions),
      },
    };

    await fs.writeFile(fullPath, JSON.stringify(nextPayload, null, 2), 'utf-8');

    console.log(`✏️ 工作流已重命名: ${workflowPath}`);
    res.json({
      success: true,
      workspaceDir,
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
      workspaceDir: requestedWorkspaceDir = DEFAULT_WORKSPACE_DIR,
      relativePath,
      name = 'Untitled Workflow',
      workflowId = null,
      nodes = [],
      edges = [],
      taskDefinitions = [],
    } = req.body;

    if (!Array.isArray(nodes) || !Array.isArray(edges)) {
      return res.status(400).json({ error: 'nodes and edges must be arrays' });
    }

    const workspaceDir = await ensureWorkspaceDirs(requestedWorkspaceDir);
    const workflowNodes = nodes.map((node) => {
      if (node?.data?.category === 'workspace') {
        const relativePath = normalizeTaskRelativePath(node.data.taskPath || node.data.relativePath);
        return {
          ...node,
          data: {
            ...node.data,
            workspaceDir,
            taskPath: relativePath,
          },
        };
      }
      return node;
    });
    const workflowTaskDefinitions = collectTaskDefinitions(workflowNodes, taskDefinitions);
    const { relativePath: savedRelativePath, fullPath } = resolveWorkflowFile(workspaceDir, relativePath, name);
    const payload = {
      schema: 'maze-playground-workflow',
      version: 2,
      savedAt: new Date().toISOString(),
      workflow: {
        name,
        sourceWorkflowId: workflowId,
        nodes: workflowNodes,
        edges,
        taskDefinitions: workflowTaskDefinitions,
      },
    };

    await fs.mkdir(path.dirname(fullPath), { recursive: true });
    await fs.writeFile(fullPath, JSON.stringify(payload, null, 2), 'utf-8');

    console.log(`💾 工作流已保存到工作区: ${savedRelativePath}`);
    res.json({
      success: true,
      workspaceDir,
      relativePath: savedRelativePath,
      workflow: payload.workflow,
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
      workspaceDir: requestedWorkspaceDir = DEFAULT_WORKSPACE_DIR,
      relativePath,
    } = req.body;

    if (!relativePath) {
      return res.status(400).json({ error: 'relativePath is required' });
    }

    const workspaceDir = await ensureWorkspaceDirs(requestedWorkspaceDir);
    const { relativePath: loadedRelativePath, fullPath } = resolveWorkflowFile(workspaceDir, relativePath, 'workflow');
    const raw = await fs.readFile(fullPath, 'utf-8');
    const payload = JSON.parse(raw);
    const workflow = normalizeWorkflowPayload(payload);
    const importedTaskDefinitions = await importTaskDefinitions(workspaceDir, workflow.taskDefinitions);
    workflow.nodes = applyWorkspaceToWorkflowNodes(workflow.nodes, workspaceDir, workflow.taskDefinitions);

    res.json({
      success: true,
      workspaceDir,
      relativePath: loadedRelativePath,
      workflow,
      importedTaskDefinitions,
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
      workspaceDir: requestedWorkspaceDir = DEFAULT_WORKSPACE_DIR,
      payload,
    } = req.body;

    if (!payload) {
      return res.status(400).json({ error: 'payload is required' });
    }

    const workspaceDir = await ensureWorkspaceDirs(requestedWorkspaceDir);
    const workflow = normalizeWorkflowPayload(payload);
    const importedTaskDefinitions = await importTaskDefinitions(workspaceDir, workflow.taskDefinitions);
    workflow.nodes = applyWorkspaceToWorkflowNodes(workflow.nodes, workspaceDir, workflow.taskDefinitions);

    res.json({
      success: true,
      workspaceDir,
      workflow,
      importedTaskDefinitions,
    });
  } catch (error) {
    console.error('❌ 导入工作区工作流失败:', error);
    res.status(500).json({ error: error.message });
  }
});

// 2. 解析自定义函数
app.post('/api/parse-custom-function', async (req, res) => {
  try {
    const { code } = req.body;
    console.log('🔍 解析自定义函数...');
    
    if (!code || !code.trim()) {
      return res.status(400).json({ error: '代码不能为空' });
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
    const { name = '未命名工作流' } = req.body;
    
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
      return res.status(404).json({ error: '工作流不存在' });
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
      return res.status(404).json({ error: '工作流不存在' });
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
    res.json({ message: '保存成功' });
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
      return res.status(404).json({ error: '工作流不存在' });
    }
    
    if (!workflow.nodes || workflow.nodes.length === 0) {
      return res.status(400).json({ error: '工作流没有任务节点' });
    }
    
    console.log(`\n🚀 开始运行工作流: ${id}`);
    console.log(`   名称: ${workflow.name}`);
    console.log(`   节点数: ${workflow.nodes.length}`);
    console.log(`   边数: ${workflow.edges.length}`);
    
    // 更新状态
    workflow.status = 'running';
    workflows.set(id, workflow);
    
    // 立即返回，异步执行工作流
    res.json({ 
      message: '工作流已开始运行',
      workflowId: id
    });
    
    // 通知 WebSocket 客户端开始运行
    broadcastToWorkflow(id, {
      type: 'workflow_started',
      workflowId: id,
      timestamp: new Date().toISOString()
    });
    
    // 异步执行工作流
    (async () => {
      try {
        // 通知开始构建
        broadcastToWorkflow(id, {
          type: 'building',
          message: '正在构建工作流...',
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
            nodes: workflow.nodes,
            edges: workflow.edges
          },
          (progress) => {
            broadcastToWorkflow(id, {
              type: 'task_update',
              event: progress,
              timestamp: new Date().toISOString()
            });
          }
        );
        
        if (!result.success) {
          console.error('❌ 工作流执行失败:', result.error);
          workflow.status = 'failed';
          workflow.error = result.error;
          workflows.set(id, workflow);
          
          broadcastToWorkflow(id, {
            type: 'workflow_failed',
            error: result.error,
            traceback: result.traceback,
            timestamp: new Date().toISOString()
          });
          return;
        }
        
        console.log('✅ 工作流执行成功');
        console.log('📊 结果数据:', JSON.stringify(result.results).substring(0, 200) + '...');
        workflow.status = 'completed';
        workflow.results = result.results;
        workflows.set(id, workflow);
        
        // 发送结果
        console.log(`📤 向工作流 ${id} 广播完成消息`);
        broadcastToWorkflow(id, {
          type: 'workflow_completed',
          results: result.results,
          timestamp: new Date().toISOString()
        });
        console.log('✅ 完成消息已发送');
        
      } catch (error) {
        console.error('❌ 工作流执行异常:', error);
        workflow.status = 'failed';
        workflow.error = error.message;
        workflows.set(id, workflow);
        
        broadcastToWorkflow(id, {
          type: 'workflow_failed',
          error: error.message,
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
      return res.status(404).json({ error: '工作流不存在' });
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
        message: '已连接到工作流结果推送',
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
            message: '工作流正在运行中...',
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
