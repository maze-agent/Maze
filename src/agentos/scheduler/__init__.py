def cpu(cpu_num=1, mem=1):
    """CPUtask"""
    def decorator(func):
        #

        func._task_decorator = {
            'type': 'cpu',
            'cpu_num': cpu_num,
            'mem': mem
        }
        
        def wrapper(*args, **kwargs):
            # wrapper
            task_info = func._task_decorator
            print(f"CPUtask - : {task_info['cpu_num']}, : {task_info['mem']}MB")
            return func(*args, **kwargs)
        
        # wrapper
        wrapper._task_decorator = func._task_decorator
        return wrapper
    return decorator

def io(mem=1):
    """IOtask"""
    def decorator(func):
        func._task_decorator = {
            'type': 'io',
            'cpu_num': 0,
            'mem': mem
        }
        
        def wrapper(*args, **kwargs):
            task_info = func._task_decorator
            print(f"IOtask - : {task_info['mem']}MB")
            return func(*args, **kwargs)
        
        wrapper._task_decorator = func._task_decorator
        return wrapper
    return decorator

def gpu(mem=0, gpu_mem=1, model_name="", backend="huggingface"):
    """GPUtask"""
    def decorator(func):
        func._task_decorator = {
            'type': 'gpu',
            'cpu_num': 0,
            'mem': mem,
            'gpu_mem': gpu_mem,
            'model_name': model_name,
            'backend': backend
        }
        
        def wrapper(*args, **kwargs):
            task_info = func._task_decorator
            print(f"GPUtask - : {task_info['gpu_mem']}MB, model: {task_info['model_name']}, : {task_info['backend']}")
            return func(*args, **kwargs)
        
        wrapper._task_decorator = func._task_decorator
        return wrapper
    return decorator