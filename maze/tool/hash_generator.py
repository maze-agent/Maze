from maze import task
import hashlib


@task(
    inputs=["input_data", "algorithm"],
    outputs=["result"],
)
def hash_generator(params):
    input_data = params.get("input_data", "")
    algorithm = params.get("algorithm", "sha256")
    
    supported_algorithms = hashlib.algorithms_available
    
    if algorithm not in supported_algorithms:
        return {"result": None, "error": f"Algorithm {algorithm} not supported. Available: {list(supported_algorithms)[:10]}..."}
    
    try:
        if isinstance(input_data, str):
            input_bytes = input_data.encode('utf-8')
        else:
            input_bytes = input_data
        
        hasher = hashlib.new(algorithm)
        hasher.update(input_bytes)
        hash_result = hasher.hexdigest()
        
        return {"result": hash_result}
    except Exception as e:
        return {"result": None, "error": str(e)}