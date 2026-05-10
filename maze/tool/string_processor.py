from maze import task



@task
def string_processor(text: str = "", operation: str = "", options: dict | None = None):
    options = options or {}
    
    if not operation:
        return {"result": None, "error": "Missing required parameter: operation"}
    
    try:
        if operation == "upper":
            result = text.upper()
        elif operation == "lower":
            result = text.lower()
        elif operation == "title":
            result = text.title()
        elif operation == "split":
            separator = options.get("separator", " ")
            result = text.split(separator)
        elif operation == "replace":
            old_str = options.get("old", "")
            new_str = options.get("new", "")
            result = text.replace(old_str, new_str)
        elif operation == "length":
            result = len(text)
        elif operation == "reverse":
            result = text[::-1]
        elif operation == "strip":
            result = text.strip()
        else:
            return {"result": None, "error": f"Unsupported operation: {operation}"}
        
        return {"result": result}
    except Exception as e:
        return {"result": None, "error": str(e)}