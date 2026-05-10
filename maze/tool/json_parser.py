from maze import task
import json



@task
def json_parser(json_string: str = "", file_path: str = ""):
    
    if not json_string and not file_path:
        return {"result": None, "error": "Either json_string or file_path must be provided"}
    
    try:
        if file_path:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        else:
            data = json.loads(json_string)
            
        return {"result": data}
    except Exception as e:
        return {"result": None, "error": str(e)}