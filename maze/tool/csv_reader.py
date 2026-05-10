from maze import task
import csv


@task
def csv_reader(file_path: str = ""):
    
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