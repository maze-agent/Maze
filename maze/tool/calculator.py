from maze import task


@task(
    inputs=["expression"],
    outputs=["result"],
)
def calculator(params):
    expression = params.get("expression")
    
    if not expression:
        return {"result": None, "error": "Missing required parameter: expression"}
    
    try:
        # 使用 eval 计算表达式，实际使用中需要注意安全性
        result = eval(expression, {"__builtins__": {}}, {})
        
        return {"result": result}
    except Exception as e:
        return {"result": None, "error": str(e)}