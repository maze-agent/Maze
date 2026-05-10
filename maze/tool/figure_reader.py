from maze import task
from PIL import Image


@task
def figure_reader(file_path: str = ""):
    
    if not file_path:
        return {"result": None, "error": "Missing required parameter: file_path"}
    
    try:
        image = Image.open(file_path)
        info = {
            "format": image.format,
            "size": image.size,
            "mode": image.mode
        }
        return {"result": info}
    except Exception as e:
        return {"result": None, "error": str(e)}