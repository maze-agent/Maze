"""
Maze Client 桥接模块 - 完整版
用于Node.js后端调用Python Maze Client
"""

import sys
import json
import os
import traceback
import io
import re
import tempfile

sys.dont_write_bytecode = True

# 设置标准输出和标准错误为 UTF-8 编码
if sys.platform == 'win32':
    # Windows 平台需要特殊处理
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# 添加项目根目录到Python路径
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..'))
sys.path.insert(0, project_root)

workspace_root = os.path.abspath(os.path.expanduser(
    os.environ.get("MAZE_WORKSPACE_ROOT_DIR")
    or os.environ.get("MAZE_WORKSPACE_DIR")
    or os.path.join(project_root, "workspace")
))
workspaces_dir = os.path.abspath(os.path.expanduser(
    os.environ.get("MAZE_WORKSPACES_DIR")
    or os.path.join(workspace_root, "workspaces")
))
default_workspace_dir = os.path.abspath(os.path.expanduser(
    os.environ.get("MAZE_DEFAULT_WORKSPACE_DIR")
    or os.path.join(workspaces_dir, os.environ.get("MAZE_DEFAULT_WORKSPACE_ID", "default"))
))

# 使用客户端模块
from maze.client.front.client import MaClient
from maze.client.maze.client import MaClient as DynamicMaClient
from maze import task, get_task_metadata
from maze.client.front.builtin import distributedSmoke
import inspect
import importlib
import importlib.util
import hashlib

def get_builtin_tasks():
    """获取所有内置任务的元数据"""
    builtin_modules = [distributedSmoke]
    tasks = []
    
    for module in builtin_modules:
        module_name = module.__name__.split('.')[-1]
        for name, obj in inspect.getmembers(module):
            if hasattr(obj, '_maze_task_metadata'):
                try:
                    metadata = get_task_metadata(obj)
                    description = inspect.getdoc(obj) or ""
                    if not description:
                        input_names = ", ".join(metadata.inputs) or "none"
                        output_names = ", ".join(metadata.outputs) or "none"
                        description = f"Inputs: {input_names}. Outputs: {output_names}."

                    tasks.append({
                        "name": name,
                        "displayName": name.replace('_', ' ').title(),
                        "description": description,
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
                        "resources": metadata.resources,
                        "functionRef": name,
                        "module": module_name
                    })
                except Exception as e:
                    print(f"Error processing {name}: {e}", file=sys.stderr)
    
    return {"tasks": tasks}


def emit_progress(event):
    """Emit structured progress to the Node.js process via stderr."""
    try:
        print(
            "__MAZE_PROGRESS__" + json.dumps(event, ensure_ascii=False),
            file=sys.stderr,
            flush=True,
        )
    except Exception as e:
        print(f"[WARNING] Failed to emit progress: {e}", file=sys.stderr)


def parse_custom_function(code):
    """Parse user-submitted custom function"""
    import tempfile
    
    temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8')
    try:
        temp_file.write(code)
        temp_file.close()
        
        # Dynamically load module
        spec = importlib.util.spec_from_file_location("custom_task", temp_file.name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        
        # Find decorated function
        for name, obj in inspect.getmembers(module):
            if hasattr(obj, '_maze_task_metadata'):
                metadata = obj._maze_task_metadata
                
                return {
                    "name": name,
                    "inputs": [
                        {"name": inp, "dataType": metadata.data_types.get(inp, "str")}
                        for inp in metadata.inputs
                    ],
                    "outputs": [
                        {"name": out, "dataType": metadata.data_types.get(out, "str")}
                        for out in metadata.outputs
                    ],
                    "resources": metadata.resources,
                    "codeStr": code
                }
        
        return {"error": "No function decorated with @task found"}
    
    except SyntaxError as e:
        return {"error": f"Syntax error: {e}", "traceback": traceback.format_exc()}
    
    except ImportError as e:
        return {"error": f"Import failed: {e}. Please use 'from maze import task'", "traceback": traceback.format_exc()}
    
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}
    
    finally:
        try:
            os.unlink(temp_file.name)
        except:
            pass


def _task_description(func, metadata):
    """Build a compact task description for the UI."""
    description = inspect.getdoc(func) or ""
    if description:
        return description

    input_names = ", ".join(metadata.inputs) or "none"
    output_names = ", ".join(metadata.outputs) or "none"
    return f"Inputs: {input_names}. Outputs: {output_names}."


