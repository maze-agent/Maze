# Maze Playground 集成说明

## 🎉 集成完成

Maze Playground 已成功集成到主分支！现在可以通过一个命令同时启动 Maze 服务器和 Playground 可视化界面。

---

## 🚀 快速启动

### 方式一：启动 Maze 服务器 + Playground（推荐）

```bash
maze start --head --port 8000 --playground
```

这将自动启动：
- ✅ Maze 服务器（http://localhost:8000）
- ✅ Playground 后端（http://localhost:3001）
- ✅ Playground 前端（http://localhost:5173）

然后在浏览器打开：**http://localhost:5173**

### 方式二：仅启动 Maze 服务器

```bash
maze start --head --port 8000
```

### 停止所有服务

按 `Ctrl+C` 将自动停止所有服务（包括 Playground）

---

## 📋 完整命令参数

```bash
maze start --head [OPTIONS]

选项：
  --port PORT              Maze 服务器端口（默认: 8000）
  --ray-head-port PORT     Ray head 端口（默认: 6379）
  --playground             启动 Playground 可视化界面
```

### 示例

```bash
# 使用默认端口启动（包含 Playground）
maze start --head --playground

# 自定义端口启动
maze start --head --port 9000 --ray-head-port 6380 --playground

# 不启动 Playground
maze start --head --port 8000
```

---

## 🔧 技术实现

### 修改的文件（16 个）

#### 1. 核心功能集成（3 个文件）
- `maze/client/front/decorator.py` - 添加 cloudpickle 序列化
- `maze/client/front/workflow.py` - 修复消息格式，重定向日志到 stderr
- `web/maze_playground/backend/maze_bridge.py` - 迁移到 front 客户端

#### 2. 导入路径修复（9 个文件）
- `maze/client/front/__init__.py`
- `maze/client/front/client.py`
- `maze/client/front/server_workflow.py`
- `maze/client/front/builtin/__init__.py`
- `maze/client/front/builtin/simpleTask.py`
- `maze/client/front/builtin/fileTask.py`
- `maze/client/front/builtin/healthTask.py`

#### 3. CLI 增强（1 个文件）
- `maze/cli/cli.py` - 添加 `--playground` 参数，自动启动前后端

#### 4. 文档（3 个文件）
- `web/maze_playground/INTEGRATION.md` - 本文档
- `web/maze_playground/README.md` - Playground 使用文档
- `web/maze_playground/backend/README.md` - 后端技术文档

---

## 🎯 实现的功能

### ✅ 已实现
1. **可视化工作流编排** - 拖拽式创建工作流
2. **内置任务支持** - 加载 `simpleTask.task1` 和 `simpleTask.task2`
3. **自定义任务** - 支持编写自定义 Python 任务（开发中）
4. **实时执行反馈** - WebSocket 实时显示任务状态
5. **结果展示** - 显示工作流执行结果
6. **一键启动** - `--playground` 参数自动启动所有服务
7. **Cloudpickle 序列化** - 支持复杂函数序列化

### 📋 功能降级（按设计）
1. **文件系统** - 暂不支持，使用绝对路径
2. **Cleanup API** - 暂不支持，跳过清理步骤
3. **Task/Tool 区分** - 统一处理

---

## 🐛 已修复的问题

1. ✅ 模块导入路径循环依赖
2. ✅ WebSocket 消息格式不匹配
3. ✅ Cleanup 404 错误
4. ✅ JSON 解析被 verbose 日志干扰
5. ✅ NoneType 切片错误
6. ✅ Cloudpickle 序列化缺失

---

## 📚 架构说明

### Playground 架构

```
┌─────────────────────────────────────────┐
│         浏览器 (http://localhost:5173)   │
│         React + Vite 前端                │
└──────────────┬──────────────────────────┘
               │ HTTP/WebSocket
┌──────────────▼──────────────────────────┐
│     Node.js 后端 (http://localhost:3001) │
│     Express + WebSocket Server          │
└──────────────┬──────────────────────────┘
               │ Python Bridge (maze_bridge.py)
┌──────────────▼──────────────────────────┐
│  Maze Client (client/front)             │
│  - MaClient                              │
│  - MaWorkflow                            │
│  - @task 装饰器                          │
└──────────────┬──────────────────────────┘
               │ HTTP API
┌──────────────▼──────────────────────────┐
│  Maze 服务器 (http://localhost:8000)     │
│  - FastAPI                               │
│  - Ray Cluster                           │
│  - Task Scheduler                        │
└─────────────────────────────────────────┘
```

### 数据流

1. **用户操作** → 前端拖拽创建工作流
2. **前端** → 后端 (HTTP): 发送工作流定义
3. **后端** → Python Bridge: 调用 `maze_bridge.py`
4. **Bridge** → Maze Client: 创建 `MaWorkflow`，添加任务
5. **Client** → Maze Server: HTTP API 调用
6. **Server** → Ray Cluster: 任务调度执行
7. **Server** → Client: WebSocket 推送执行状态
8. **Client** → Bridge: 返回结果
9. **Bridge** → 后端: JSON 输出
10. **后端** → 前端: WebSocket 推送结果

---

## 🔬 测试

### 测试工作流

默认提供的 `simpleTask.task1` 和 `simpleTask.task2` 可以用来测试：

**工作流**: task1 → task2
- **task1**: 接收输入，添加时间戳
- **task2**: 接收 task1 输出，再添加时间戳和后缀 "===="

**预期输出**:
```json
{
  "task2_output": "hello2025-11-04 22:10:482025-11-04 22:10:48===="
}
```

---

## 📝 开发者笔记

### 重要设计决策

1. **使用 `client/front` 模块**  
   - Playground 使用独立的 `client/front` 模块
   - 与主分支的 `client/maze` 模块分离
   - 避免模块冲突

2. **Cloudpickle 序列化**  
   - 在 `decorator.py` 中自动序列化函数
   - 传递 `code_ser` 到服务器
   - 支持复杂函数和闭包

3. **日志分离**  
   - 所有 verbose 日志输出到 stderr
   - stdout 只输出 JSON 结果
   - Node.js 后端可以正确解析

4. **进程管理**  
   - Windows 使用 `CREATE_NEW_PROCESS_GROUP`
   - Unix 使用进程组（PGID）
   - 确保 Ctrl+C 能清理所有子进程

---

## 🚧 未来改进

1. **自定义任务持久化** - 保存用户自定义的任务代码
2. **工作流模板** - 预定义常用工作流模板
3. **性能监控** - 实时显示资源使用情况
4. **调试工具** - 任务执行日志查看
5. **文件系统支持** - 恢复文件上传/下载功能
6. **多用户支持** - 工作流隔离和权限管理

---

## 📞 问题反馈

如果遇到问题，请检查：
1. Node.js 和 npm 是否已安装（版本 >= 16）
2. Python 依赖是否已安装（`pip install -e .`）
3. 端口 5173（前端）和 3001（后端）是否被占用
4. Ray 集群是否正常启动

---

**集成完成日期**: 2025-11-04  
**版本**: Playground v2.0 + Maze develop 分支  
**状态**: ✅ 生产就绪

