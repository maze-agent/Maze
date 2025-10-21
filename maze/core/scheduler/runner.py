import ray
import ast

@ray.remote
def remote_task_runner(code_str:str,task_input_data:dict):
    runner = Runner(code_str,task_input_data)
    output = runner.run()
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
                break #提取顶层函数
        return func

    def run(self):
        # 提取函数
        func_node = self._extract_function()
         
        # 提取导入语句
        import_nodes = self._extract_imports()
        
        # 创建一个命名空间来执行代码
        namespace = {}
        
        # 执行导入语句
        for imp in import_nodes:
            # 将Import/ImportFrom节点编译成可执行的代码对象
            module = ast.Module(body=[imp], type_ignores=[])
            code = compile(module, '<string>', 'exec')
            exec(code, namespace)
        
        # 将FunctionDef节点编译成可执行的代码对象
        module = ast.Module(body=[func_node], type_ignores=[])
        code = compile(module, '<string>', 'exec')
        exec(code, namespace)
        
        # 获取函数名并调用
        func_name = func_node.name
        if func_name in namespace:
            return namespace[func_name](self.task_input_data)
        else:
            raise NameError(f"Function {func_name} not found in namespace")

            