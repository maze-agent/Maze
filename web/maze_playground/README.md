# Maze Workflow Playground V2

基于 React + Node.js 的现代化工作流设计器

## 🏗️ 技术栈

### Frontend
- React 18 + Vite
- ReactFlow - 工作流可视化
- Ant Design - UI组件库
- Zustand - 状态管理
- Monaco Editor - 代码编辑器

### Backend
- Express.js - Node.js服务器
- Python Maze Client - 工作流引擎

## 📂 项目结构

```
v2/
├── frontend/          # React前端
│   ├── src/
│   │   ├── components/
│   │   ├── stores/
│   │   ├── api/
│   │   ├── types/
│   │   └── App.tsx
│   ├── package.json
│   └── vite.config.ts
├── backend/           # Node.js后端
│   ├── src/
│   │   ├── routes/
│   │   ├── services/
│   │   └── server.js
│   ├── maze_bridge.py
│   └── package.json
└── README.md
```

## 🚀 快速开始

### 安装依赖

```bash
# 安装前端依赖
cd frontend
npm install

# 安装后端依赖
cd ../backend
npm install
```

### 启动开发服务器

```bash
# 终端1: 启动Maze服务器
cd E:\PythonProject\Maze
uvicorn maze.core.server:app --port 8000

# 终端2: 启动后端
cd web/maze_playground/v2/backend
npm run dev

# 终端3: 启动前端
cd web/maze_playground/v2/frontend
npm run dev
```

访问: http://localhost:5173

## 🎯 核心功能

- ✅ 可视化工作流设计器
- ✅ 内置Task/Tool节点
- ✅ 自定义函数解析
- ✅ 智能参数配置（支持任务输出引用）
- ✅ 实时运行结果
- ✅ 代码编辑器

