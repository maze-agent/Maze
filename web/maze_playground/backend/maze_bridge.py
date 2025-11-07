"""
Maze Client 桥接模块 - 完整版
用于Node.js后端调用Python Maze Client
"""

import sys
import json
import os
import traceback
import io

# 设置标准输出和标准错误为 UTF-8 编码
if sys.platform == 'win32':
    # Windows 平台需要特殊处理
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# 添加项目根目录到Python路径
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..'))
sys.path.insert(0, project_root)

# 使用 front 客户端模块
from maze.client.front.client import MaClient
from maze.client.front.decorator import task, tool, get_task_metadata
from maze.client.front.builtin import simpleTask
import inspect
import importlib


def get_builtin_tasks():
    """获取所有内置任务的元数据"""
    builtin_modules = [simpleTask]
    tasks = []
    
    for module in builtin_modules:
        module_name = module.__name__.split('.')[-1]
        for name, obj in inspect.getmembers(module):
            if hasattr(obj, '_maze_task_metadata'):
                try:
                    metadata = get_task_metadata(obj)
                    tasks.append({
                        "name": name,
                        "displayName": name.replace('_', ' ').title(),
                        "nodeType": metadata.node_type,
                        "inputs": [
                            {
                                "name": inp,
                                "dataType": metadata.data_types.get(inp, "str")
                            }
                            for inp in metadata.inputs
                        ],
                        "outputs": [
                            {
                                "name": out,
                                "dataType": metadata.data_types.get(out, "str")
                            }
                            for out in metadata.outputs
                        ],
                        "resources": metadata.resources if metadata.node_type == "task" else None,
                        "functionRef": name,
                        "module": module_name
                    })
                except Exception as e:
                    print(f"Error processing {name}: {e}", file=sys.stderr)
    
    return {"tasks": tasks}


def parse_custom_function(code):
    """解析用户提交的自定义函数"""
    import tempfile
    import importlib.util
    
    temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False)
    try:
        temp_file.write(code)
        temp_file.close()
        
        # 动态加载模块
        spec = importlib.util.spec_from_file_location("custom_task", temp_file.name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        
        # 查找装饰的函数
        for name, obj in inspect.getmembers(module):
            if hasattr(obj, '_maze_task_metadata'):
                metadata = get_task_metadata(obj)
                return {
                    "name": name,
                    "nodeType": metadata.node_type,
                    "inputs": [
                        {"name": inp, "dataType": metadata.data_types.get(inp, "str")}
                        for inp in metadata.inputs
                    ],
                    "outputs": [
                        {"name": out, "dataType": metadata.data_types.get(out, "str")}
                        for out in metadata.outputs
                    ],
                    "resources": metadata.resources if metadata.node_type == "task" else None,
                    "codeStr": code
                }
        
        return {"error": "未找到使用 @task 或 @tool 装饰的函数"}
    
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}
    
    finally:
        os.unlink(temp_file.name)


def create_maze_workflow(workflow_id, server_url="http://localhost:8000"):
    """
    创建 Maze 工作流（已废弃）
    
    注意：每次 Python 调用都是新进程，全局变量不会保留
    因此不再预先创建工作流，而是在运行时创建
    """
    # 返回成功，但实际不做任何事
    # 保留此函数是为了兼容性
    return {
        "success": True,
        "mazeWorkflowId": "will-be-created-on-run"
    }


