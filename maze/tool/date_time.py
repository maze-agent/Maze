from maze import task
from datetime import datetime


@task(
    inputs=["format"],
    outputs=["result"],
)
def date_time(params):
    format_str = params.get("format", "%Y-%m-%d %H:%M:%S")
    
    try:
        current_time = datetime.now().strftime(format_str)
        
        return {"result": current_time}
    except Exception as e:
        return {"result": None, "error": str(e)}