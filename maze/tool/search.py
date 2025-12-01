from maze import task
import requests

@task(
    inputs=["query", "google_api_key", "google_cse_id"],
    outputs=["result"],
)
def google_search(params):
    query = params.get("query")
    api_key = params.get("google_api_key")
    cse_id = params.get("google_cse_id")

    if not query or not api_key or not cse_id:
        return {"result": None, "error": "Missing required parameters: query, google_api_key, or google_cse_id"}

    url = "https://www.googleapis.com/customsearch/v1"
    search_params = {
        "q": query,
        "key": api_key,
        "cx": cse_id,
    }

    try:
        response = requests.get(url, params=search_params)
        response.raise_for_status()
        data = response.json()
        results = []
        for item in data.get("items", []):
            results.append({
                "title": item.get("title"),
                "link": item.get("link"),
                "snippet": item.get("snippet")
            })
        return {"result": results}
    except Exception as e:
        return {"result": None, "error": str(e)}