import requests
from typing import Optional
from maze.client.front.workflow import MaWorkflow


class MaClient:
    """
    Maze client for connecting to Maze server and managing workflows
    
    Example:
        # Local workflow
        client = MaClient("http://localhost:8000")
        workflow = client.create_workflow()
        
        # Agent service
        client = MaClient("http://localhost:8000", agent_port=8001)
        workflow = client.create_workflow(name="my_agent", mode="server")
    """
    
    def __init__(self, server_url: str = "http://localhost:8000", agent_port: int = 8001):
        """
        Initialize Maze client
        
        Args:
            server_url: Maze server address, defaults to http://localhost:8000
            agent_port: Agent service port, defaults to 8001 (only used in ServerWorkflow mode)
        """
        self.server_url = server_url.rstrip('/')
        self.agent_port = agent_port
        
    def create_workflow(self) -> MaWorkflow:
        """
        Create local workflow (LocalWorkflow)
        
        Workflow that completes after one execution
        
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
    
    def create_server_workflow(self, name: str) -> 'ServerWorkflow':
        """
        Create server workflow (ServerWorkflow)
        
        Workflow that can run continuously as an Agent
        
        Args:
            name: Workflow name (used for API path)
            
        Returns:
            ServerWorkflow: Server workflow object
            
        Example:
            workflow = client.create_server_workflow(name="my_agent")
            task = workflow.add_task(func, inputs={"user_input": None})
            workflow.deploy()
        """
        from maze.client.server_workflow import ServerWorkflow
        return ServerWorkflow(name, self.server_url, self.agent_port)
    
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

