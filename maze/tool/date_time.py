from maze import task
from datetime import datetime



@task
def date_time(format: str = "%Y-%m-%d %H:%M:%S"):
    format_str = format
    
    try:
        current_time = datetime.now().strftime(format_str)
        
        return {"result": current_time}
    except Exception as e:
        return {"result": None, "error": str(e)}