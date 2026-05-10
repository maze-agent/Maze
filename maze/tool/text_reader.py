from maze import task


@task
def text_reader(file_path: str = ""):
    
    if not file_path:
        return {"result": None, "error": "Missing required parameter: file_path"}
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        return {"result": content}
    except Exception as e:
        return {"result": None, "error": str(e)}