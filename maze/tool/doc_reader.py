from maze import task
from docx import Document



@task
def doc_reader(file_path: str = ""):
    if not file_path:
        return {"result": None, "error": "Missing required parameter: file_path"}
    try:
        doc = Document(file_path)
        
        text = ""
        for paragraph in doc.paragraphs:
            text += paragraph.text + "\n"
            
        return {"result": text}
    except Exception as e:
        return {"result": None, "error": str(e)}