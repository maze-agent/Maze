 
import pytest
from maze import MaClient


class TestLLMInstance:
    def test_llm_instance(self):
        client = MaClient()
        #1.create instance
        instance_id = client.start_llm_instance("facebook/opt-125m")
        #2.query instance
        response = client.query_llm_instance("What is the capital of France?", instance_id)
        print(response)
        #3.stop instance
        client.stop_llm_instance(instance_id)

      
 
if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])