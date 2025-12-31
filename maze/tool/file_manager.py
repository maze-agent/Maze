from maze import task
import os
import shutil


@task(
    inputs=["action", "source", "destination"],
    outputs=["result"],
)
def file_manager(params):
    action = params.get("action")  # copy, move, delete, list
    source = params.get("source")
    destination = params.get("destination")
    
    if not action:
        return {"result": None, "error": "Missing required parameter: action"}
    
    try:
        if action == "copy":
            if not source or not destination:
                return {"result": None, "error": "For copy action, both source and destination are required"}
            shutil.copy2(source, destination)
            return {"result": f"Successfully copied {source} to {destination}"}
        
        elif action == "move":
            if not source or not destination:
                return {"result": None, "error": "For move action, both source and destination are required"}
            shutil.move(source, destination)
            return {"result": f"Successfully moved {source} to {destination}"}
        
        elif action == "delete":
            if not source:
                return {"result": None, "error": "For delete action, source is required"}
            if os.path.isdir(source):
                shutil.rmtree(source)
            else:
                os.remove(source)
            return {"result": f"Successfully deleted {source}"}
        
        elif action == "list":
            if not source:
                return {"result": None, "error": "For list action, source is required"}
            items = os.listdir(source)
            return {"result": items}
        
        else:
            return {"result": None, "error": f"Unsupported action: {action}"}
    
    except Exception as e:
        return {"result": None, "error": str(e)}