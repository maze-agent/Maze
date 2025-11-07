# Maze Board 前后端集成指南

本指南说明了 Maze Board 前端如何支持 **Mock 模式**和 **API 模式**的双重运行方式。

## 📋 目录

- [概述](#概述)
- [新增文件](#新增文件)
- [环境配置](#环境配置)
- [功能特性](#功能特性)
- [使用说明](#使用说明)
- [后端接口需求](#后端接口需求)

---

## 概述

Maze Board 现已支持两种数据模式：

- **🎭 Mock 模式**：使用本地 mock 数据，无需后端服务，适合前端开发和演示
- **🔌 API 模式**：连接真实后端 API，获取实时数据

用户可以通过右上角的开关在两种模式间切换，切换状态会保存在 `localStorage` 中。

---

## 新增文件

### 1. API 服务层
```
src/services/
  └── api.js           # API 客户端封装，包含所有后端接口调用
```

### 2. 自定义 Hooks
```
src/hooks/
  ├── index.js              # Hooks 导出
  ├── useRealtimeData.js    # 通用实时数据获取 Hook（支持轮询）
  ├── useWorkers.js         # Workers 数据 Hook
  └── useWorkflows.js       # Workflows 数据和操作 Hook
```

### 3. 上下文管理
```
src/contexts/
  └── DataContext.jsx   # 数据模式上下文（管理 mock/api 切换）
```

### 4. 通用组件
```
src/components/common/
  ├── index.js              # 组件导出
  ├── LoadingSpinner.jsx    # 加载动画组件
  ├── ErrorMessage.jsx      # 错误提示组件
  └── DataStatusWrapper.jsx # 数据状态包装组件
```

### 5. 配置文件
```
.env.example         # 环境变量示例文件
```

---

## 环境配置

### 创建 `.env` 文件

在 `web/maze_board/` 目录下创建 `.env` 文件（参考 `.env.example`）：

```env
# API 基础 URL
VITE_API_URL=http://localhost:8000

# WebSocket 基础 URL
VITE_WS_URL=ws://localhost:8000

# 数据刷新间隔（毫秒）
VITE_REFRESH_INTERVAL=5000

# 默认模式：mock 或 api
VITE_DATA_MODE=mock
```

### 配置说明

- `VITE_API_URL`：后端 API 的基础地址
- `VITE_WS_URL`：WebSocket 服务的基础地址
- `VITE_REFRESH_INTERVAL`：数据轮询刷新间隔（毫秒）
- `VITE_DATA_MODE`：启动时的默认模式（`mock` 或 `api`）

---

## 功能特性

### ✅ 已实现功能

1. **双模式支持**
   - Mock 模式：完全离线运行，使用 mockData.js 中的数据
   - API 模式：调用后端接口获取实时数据

2. **实时数据刷新**
   - 支持自动轮询刷新（可配置间隔）
   - 手动刷新按钮

3. **状态管理**
   - Loading 状态显示
   - Error 错误处理和重试
   - Empty 空数据提示

4. **页面功能**
   - ✅ Dashboard：实时监控概览
   - ✅ Workers：工作节点列表和资源使用
   - ✅ Workflows：工作流列表和服务控制
   - ⏳ WorkflowDetail：工作流详情（目前仅 Mock）
   - ⏳ WorkflowRunDetail：运行详情（目前仅 Mock）

5. **服务控制**
   - Start/Pause/Resume/Stop 工作流服务
   - Mock 模式本地状态更新
   - API 模式调用后端接口

---

## 使用说明

### 启动项目

```bash
cd web/maze_board
npm install
npm run dev
```

### 模式切换

1. 页面右上角有一个切换开关
2. 黄色 🎭 表示 Mock 模式
3. 绿色 🔌 表示 API 模式
4. 点击开关即可切换模式

### 开发流程

#### Mock 模式开发（无需后端）

1. 确保 `.env` 中 `VITE_DATA_MODE=mock`
2. 启动前端：`npm run dev`
3. 在浏览器中访问，默认使用 Mock 数据

#### API 模式开发（连接后端）

1. 启动后端服务（确保运行在配置的端口）
2. 切换到 API 模式（右上角开关）
3. 前端将自动调用后端接口

---

## 后端接口需求

### 必需接口清单

#### 1. Worker APIs

```http
GET /api/workers
Response: {
  "status": "success",
  "workers": [
    {
      "worker_id": "worker-1",
      "hostname": "node-01.cluster",
      "cpu_total": 16,
      "cpu_used": 8,
      "memory_total_gb": 64,
      "memory_used_gb": 32,
      "gpu_total": 2,
      "gpu_used": 1,
      "status": "active"
    }
  ]
}
```

#### 2. Workflow APIs

```http
# 获取所有工作流
GET /api/workflows
Response: {
  "status": "success",
  "workflows": [
    {
      "workflow_id": "wf-001",
      "workflow_name": "ML Training Pipeline",
      "created_at": "2025-10-15T08:00:00",
      "total_requests": 125,
      "service_status": "running"
    }
  ]
}

# 获取工作流详情
GET /api/workflows/{workflow_id}
Response: {
  "status": "success",
  "workflow": {
    "workflow_id": "wf-001",
    "workflow_name": "ML Training Pipeline",
    "nodes": [...],
    "edges": [...],
    "api_config": {...}
  }
}

# 服务控制
POST /api/workflows/{workflow_id}/start
POST /api/workflows/{workflow_id}/pause
POST /api/workflows/{workflow_id}/resume
POST /api/workflows/{workflow_id}/stop
Response: {
  "status": "success",
  "message": "Service started successfully"
}
```

#### 3. Run History APIs

```http
# 获取工作流运行历史
GET /api/workflows/{workflow_id}/runs?limit=50&offset=0
Response: {
  "status": "success",
  "runs": [
    {
      "run_id": "run-001-001",
      "workflow_id": "wf-001",
      "status": "completed",
      "started_at": "2025-10-22T10:00:00",
      "completed_at": "2025-10-22T10:15:30",
      "duration": 930,
      "total_tasks": 6,
      "completed_tasks": 6
    }
  ]
}

# 获取单次运行详情
GET /api/runs/{run_id}
Response: {
  "status": "success",
  "run": {...},
  "task_executions": [...]
}
```

#### 4. Dashboard APIs

```http
GET /api/dashboard/summary
Response: {
  "status": "success",
  "summary": {
    "workers": {
      "total": 4,
      "active": 2,
      "idle": 1,
      "offline": 1
    },
    "workflows": {
      "total": 3,
      "running": 2,
      "paused": 1
    },
    "resources": {
      "cpu_total": 56,
      "cpu_used": 24,
      "memory_total_gb": 224,
      "memory_used_gb": 92
    }
  }
}
```

#### 5. WebSocket (可选)

```http
WebSocket /api/monitoring/stream
Message: {
  "type": "worker_update" | "workflow_update",
  "data": {...}
}
```

---

## API 实现示例

后端可以参考以下 FastAPI 实现模板：

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/workers")
async def get_workers():
    # 从实际系统获取 worker 信息
    workers = []  # TODO: 实现
    return {"status": "success", "workers": workers}

@app.get("/api/workflows")
async def get_workflows():
    # 从实际系统获取 workflow 信息
    workflows = []  # TODO: 实现
    return {"status": "success", "workflows": workflows}

@app.post("/api/workflows/{workflow_id}/{action}")
async def control_workflow(workflow_id: str, action: str):
    # 实现服务控制逻辑
    # action: start, pause, resume, stop
    return {"status": "success", "message": f"Service {action}ed"}
```

---

## 代码架构

### 数据流程

```
┌─────────────┐
│   用户点击   │
└──────┬──────┘
       │
       ▼
┌─────────────────┐      ┌──────────────┐
│  DataContext    │─────▶│  dataMode    │
│  (全局上下文)    │      │ (mock/api)   │
└────────┬────────┘      └──────────────┘
         │
         ▼
┌─────────────────┐
│  Custom Hooks   │
│ (useWorkers等)  │
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
Mock模式    API模式
    │         │
    ▼         ▼
┌────────┐ ┌────────┐
│mockData│ │ API调用 │
└────────┘ └────────┘
```

### 组件层次

```
App (DataProvider)
  │
  ├─ MainLayout
  │    ├─ Navbar (模式切换开关)
  │    └─ Page Components
  │         ├─ Dashboard (useWorkers, useWorkflows)
  │         ├─ Workers (useWorkers)
  │         └─ Workflows (useWorkflows)
  │              ├─ DataStatusWrapper (Loading/Error)
  │              └─ 数据展示
```

---

## 注意事项

1. **CORS 配置**：确保后端正确配置了 CORS，允许前端跨域访问

2. **数据格式**：后端返回的数据格式需要与 `mockData.js` 保持一致

3. **错误处理**：所有接口应返回统一的错误格式
   ```json
   {
     "status": "fail",
     "message": "错误描述"
   }
   ```

4. **刷新频率**：生产环境建议将 `VITE_REFRESH_INTERVAL` 设置为较大值（如 10000ms）以减少服务器负载

5. **模式持久化**：用户选择的模式会保存在 `localStorage` 中，刷新页面后保持

---

## 下一步开发

### Phase 1：完善基础监控（高优先级）
- [x] Workers 页面 API 接入
- [x] Workflows 页面 API 接入  
- [x] Dashboard 页面 API 接入
- [ ] WorkflowDetail 页面完整 API 支持
- [ ] WorkflowRunDetail 页面完整 API 支持

### Phase 2：实时功能（中优先级）
- [ ] WebSocket 实时数据推送
- [ ] 任务执行状态实时更新
- [ ] 资源使用实时图表

### Phase 3：增强功能（低优先级）
- [ ] 历史数据趋势图
- [ ] 告警和通知
- [ ] 日志查看功能
- [ ] 性能分析面板

---

## 技术支持

如有问题，请参考：
- Mock 数据结构：`src/utils/mockData.js`
- API 服务：`src/services/api.js`
- Hooks 实现：`src/hooks/`

祝开发顺利！🚀

