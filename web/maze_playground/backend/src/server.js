import express from 'express';
import cors from 'cors';
import { WebSocketServer } from 'ws';
import { spawn } from 'child_process';
import { v4 as uuidv4 } from 'uuid';
import path from 'path';
import { fileURLToPath } from 'url';
import http from 'http';

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
    const { nodes, edges } = req.body;
    
    const workflow = workflows.get(id);
    if (!workflow) {
      return res.status(404).json({ error: '工作流不存在' });
    }
    
    console.log(`💾 保存工作流: ${id}`);
    console.log(`   节点数: ${nodes.length}, 边数: ${edges.length}`);
    
    workflow.nodes = nodes;
    workflow.edges = edges;
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
