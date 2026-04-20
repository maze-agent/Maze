# find_path.py
import ray
import sys

print("Connecting to the existing Ray cluster...")
# via "ray start --head" 
ray.init(address='auto')
print("Connection successful.")

@ray.remote
def get_executed_file_path():
    """
     Ray 
    failed task 
    """
    try:
        #

        import core.workflows.openagi.document_qa.task
        
        #  Python file
        return core.workflows.openagi.document_qa.task.__file__
    except Exception as e:
        return f"Error importing module: {e}"

print("Running remote task to find the file path...")
#

actual_path = ray.get(get_executed_file_path.remote())
print("Task completed.")

print("\n" + "="*80)
print(">>>  / DIAGNOSTIC RESULT <<<")
print(f"\nRay file:\n{actual_path}")
print("\n" + "="*80)

print("\ncurrently:")
print(f"/home/hustlbw/gujing/AgentOS/src/core/workflows/openagi/document_qa/task.py")
print("\nalignedfound")

ray.shutdown()
