from maze import task
from docx import Document


@task(
    inputs=["file_path"],
    outputs=["result"],
)
def doc_reader(params):
    file_path = params.get("file_path")
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