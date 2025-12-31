from maze import task
import json


@task(
    inputs=["json_string", "file_path"],
    outputs=["result"],
)
def json_parser(params):
    json_string = params.get("json_string")
    file_path = params.get("file_path")
    
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