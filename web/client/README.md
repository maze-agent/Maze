# Maze Client SDK

简洁易用的 Maze 工作流客户端 SDK。

## 快速开始

### 基本使用

```python
from maze.core.client import MaClient

# 1. 创建客户端
client = MaClient("http://localhost:8000")

# 2. 创建工作流
workflow = client.create_workflow()

# 3. 添加任务
task1 = workflow.add_task(task_type="code", task_name="任务1")
task2 = workflow.add_task(task_type="code", task_name="任务2")

# 4. 配置任务
task1.save(
    code_str="""
def task1(params):
    input_value = params.get("input_key")
    return {"output_key": input_value + " processed"}
    """,
    task_input={
        "input_params": {
            "1": {
                "key": "input_key",
                "input_schema": "from_user",
                "data_type": "str",
                "value": "hello"
            }
        }
    },
    task_output={
        "output_params": {
            "1": {
                "key": "output_key",
                "data_type": "str"
            }
        }
    },
    resources={
        "cpu": 1,
        "cpu_mem": 128,
        "gpu": 0,
        "gpu_mem": 0
    }
)

# 5. 添加任务依赖
workflow.add_edge(task1, task2)

# 6. 运行工作流
workflow.run()

# 7. 获取结果
for message in workflow.get_results():
    print(message)
```

## API 文档

### MaClient

客户端类，用于连接 Maze 服务器。

```python
client = MaClient(server_url="http://localhost:8000")
```

**方法:**

- `create_workflow()` - 创建新工作流，返回 `MaWorkflow` 对象
- `get_workflow(workflow_id)` - 获取已存在的工作流对象
- `get_ray_head_port()` - 获取 Ray 头节点端口

### MaWorkflow

工作流类，用于管理任务和执行流程。

**方法:**

- `add_task(task_type="code", task_name=None)` - 添加任务，返回 `MaTask` 对象
- `get_tasks()` - 获取所有任务列表
- `add_edge(source_task, target_task)` - 添加任务依赖边
- `del_edge(source_task, target_task)` - 删除任务依赖边
- `run()` - 运行工作流
- `get_results(verbose=True)` - 通过 WebSocket 获取执行结果（生成器）

### MaTask

任务类，用于配置单个任务。

**属性:**

- `task_id` - 任务ID
- `workflow_id` - 所属工作流ID
- `task_name` - 任务名称

**方法:**

- `save(code_str, task_input, task_output, resources)` - 保存任务配置
- `delete()` - 删除任务

## 任务配置说明

### code_str

任务的 Python 代码字符串，必须包含一个函数定义，函数接受 `params` 参数并返回字典。

```python
code_str = """
def my_task(params):
    # 获取输入参数
    input_value = params.get("input_key")
    
    # 处理逻辑
    result = input_value + " processed"
    
    # 返回输出
    return {"output_key": result}
"""
```

### task_input

任务输入参数配置：

```python
task_input = {
    "input_params": {
        "1": {  # 参数序号
            "key": "input_key",           # 参数名
            "input_schema": "from_user",   # 输入模式: from_user 或 from_task
            "data_type": "str",            # 数据类型
            "value": "input_value"         # 参数值或引用
        }
    }
}
```

**input_schema 说明:**
- `from_user` - 从用户直接输入，value 是实际值
- `from_task` - 从其他任务输出获取，value 格式为 `{task_id}.output.{output_key}`

### task_output

任务输出参数配置：

```python
task_output = {
    "output_params": {
        "1": {  # 参数序号
            "key": "output_key",     # 输出参数名
            "data_type": "str"       # 数据类型
        }
    }
}
```

### resources

任务资源需求：

```python
resources = {
    "cpu": 1,        # CPU 核心数
    "cpu_mem": 128,  # CPU 内存 (MB)
    "gpu": 0,        # GPU 数量
    "gpu_mem": 0     # GPU 内存 (MB)
}
```

## 完整示例

查看 `test/client_example.py` 获取完整的使用示例。

## 对比原始 API

**原始方式** (test/api_test.py):
```python
workflow_id = create_workflow()
task_id1 = add_task(workflow_id)
save_task(workflow_id, task_id1, code_str, task_input, task_output, resources)
add_edge(workflow_id, task_id1, task_id2)
run_workflow(workflow_id)
get_workflow_status(workflow_id)
```

**新方式** (使用 MaClient):
```python
client = MaClient()
workflow = client.create_workflow()
task1 = workflow.add_task()
task1.save(code_str, task_input, task_output, resources)
workflow.add_edge(task1, task2)
workflow.run()
for msg in workflow.get_results():
    print(msg)
```

更加简洁、类型安全、易于使用！

