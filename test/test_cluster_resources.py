import pytest
from datetime import datetime
from maze import MaClient, task
import time
import torch

 
'''
Test cluster resources
'''
class TestClusterResources:
    def test_cluster_resources(self):
        client = MaClient()
        workflow = client.create_workflow()
        
        @task(resources={"cpu": 1,"cpu_mem":0,"gpu":0,"gpu_mem":0})
        def task1(text):
            time.sleep(2)
            return {"result": text}

        @task(resources={"cpu": 1,"cpu_mem":0,"gpu":1,"gpu_mem":0})
        def task2(text):
            tensor = torch.rand(1000, 1000).cuda()
            time.sleep(8)
            return {"result": text}

        @task(resources={"cpu": 1,"cpu_mem":0,"gpu":1,"gpu_mem":0})
        def task3(text):
            tensor = torch.rand(1000, 1000).cuda()
            time.sleep(8)
            return {"result": text}

        @task(resources={"cpu": 1,"cpu_mem":0,"gpu":0,"gpu_mem":0})
        def task4(text1, text2):
            time.sleep(2)
            return {"result": text1+text2}  
            
        task1 = workflow.add_task(task1, inputs={"text": "Maze"})
        task2 = workflow.add_task(task2, inputs={"text": task1.outputs["result"]})
        task3 = workflow.add_task(task3, inputs={"text": task1.outputs["result"]})
        task4 = workflow.add_task(task4, inputs={"text1": task2.outputs["result"], "text2": task3.outputs["result"]})

        run_ids = []
        for _ in range(20):
            run_ids.append(workflow.run())
             
        for run_id in run_ids:
            workflow.get_results(run_id)
            res = workflow.get_task_result(run_id,task4.task_id)
            assert(res['result']['result'] == 'MazeMaze')
        

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])