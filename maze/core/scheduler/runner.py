import ray
import ast
import binascii
import base64
import cloudpickle
from maze.core.files.lineage import ArtifactError, run_task_with_file_context
from maze.core.scheduler.error import exception_to_error_envelope, task_error_result


def run_code_task(
    code_str: str = None,
    code_ser: str = None,
    task_input_data: dict = None,
    file_context: dict | None = None,
):
    try:
        if code_ser is not None:
            func = cloudpickle.loads(base64.b64decode(code_ser))
            return run_task_with_file_context(func, task_input_data, file_context)
        if code_str is not None:
            runner = Runner(code_str, task_input_data)
            return run_task_with_file_context(lambda data: runner.run(data), task_input_data, file_context)
        raise ValueError("Missing code_str or code_ser")
    except ArtifactError as exc:
        return task_error_result(
            exception_to_error_envelope(
                "artifact_error",
                exc,
                origin="artifact",
            )
        )
    except Exception as exc:
        return task_error_result(
            exception_to_error_envelope(
                "user_code",
                exc,
                origin="runner",
            )
        )


def run_langgraph_task(code_ser: str, args: str, kwargs: str):
    try:
        func = cloudpickle.loads(base64.b64decode(code_ser))
        loaded_args = cloudpickle.loads(base64.b64decode(args))
        loaded_kwargs = cloudpickle.loads(base64.b64decode(kwargs))
        return func(*loaded_args, **loaded_kwargs)
    except Exception as exc:
        return task_error_result(
            exception_to_error_envelope(
                "user_code",
                exc,
                origin="runner",
            )
        )

@ray.remote(max_retries=0)
def remote_task_runner(
    code_str:str=None,
    code_ser:str=None,
    task_input_data:dict=None,
    cuda_visible_devices:str|None=None,
    file_context:dict|None=None,
):
    if cuda_visible_devices:
        import os
        os.environ["CUDA_VISIBLE_DEVICES"] = cuda_visible_devices

    return run_code_task(
        code_str=code_str,
        code_ser=code_ser,
        task_input_data=task_input_data,
        file_context=file_context,
    )

@ray.remote(max_retries=0)
def remote_lgraph_task_runner(code_ser:str,args:str,kwargs:str,cuda_visible_devices:str|None=None):
    if cuda_visible_devices:
        import os
        os.environ["CUDA_VISIBLE_DEVICES"] = cuda_visible_devices

    return run_langgraph_task(code_ser, args, kwargs)
 

class Runner():
    def __init__(self,code_str,task_input_data):
        self.code_str = code_str
        self.task_input_data = task_input_data
    
    def _extract_imports(self):
        tree = ast.parse(self.code_str)
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imports.append(node)
        return imports

    def _extract_function(self):
        func = None
        tree = ast.parse(self.code_str)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                func = node
                break
        return func

    def run(self, task_input_data=None):
        func_node = self._extract_function()
        import_nodes = self._extract_imports()
        namespace = {}
        
    
        for imp in import_nodes:
            module = ast.Module(body=[imp], type_ignores=[])
            code = compile(module, '<string>', 'exec')
            exec(code, namespace)
        
        module = ast.Module(body=[func_node], type_ignores=[])
        code = compile(module, '<string>', 'exec')
        exec(code, namespace)
        
        func_name = func_node.name
        if func_name in namespace:
            result = namespace[func_name](**(task_input_data if task_input_data is not None else (self.task_input_data or {})))
            if not isinstance(result, dict):
                raise TypeError(
                    f"Task {func_name} must return a dict. "
                    f"Got {type(result).__name__} instead."
                )
            return result
        else:
            raise NameError(f"Function {func_name} not found in namespace")

            