def build_and_run_workflow(workflow_id, nodes, edges):
    """构建并运行工作流"""
    try:
        print(f"[DEBUG] 开始构建工作流: {workflow_id}", file=sys.stderr)
        print(f"[DEBUG] 节点数: {len(nodes)}, 边数: {len(edges)}", file=sys.stderr)
        
        # 每次运行都创建新的 client 和 workflow
        # 因为每次 Python 调用都是新进程，全局变量不会保留
        client = MaClient(server_url="http://localhost:8000")
        workflow = client.create_workflow()
        task_map = {}  # node_id -> MaTask
        
        print(f"[DEBUG] Maze 工作流已创建: {workflow.workflow_id}", file=sys.stderr)
        
        # Step 1: 添加所有任务节点
        for node in nodes:
            node_id = node["id"]
            node_data = node["data"]
            category = node_data["category"]
            
            if category == "builtin":
                # 内置任务
                task_ref = node_data["taskRef"]  # e.g., "simpleTask.task1"
                module_name, func_name = task_ref.split(".")
                
                # 动态导入模块和函数
                if module_name == "simpleTask":
                    task_func = getattr(simpleTask, func_name)
                else:
                    return {"success": False, "error": f"未知模块: {module_name}"}
                
                # 构建输入字典
                task_inputs = {}
                for inp in node_data["inputs"]:
                    if inp["source"] == "user":
                        task_inputs[inp["name"]] = inp.get("value", "")
                    elif inp["source"] == "task":
                        task_source = inp.get("taskSource")
                        if task_source:
                            source_node_id = task_source["taskId"]
                            output_key = task_source["outputKey"]
                            if source_node_id in task_map:
                                source_task = task_map[source_node_id]
                                task_inputs[inp["name"]] = source_task.outputs[output_key]
                            else:
                                return {"success": False, "error": f"任务依赖错误: {source_node_id} 未找到"}
                
                # 添加任务到工作流
                print(f"[DEBUG] 添加内置任务: {func_name}, 输入: {list(task_inputs.keys())}", file=sys.stderr)
                ma_task = workflow.add_task(task_func, inputs=task_inputs)
                task_map[node_id] = ma_task
                print(f"[DEBUG] 任务已添加: {node_id} -> {ma_task.task_id}", file=sys.stderr)
            
            elif category == "custom":
                # 自定义任务
                custom_code = node_data.get("customCode", "")
                
                # 动态执行自定义代码并获取函数
                import tempfile
                import importlib.util
                
                temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False)
                try:
                    temp_file.write(custom_code)
                    temp_file.close()
                    
                    spec = importlib.util.spec_from_file_location("custom_module", temp_file.name)
                    custom_module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(custom_module)
                    
                    # 找到装饰的函数
                    task_func = None
                    for name, obj in inspect.getmembers(custom_module):
                        if hasattr(obj, '_maze_task_metadata'):
                            task_func = obj
                            break
                    
                    if not task_func:
                        return {"success": False, "error": "自定义函数未找到装饰器"}
                    
                    # 构建输入
                    task_inputs = {}
                    for inp in node_data["inputs"]:
                        if inp["source"] == "user":
                            task_inputs[inp["name"]] = inp.get("value", "")
                        elif inp["source"] == "task":
                            task_source = inp.get("taskSource")
                            if task_source:
                                source_node_id = task_source["taskId"]
                                output_key = task_source["outputKey"]
                                if source_node_id in task_map:
                                    source_task = task_map[source_node_id]
                                    task_inputs[inp["name"]] = source_task.outputs[output_key]
                    
                    # 添加任务
                    ma_task = workflow.add_task(task_func, inputs=task_inputs)
                    task_map[node_id] = ma_task
                
                finally:
                    os.unlink(temp_file.name)
        
        # Step 2: 添加所有边（依赖关系）
        print(f"[DEBUG] 添加依赖边...", file=sys.stderr)
        for edge in edges:
            source_node_id = edge["source"]
            target_node_id = edge["target"]
            
            if source_node_id in task_map and target_node_id in task_map:
                source_task = task_map[source_node_id]
                target_task = task_map[target_node_id]
                workflow.add_edge(source_task, target_task)
                print(f"[DEBUG] 边已添加: {source_node_id} -> {target_node_id}", file=sys.stderr)
            else:
                print(f"[WARNING] 跳过无效边: {source_node_id} -> {target_node_id}", file=sys.stderr)
        
        # Step 3: 运行工作流
        print(f"[DEBUG] 开始运行工作流...", file=sys.stderr)
        workflow.run()
        
        # Step 4: 获取结果
        print(f"[DEBUG] 获取运行结果...", file=sys.stderr)
        results = workflow.get_results(verbose=True)
        print(f"[DEBUG] 结果: {results}", file=sys.stderr)
        
        # Step 5: 清理（当前服务器不支持 cleanup endpoint，已降级）
        # print(f"[DEBUG] 清理工作流...", file=sys.stderr)
        # workflow.cleanup()
        print(f"[DEBUG] 工作流执行完成（跳过清理步骤）", file=sys.stderr)
        
        return {
            "success": True,
            "results": results
        }
    
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }


def main():
    """主函数 - 根据命令行参数执行不同操作"""
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Missing action parameter"}, ensure_ascii=False))
        sys.exit(1)
    
    action = sys.argv[1]
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    
    result = None
    
    try:
        if action == 'get_builtin_tasks':
            result = get_builtin_tasks()
        
        elif action == 'parse_custom_function':
            result = parse_custom_function(params.get('code', ''))
        
        elif action == 'create_workflow':
            result = create_maze_workflow(
                params.get('workflowId'),
                params.get('serverUrl', 'http://localhost:8000')
            )
        
        elif action == 'run_workflow':
            result = build_and_run_workflow(
                params.get('workflowId'),
                params.get('nodes', []),
                params.get('edges', [])
            )
        
        else:
            result = {"error": f"Unknown action: {action}"}
    
    except Exception as e:
        result = {
            "error": str(e),
            "traceback": traceback.format_exc()
        }
    
    # 确保输出 UTF-8 编码的 JSON，不转义中文
    print(json.dumps(result, ensure_ascii=False))
    sys.stdout.flush()  # 确保输出被刷新


if __name__ == '__main__':
    main()