def _task_metadata_payload(func, name, code, workspace_dir=None, relative_path=None):
    """Convert a decorated Maze task function to frontend metadata."""
    metadata = get_task_metadata(func)
    payload = {
        "name": name,
        "displayName": name.replace("_", " ").title(),
        "description": _task_description(func, metadata),
        "inputs": [
            {"name": inp, "dataType": metadata.data_types.get(inp, "str")}
            for inp in metadata.inputs
        ],
        "outputs": [
            {"name": out, "dataType": metadata.data_types.get(out, "str")}
            for out in metadata.outputs
        ],
        "resources": metadata.resources,
        "functionName": name,
        "code": code,
    }

    if workspace_dir is not None:
        payload["workspaceDir"] = workspace_dir
    if relative_path is not None:
        payload["relativePath"] = relative_path

    return payload


def _load_module_from_file(file_path, workspace_dir):
    """Load a Python file as an isolated module while allowing local imports."""
    module_hash = hashlib.sha1(file_path.encode("utf-8")).hexdigest()[:12]
    module_name = f"maze_workspace_task_{module_hash}"
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {file_path}")

    module = importlib.util.module_from_spec(spec)
    original_sys_path = list(sys.path)
    try:
        sys.path.insert(0, workspace_dir)
        sys.path.insert(0, os.path.dirname(file_path))
        spec.loader.exec_module(module)
    finally:
        sys.path = original_sys_path

    return module


def _is_windows_drive_path(value):
    return bool(re.match(r"^[a-zA-Z]:[\\/]", str(value or "").strip()))


def _workspace_id_to_dir(workspace_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(workspace_id or "").strip()).strip("-")
    safe = safe[:80] or "default"
    return os.path.join(workspaces_dir, safe)


