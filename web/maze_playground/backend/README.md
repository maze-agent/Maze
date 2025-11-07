# Maze Playground V2 - Backend

Node.js + Python 混合后端，用于桥接前端和 Maze Client。

## 🏗️ 架构

```
Express Server (Node.js)
    ↓
maze_bridge.py (Python)
    ↓
Maze Client (Python)
    ↓
Maze Server (FastAPI)
```

## 📋 核心功能

### 1. Python 桥接 (`maze_bridge.py`)

提供以下功能：

- **`get_builtin_tasks`**: 扫描并返回所有内置任务的元数据
- **`parse_custom_function`**: 解析用户编写的 `@task` 或 `@tool` 装饰函数
- **`create_workflow`**: 创建 Maze 工作流实例
- **`run_workflow`**: 构建并执行完整的工作流

### 2. Express 服务器 (`src/server.js`)

提供 RESTful API 和 WebSocket 服务：

**HTTP API**:
- `GET /api/builtin-tasks` - 获取内置任务列表
- `POST /api/parse-custom-function` - 解析自定义函数
- `POST /api/workflows` - 创建工作流
- `GET /api/workflows/:id` - 获取工作流详情
- `PUT /api/workflows/:id` - 保存工作流
- `POST /api/workflows/:id/run` - 运行工作流
- `GET /api/workflows/:id/results` - 获取运行结果

**WebSocket**:
- `ws://localhost:3001/ws/workflows/:id/results` - 实时推送工作流执行状态和结果

## 🧪 测试

### 测试 Python 桥接

```bash
cd backend
python test_bridge.py
```

这将测试：
- 获取内置任务
- 解析自定义函数
- 创建工作流（需要 Maze 服务器运行）

### 测试完整后端

```bash
# 启动 Maze 服务器
cd E:\PythonProject\Maze
uvicorn maze.core.server:app --port 8000

# 启动后端
cd web/maze_playground/v2/backend
npm run dev

# 使用 curl 测试
curl http://localhost:3001/health
curl http://localhost:3001/api/builtin-tasks
```

## 🔍 调试

### 查看日志

Node.js 服务器会输出详细日志：

```
✅ Backend server running on http://localhost:3001
📋 获取内置任务列表...
✅ 成功获取 3 个内置任务
📝 创建工作流: abc-123-def
✅ 工作流创建成功
🚀 开始运行工作流: abc-123-def
```

### Python 错误

如果 Python 调用失败，检查：

1. Python 是否在 PATH 中
2. Maze 包是否已安装
3. Python 路径是否正确

可以手动测试：

```bash
cd backend
python maze_bridge.py get_builtin_tasks '{}'
```

## 📦 依赖

### Node.js
- `express` - Web 服务器
- `cors` - 跨域支持
- `ws` - WebSocket 服务
- `uuid` - 生成唯一 ID

### Python
- `maze` - Maze 框架
- Python 标准库

## 🔧 配置

### 端口
- Backend: `3001`
- Maze Server: `8000`

修改端口：

```javascript
// src/server.js
const PORT = process.env.PORT || 3001;
```

```python
# maze_bridge.py
def create_maze_workflow(workflow_id, server_url="http://localhost:8000"):
```

## 🐛 常见问题

### 问题：Python 进程找不到
**错误**: `spawn python ENOENT`

**解决**:
```bash
# Windows
where python

# 如果没有输出，安装 Python 并添加到 PATH
```

### 问题：导入 Maze 失败
**错误**: `ModuleNotFoundError: No module named 'maze'`

**解决**:
```bash
# 确保在 Maze 项目根目录
cd E:\PythonProject\Maze
pip install -e .
```

### 问题：WebSocket 连接失败
**检查**:
1. Backend 是否在运行
2. 防火墙是否阻止 3001 端口
3. 浏览器控制台是否有错误

## 📊 性能

### 工作流执行

- 小型工作流 (1-3个节点): < 5秒
- 中型工作流 (4-10个节点): 5-30秒
- 大型工作流 (10+个节点): > 30秒

### 内存使用

- Node.js 进程: ~50-100MB
- Python 子进程: ~100-200MB (每次调用)
- Ray 进程: 根据任务资源需求

## 🚀 生产部署

### 环境变量

```bash
# .env
PORT=3001
MAZE_SERVER_URL=http://localhost:8000
NODE_ENV=production
```

### PM2 部署

```bash
npm install -g pm2

pm2 start src/server.js --name maze-backend
pm2 logs maze-backend
pm2 restart maze-backend
```

### Docker (未来)

```dockerfile
FROM node:18
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
EXPOSE 3001
CMD ["npm", "start"]
```

## 📝 开发指南

### 添加新的 API

1. 在 `src/server.js` 添加路由
2. 如需 Python 功能，在 `maze_bridge.py` 添加函数
3. 更新文档

### 添加新的 WebSocket 事件

修改 `run_workflow` 中的 `broadcastToWorkflow` 调用：

```javascript
broadcastToWorkflow(id, {
  type: 'custom_event',
  data: yourData,
  timestamp: new Date().toISOString()
});
```

## 📚 相关文档

- [Maze 框架文档](../../../README.md)
- [前端文档](../frontend/README.md)
- [API 文档](./API.md) (待补充)

