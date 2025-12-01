from maze import task
import csv

@task(
    inputs=["file_path"],
    outputs=["result"],
)
def csv_reader(params):
    file_path = params.get("file_path")
    
    if not file_path:
        return {"result": None, "error": "Missing required parameter: file_path"}
    try:
        rows = []
        
        with open(file_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            for row in reader:
                rows.append(row)
            
        return {"result": rows}
    except Exception as e:
        return {"result": None, "error": str(e)}