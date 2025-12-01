from maze import task
import PyPDF2
import io


@task(
    inputs=["file_path"],
    outputs=["result"],
)
def pdf_reader(params):
    file_path = params.get("file_path")
    
    if not file_path:
        return {"result": None, "error": "Missing required parameter: file_path"}
    
    try:
        with open(file_path, 'rb') as f:
            pdf_file = io.BytesIO(f.read())
    
        pdf_reader = PyPDF2.PdfReader(pdf_file)
        text = ""
        for page in pdf_reader.pages:
            text += page.extract_text()
            
        return {"result": text}
    except Exception as e:
        return {"result": None, "error": str(e)}