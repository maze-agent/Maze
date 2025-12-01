from maze import task
import cv2

@task(
    inputs=["file_path"],
    outputs=["result"],
)
def video_reader(params):
    file_path = params.get("file_path")
    
    if not file_path:
        return {"result": None, "error": "Missing required parameter: file_path"}
    
    try:
        cap = cv2.VideoCapture(file_path)
        
        info = {
            "frame_count": int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
            "fps": cap.get(cv2.CAP_PROP_FPS),
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "codec": int(cap.get(cv2.CAP_PROP_FOURCC))
        }
        
        cap.release()
        
        return {"result": info}
    except Exception as e:
        return {"result": None, "error": str(e)}