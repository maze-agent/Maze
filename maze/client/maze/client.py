import requests
from typing import Optional
from maze.client.maze.workflow import MaWorkflow


class MaClient:
    """
    Maze client for connecting to Maze server and managing workflows
    
    Example:
        client = MaClient("http://localhost:8000")
        workflow = client.create_workflow()
    """
    
    def __init__(self, server_url: str = "http://localhost:8000"):
        """
        Initialize Maze client
        
        Args:
            server_url: Maze server address, defaults to http://localhost:8000
        """
        self.server_url = server_url.rstrip('/')
        
    def create_workflow(self) -> MaWorkflow:
        """
        Create a new workflow
        
        Returns:
            MaWorkflow: Workflow object
            
        Raises:
            Exception: If creation fails
        """
        url = f"{self.server_url}/create_workflow"
        response = requests.post(url)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "success":
                workflow_id = data["workflow_id"]
                return MaWorkflow(workflow_id, self.server_url)
            else:
                raise Exception(f"Failed to create workflow: {data.get('message', 'Unknown error')}")
        else:
            raise Exception(f"Request failed, status code: {response.status_code}, response: {response.text}")
    
    def get_workflow(self, workflow_id: str) -> MaWorkflow:
        """
        Get existing workflow object
        
        Args:
            workflow_id: Workflow ID
            
        Returns:
            MaWorkflow: Workflow object
        """
        return MaWorkflow(workflow_id, self.server_url)
    
    def get_ray_head_port(self) -> dict:
        """
        Get Ray head node port (for worker connection)
        
        Returns:
            dict: Dictionary containing port information
        """
        url = f"{self.server_url}/get_head_ray_port"
        response = requests.post(url)
        
        if response.status_code == 200:
            return response.json()
        else:
            raise Exception(f"Failed to get Ray port, status code: {response.status_code}")

