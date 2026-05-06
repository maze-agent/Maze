from maze import task

@task(
    inputs=["text"],
    outputs=["result"],
    resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0}
)
def read_csv_file(params):
    """Process text and return a result."""
    text = params.get("text", "")
    return {"result": f"Processed: {text}"}
