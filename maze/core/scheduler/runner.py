import ray
import ast
import binascii
import base64
import cloudpickle
from maze.core.files.lineage import run_task_with_file_context

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
    
    if code_ser is not None:
        func = cloudpickle.loads(base64.b64decode(code_ser))
        output = run_task_with_file_context(func, task_input_data, file_context)
        return output
    elif code_str is not None:
        runner = Runner(code_str, task_input_data)
        output = run_task_with_file_context(lambda data: runner.run(data), task_input_data, file_context)
        return output
    else:
        raise ValueError("Missing code_str or code_ser")

@ray.remote(max_retries=0)
def remote_lgraph_task_runner(code_ser:str,args:str,kwargs:str,cuda_visible_devices:str|None=None):
    if cuda_visible_devices:
        import os
        os.environ["CUDA_VISIBLE_DEVICES"] = cuda_visible_devices

    func = cloudpickle.loads(base64.b64decode(code_ser))
    args = cloudpickle.loads(base64.b64decode(args))
    kwargs = cloudpickle.loads(base64.b64decode(kwargs))

    output = func(*args, **kwargs)
    return output
 

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

            
