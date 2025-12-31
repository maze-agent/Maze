from maze import task
import requests


@task(
    inputs=["url", "method", "headers", "data"],
    outputs=["result"],
)
def http_request(params):
    url = params.get("url")
    method = params.get("method", "GET").upper()
    headers = params.get("headers", {})
    data = params.get("data", {})
    
    if not url:
        return {"result": None, "error": "Missing required parameter: url"}
    
    try:
        if method == "GET":
            response = requests.get(url, headers=headers, params=data)
        elif method == "POST":
            response = requests.post(url, headers=headers, json=data)
        elif method == "PUT":
            response = requests.put(url, headers=headers, json=data)
        elif method == "DELETE":
            response = requests.delete(url, headers=headers)
        else:
            return {"result": None, "error": f"Unsupported method: {method}"}
        
        response.raise_for_status()
        
        result = {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "content": response.text,
            "json": response.json() if response.headers.get('content-type', '').startswith('application/json') else None
        }
        
        return {"result": result}
    except Exception as e:
        return {"result": None, "error": str(e)}