def _ensure_workspace_layout(resolved):
    os.makedirs(resolved, exist_ok=True)
    for name in ("files", "workflows", "tasks", "policies", "runs"):
        os.makedirs(os.path.join(resolved, name), exist_ok=True)

    manifest_path = os.path.join(resolved, "workspace.json")
    if not os.path.exists(manifest_path):
        workspace_id = os.path.basename(resolved.rstrip(os.sep)) or "default"
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump({
                "schema": "maze_workspace",
                "schema_version": 1,
                "manifest_version": 1,
                "workspace_id": workspace_id,
                "name": "Default workspace" if workspace_id == "default" else "Untitled workspace",
                "created_at": now,
                "updated_at": now,
                "mode": "session",
                "default_sandbox": "workspace_sandbox",
                "files_dir": "files",
                "workflows_dir": "workflows",
                "tasks_dir": "tasks",
                "runs_dir": "runs",
                "policy_path": "policies/sandbox_policy.json",
                "imports": [],
                "local_mounts": [],
            }, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

    policy_path = os.path.join(resolved, "policies", "sandbox_policy.json")
    if not os.path.exists(policy_path):
        with open(policy_path, "w", encoding="utf-8") as handle:
            json.dump({
                "schema": "maze_sandbox_policy",
                "schema_version": 1,
                "permission": {
                    "read": {
                        "*": "allow",
                        ".env": "deny",
                        ".env.*": "deny",
                        "*secret*": "deny",
                        "*credential*": "deny",
                        "*token*": "deny",
                        "api_key*": "deny",
                    },
                    "write": {
                        "*": "ask",
                        ".env": "deny",
                        ".env.*": "deny",
                        "*secret*": "deny",
                        "*credential*": "deny",
                        "*token*": "deny",
                        "api_key*": "deny",
                    },
                    "exec_code": {"*": "ask", "python *": "allow", "rm *": "deny"},
                    "mcp": {"*": "ask"},
                    "skill": {"*": "allow"},
                },
            }, handle, ensure_ascii=False, indent=2)
            handle.write("\n")


def _resolve_workspace_dir(workspace_dir=None, workspace_id=None):
    raw = str(workspace_dir or "").strip()
    if workspace_id and not raw:
        raw = _workspace_id_to_dir(workspace_id)
    if not raw:
        raw = default_workspace_dir

    if sys.platform != "win32":
        if _is_windows_drive_path(raw):
            return None, {"error": "Windows drive paths cannot be used as service-side workspace paths"}
        if "\\" in raw:
            return None, {"error": "Workspace paths must use POSIX-style separators on this service"}

    if raw and not os.path.isabs(os.path.expanduser(raw)) and "/" not in raw and "\\" not in raw:
        raw = _workspace_id_to_dir(raw)

    resolved = os.path.abspath(os.path.expanduser(raw))
    if resolved == project_root:
        return None, {"error": f"Project root cannot be used as a workspace directory: {project_root}"}

    try:
        _ensure_workspace_layout(resolved)
    except Exception as exc:
        return None, {"error": f"Failed to initialize workspace directory {resolved}: {exc}"}

    if not os.path.isdir(resolved):
        return None, {"error": f"Workspace directory does not exist: {resolved}"}

    return resolved, None


def _string_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _normalize_task_relative_path(relative_path):
    relative_path = (relative_path or "tasks/custom_task.py").replace("\\", "/").strip()
    relative_path = relative_path.lstrip("/")

    if not relative_path.startswith("tasks/"):
        relative_path = f"tasks/{relative_path}"

    normalized = os.path.normpath(relative_path).replace("\\", "/")
    if normalized == "tasks" or not normalized.endswith(".py"):
        normalized = f"{normalized}.py"

    if normalized.startswith("../") or "/../" in normalized or normalized == "..":
        raise ValueError("Task path must stay inside the workspace tasks directory")

    return normalized


def _task_file_path(workspace_dir, relative_path):
    normalized = _normalize_task_relative_path(relative_path)
    full_path = os.path.abspath(os.path.join(workspace_dir, normalized))
    tasks_dir = os.path.abspath(os.path.join(workspace_dir, "tasks"))

    if not (full_path == tasks_dir or full_path.startswith(tasks_dir + os.sep)):
        raise ValueError("Task path must stay inside the workspace tasks directory")

    return normalized, full_path


def _extract_tasks_from_file(file_path, workspace_dir, relative_path):
    with open(file_path, "r", encoding="utf-8") as f:
        code = f.read()

    module = _load_module_from_file(file_path, workspace_dir)
    tasks = []
    for name, obj in inspect.getmembers(module):
        if hasattr(obj, "_maze_task_metadata"):
            tasks.append(_task_metadata_payload(
                obj,
                name,
                code,
                workspace_dir=workspace_dir,
                relative_path=relative_path,
            ))

    return tasks


def _extract_single_task_from_file(file_path, workspace_dir, relative_path):
    tasks = _extract_tasks_from_file(file_path, workspace_dir, relative_path)
    if len(tasks) > 1:
        task_names = ", ".join(task["functionName"] for task in tasks)
        raise ValueError(
            f"Workspace task files must define exactly one @task function. "
            f"{relative_path} defines {len(tasks)} tasks: {task_names}"
        )

    return tasks


def get_workspace_tasks(workspace_dir):
    """Scan <workspace>/tasks/**/*.py and return decorated Maze tasks."""
    workspace_dir, error = _resolve_workspace_dir(workspace_dir)
    if error:
        return error

    tasks_dir = os.path.join(workspace_dir, "tasks")
    tasks = []
    errors = []

    if not os.path.isdir(tasks_dir):
        return {
            "workspaceDir": workspace_dir,
            "tasksDir": tasks_dir,
            "tasks": [],
            "errors": [],
        }

    for root, _, files in os.walk(tasks_dir):
        for file_name in files:
            if not file_name.endswith(".py") or file_name.startswith("__"):
                continue

            file_path = os.path.join(root, file_name)
            relative_path = os.path.relpath(file_path, workspace_dir).replace("\\", "/")
            try:
                tasks.extend(_extract_single_task_from_file(file_path, workspace_dir, relative_path))
            except Exception as e:
                errors.append({
                    "relativePath": relative_path,
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                })

    return {
        "workspaceDir": workspace_dir,
        "tasksDir": tasks_dir,
        "tasks": tasks,
        "errors": errors,
    }


def save_workspace_task(workspace_dir, relative_path, code, parse=True):
    """Save a workspace task file, optionally parsing it afterwards."""
    workspace_dir, error = _resolve_workspace_dir(workspace_dir)
    if error:
        return error

    if code is None:
        return {"error": "Task code cannot be empty"}

    if parse and not code.strip():
        return {"error": "Task code cannot be empty"}

    try:
        relative_path, file_path = _task_file_path(workspace_dir, relative_path)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        response = {
            "success": True,
            "workspaceDir": workspace_dir,
            "tasksDir": os.path.join(workspace_dir, "tasks"),
            "relativePath": relative_path,
        }

        if parse:
            tasks = _extract_single_task_from_file(file_path, workspace_dir, relative_path)
            if not tasks:
                return {
                    **response,
                    "success": False,
                    "error": "No function decorated with @task found",
                }
            response["tasks"] = tasks
            response["task"] = tasks[0]

        return response
    except SyntaxError as e:
        return {"error": f"Syntax error: {e}", "traceback": traceback.format_exc()}
    except ImportError as e:
        return {"error": f"Import failed: {e}", "traceback": traceback.format_exc()}
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


def delete_workspace_task(workspace_dir, relative_path):
    """Delete a task file from <workspace>/tasks."""
    workspace_dir, error = _resolve_workspace_dir(workspace_dir)
    if error:
        return error

    try:
        relative_path, file_path = _task_file_path(workspace_dir, relative_path)

        if not os.path.isfile(file_path):
            return {"error": f"Workspace task file not found: {relative_path}"}

        os.unlink(file_path)
        return {
            "success": True,
            "workspaceDir": workspace_dir,
            "relativePath": relative_path,
        }
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


def _normalize_python_identifier(name):
    raw_name = (name or "").strip()
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", raw_name):
        return raw_name

    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", raw_name).strip("_").lower()
    if not normalized:
        raise ValueError("Task name cannot be empty")
    if normalized[0].isdigit():
        normalized = f"task_{normalized}"
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", normalized):
        raise ValueError("Task name must be a valid Python identifier")

    return normalized


def rename_workspace_task(workspace_dir, relative_path, old_function_name, new_name):
    """Rename a decorated task function inside a workspace task file."""
    workspace_dir, error = _resolve_workspace_dir(workspace_dir)
    if error:
        return error

    try:
        new_function_name = _normalize_python_identifier(new_name)
        relative_path, file_path = _task_file_path(workspace_dir, relative_path)

        if not os.path.isfile(file_path):
            return {"error": f"Workspace task file not found: {relative_path}"}

        with open(file_path, "r", encoding="utf-8") as f:
            code = f.read()

        pattern = re.compile(
            r"(\b(?:async\s+def|def)\s+)" + re.escape(old_function_name) + r"(\s*\()"
        )
        code, count = pattern.subn(r"\1" + new_function_name + r"\2", code, count=1)

        if count == 0:
            return {"error": f"Task function not found: {old_function_name}"}

        result = save_workspace_task(workspace_dir, relative_path, code, parse=True)
        if result.get("error") or result.get("success") is False:
            return result

        result["oldFunctionName"] = old_function_name
        result["newFunctionName"] = new_function_name
        return result
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


def _build_task_inputs(node_data, task_map):
    task_inputs = {}
    for inp in node_data.get("inputs", []):
        source = inp.get("source")
        if source == "user":
            task_inputs[inp["name"]] = inp.get("value", "")
        elif source in ("task", "node"):
            task_source = inp.get("taskSource") or {}
            source_node_id = (
                task_source.get("taskId")
                or task_source.get("nodeId")
                or inp.get("taskId")
                or inp.get("nodeId")
            )
            output_key = (
                task_source.get("outputKey")
                or task_source.get("outputName")
                or inp.get("outputKey")
                or inp.get("outputName")
            )
            if source_node_id and output_key:
                if source_node_id in task_map:
                    source_task = task_map[source_node_id]
                    task_inputs[inp["name"]] = source_task.outputs[output_key]
                else:
                    raise ValueError(f"Task dependency error: {source_node_id} not found")

    return task_inputs


def _load_workspace_task_func(workspace_dir, relative_path, function_name):
    workspace_dir, error = _resolve_workspace_dir(workspace_dir)
    if error:
        raise ValueError(error["error"])

    relative_path, file_path = _task_file_path(workspace_dir, relative_path)
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Workspace task file not found: {relative_path}")

    module = _load_module_from_file(file_path, workspace_dir)
    task_func = getattr(module, function_name, None)
    if task_func is None:
        raise AttributeError(f"Workspace task function not found: {function_name}")
    if not hasattr(task_func, "_maze_task_metadata"):
        raise ValueError(f"Workspace function is not decorated with @task: {function_name}")

    return task_func


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


def build_and_run_workflow(
    workflow_id,
    nodes,
    edges,
    static_run_id=None,
    workspace_dir=None,
    workspace_id=None,
    workspace_manifest_version=None,
):
    """构建并运行工作流"""
    try:
        workflow_workspace_dir, workspace_error = _resolve_workspace_dir(workspace_dir)
        if workspace_error:
            return {"success": False, "error": workspace_error["error"]}

        print(f"[DEBUG] 开始构建工作流: {workflow_id}", file=sys.stderr)
        print(f"[DEBUG] 节点数: {len(nodes)}, 边数: {len(edges)}", file=sys.stderr)
        
        # 每次运行都创建新的 client 和 workflow
        # 因为每次 Python 调用都是新进程，全局变量不会保留
        client = MaClient(server_url="http://localhost:8000")
        workflow = client.create_workflow()
        task_map = {}  # node_id -> MaTask
        maze_task_to_node = {}
        node_lookup = {node.get("id"): node for node in nodes}
        failed_progress = []

        def remember_task(node_id, ma_task):
            task_map[node_id] = ma_task
            maze_task_to_node[ma_task.task_id] = node_id

        def emit_workflow_progress(event):
            event_type = event.get("type")
            data = dict(event.get("data") or {})
            maze_task_id = data.get("task_id")
            node_id = maze_task_to_node.get(maze_task_id)
            if event_type == "task_exception" or data.get("run_status") == "failed":
                failed_progress.append({
                    "type": event_type,
                    "data": data,
                })

            if static_run_id:
                data["workflow_run_id"] = static_run_id
            if node_id:
                node = node_lookup.get(node_id) or {}
                node_data = node.get("data") or {}
                data["node_id"] = node_id
                data["node_label"] = node_data.get("label")
                data["maze_task_id"] = maze_task_id

            emit_progress({
                "type": event_type,
                "data": data,
            })
        
        print(f"[DEBUG] Maze 工作流已创建: {workflow.workflow_id}", file=sys.stderr)
        
        # Step 1: 添加所有任务节点
        for node in nodes:
            node_id = node["id"]
            node_data = node["data"]
            category = node_data["category"]
            
            if category == "builtin":
                # 内置任务
                task_ref = node_data["taskRef"]
                module_name, func_name = task_ref.split(".")
                
                # 动态导入模块和函数
                if module_name == "distributedSmoke":
                    task_func = getattr(distributedSmoke, func_name)
                else:
                    return {"success": False, "error": f"Unknown module: {module_name}"}
                
                try:
                    task_inputs = _build_task_inputs(node_data, task_map)
                except Exception as e:
                    return {
                        "success": False,
                        "error": str(e),
                        "traceback": traceback.format_exc(),
                    }

                # 添加任务到工作流
                print(f"[DEBUG] 添加内置任务: {func_name}, 输入: {list(task_inputs.keys())}", file=sys.stderr)
                ma_task = workflow.add_task(task_func, inputs=task_inputs)
                remember_task(node_id, ma_task)
                print(f"[DEBUG] 任务已添加: {node_id} -> {ma_task.task_id}", file=sys.stderr)

            elif category == "workspace":
                node_workspace_dir, workspace_error = _resolve_workspace_dir(
                    node_data.get("workspaceDir") or workflow_workspace_dir
                )
                if workspace_error:
                    return {"success": False, "error": workspace_error["error"]}
                task_path = node_data.get("taskPath") or node_data.get("relativePath")
                function_name = node_data.get("functionName") or node_data.get("label")

                if not task_path or not function_name:
                    return {
                        "success": False,
                        "error": f"Workspace task is missing file path or function name: {node_id}",
                    }

                try:
                    task_func = _load_workspace_task_func(node_workspace_dir, task_path, function_name)
                    task_inputs = _build_task_inputs(node_data, task_map)
                except Exception as e:
                    return {
                        "success": False,
                        "error": str(e),
                        "traceback": traceback.format_exc(),
                    }

                print(
                    f"[DEBUG] 添加工作区任务: {task_path}:{function_name}, 输入: {list(task_inputs.keys())}",
                    file=sys.stderr,
                )
                ma_task = workflow.add_task(task_func, inputs=task_inputs)
                remember_task(node_id, ma_task)
                print(f"[DEBUG] 工作区任务已添加: {node_id} -> {ma_task.task_id}", file=sys.stderr)
            
            elif category == "custom":
                # Custom task
                custom_code = node_data.get("customCode", "")
                
                # Dynamically execute custom code and get function
                import tempfile
                import importlib.util
                
                temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8')
                try:
                    temp_file.write(custom_code)
                    temp_file.close()
                    
                    spec = importlib.util.spec_from_file_location("custom_module", temp_file.name)
                    custom_module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(custom_module)
                    
                    # Find decorated function
                    task_func = None
                    for name, obj in inspect.getmembers(custom_module):
                        if hasattr(obj, '_maze_task_metadata'):
                            task_func = obj
                            break
                    
                    if not task_func:
                        return {"success": False, "error": "No @task decorated function found"}
                    
                    try:
                        task_inputs = _build_task_inputs(node_data, task_map)
                    except Exception as e:
                        return {
                            "success": False,
                            "error": str(e),
                            "traceback": traceback.format_exc(),
                        }
                    
                    # Add task
                    ma_task = workflow.add_task(task_func, inputs=task_inputs)
                    remember_task(node_id, ma_task)
                
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
        
        # Step 3: 运行工作流并获取 run_id
        print(f"[DEBUG] 开始运行工作流...", file=sys.stderr)
        file_context = None
        if static_run_id and workflow_workspace_dir:
            import socket

            core_url = os.environ.get("MAZE_CORE_URL")
            if not core_url:
                core_url = f"http://{socket.gethostbyname(socket.gethostname())}:8000"
            file_context = {
                "enabled": True,
                "workspace_dir": workflow_workspace_dir,
                "workspace_id": workspace_id or os.path.basename(workflow_workspace_dir.rstrip(os.sep)),
                "workspace_manifest_version": workspace_manifest_version,
                "run_id": static_run_id,
                "task_node_ids": maze_task_to_node,
                "artifact_store": {
                    "type": "head_http",
                    "base_url": core_url,
                },
            }
        run_id = workflow.run(file_context=file_context)
        print(f"[DEBUG] 工作流已提交，run_id: {run_id}", file=sys.stderr)
        
        # Step 4: 通过 run_id 获取结果
        print(f"[DEBUG] 获取运行结果 (run_id: {run_id})...", file=sys.stderr)
        results = workflow.get_results(run_id, verbose=True, progress_callback=emit_workflow_progress)
        print(f"[DEBUG] 结果: {results}", file=sys.stderr)
        if failed_progress:
            failure_data = failed_progress[-1].get("data") or {}
            failure = failure_data.get("error") or failure_data.get("result") or failure_data
            if isinstance(failure, dict):
                failure_message = failure.get("message") or failure.get("error") or json.dumps(failure, ensure_ascii=False)
                failure_traceback = failure.get("traceback")
            else:
                failure_message = str(failure)
                failure_traceback = None
            return {
                "success": False,
                "mazeRunId": run_id,
                "error": failure_message or "Workflow task failed",
                "traceback": failure_traceback,
                "results": results,
            }
        
        # Step 5: 清理（当前服务器不支持 cleanup endpoint，已降级）
        # print(f"[DEBUG] 清理工作流...", file=sys.stderr)
        # workflow.cleanup()
        print(f"[DEBUG] 工作流执行完成（跳过清理步骤）", file=sys.stderr)
        
        return {
            "success": True,
            "mazeRunId": run_id,
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

        elif action == 'get_workspace_tasks':
            result = get_workspace_tasks(params.get('workspaceDir', ''))

        elif action == 'save_workspace_task':
            result = save_workspace_task(
                params.get('workspaceDir', ''),
                params.get('relativePath', ''),
                params.get('code', ''),
                params.get('parse', True),
            )

        elif action == 'delete_workspace_task':
            result = delete_workspace_task(
                params.get('workspaceDir', ''),
                params.get('relativePath', ''),
            )

        elif action == 'rename_workspace_task':
            result = rename_workspace_task(
                params.get('workspaceDir', ''),
                params.get('relativePath', ''),
                params.get('oldFunctionName', ''),
                params.get('newName', ''),
            )
        
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
                params.get('edges', []),
                params.get('staticRunId'),
                params.get('workspaceDir'),
                params.get('workspaceId') or params.get('workspace_id'),
                params.get('workspaceManifestVersion') or params.get('workspace_manifest_version'),
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
