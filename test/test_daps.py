import pytest
from datetime import datetime
from maze import MaClient, task
import time
import torch


'''
"llm_process": [
        "text_length",
        "token_count",
        "batch_size",
        "reason",
    ],
    "vlm_process": [
        "image_height", "image_width", "image_area", "image_aspect_ratio", "batch_size",
        "image_entropy", "edge_density", "text_area_ratio", "text_block_count",
        "avg_brightness", "brightness_variance", "prompt_length", "prompt_token_count"
    ],
    "llm_fuse": [
        "prompt_length", "prompt_token_count", "text1_length", "text2_length",
        "text1_token_count", "text2_token_count", "reason"
    ],
    "speech_process":[
        "duration",
        "audio_array_len",
        "audio_entropy",
        "audio_energy"
    ],
''' 


'''
Test cluster resources
'''
class TestDAPS:
    def test_cluster_resources(self):
 

        client = MaClient()
        workflow = client.create_workflow()
        
        @task(
            inputs=["text"],
            outputs=["result"],
            resources={"cpu": 1,"cpu_mem":0,"gpu":0,"gpu_mem":0},
        )
        def task1(params):
            text = params.get("text")
            return {
                "text_length": 1,
                "token_count": 1,
                "batch_size": 1,
                "reason": 12
            }

        @task(
            inputs=["text"],
            outputs=["result"],
            resources={"cpu": 1,"cpu_mem":0,"gpu":1,"gpu_mem":0},
        )
        def llm_process(params):
            text = params.get("text")
            return {"result": 'MazeMaze'}
 
 
        task1 = workflow.add_task(task1, inputs={"text": "Maze"})
        task2 = workflow.add_task(llm_process, inputs={"text": task1.outputs["result"]})

        run_ids = []
        for _ in range(1):
            run_ids.append(workflow.run())
             
        for run_id in run_ids:
            workflow.get_results(run_id)
            res = workflow.get_task_result(run_id,task2.task_id)
            assert(res['result']['result'] == 'MazeMaze')
        

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])