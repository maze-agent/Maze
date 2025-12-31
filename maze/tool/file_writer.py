from maze import task


@task(
    inputs=["file_path", "content", "mode"],
    outputs=["result"],
)
def file_writer(params):
    file_path = params.get("file_path")
    content = params.get("content", "")
    mode = params.get("mode", "w")
    
    if not file_path:
        return {"result": None, "error": "Missing required parameter: file_path"}
    
    try:
        with open(file_path, mode, encoding='utf-8') as f:
            f.write(content)
            
        return {"result": f"Successfully wrote content to {file_path}"}
    except Exception as e:
        return {"result": None, "error": str(e)}