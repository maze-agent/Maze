import requests
from typing import Optional
from maze.core.client.workflow import MaWorkflow


class MaClient:
    """
    Maze客户端，用于连接Maze服务器并管理工作流
    
    示例:
        client = MaClient("http://localhost:8000")
        workflow = client.create_workflow()
    """
    
    def __init__(self, server_url: str = "http://localhost:8000"):
        """
        初始化Maze客户端
        
        Args:
            server_url: Maze服务器地址，默认为 http://localhost:8000
        """
        self.server_url = server_url.rstrip('/')
        
    def create_workflow(self) -> MaWorkflow:
        """
        创建一个新的工作流
        
        Returns:
            MaWorkflow: 工作流对象
            
        Raises:
            Exception: 如果创建失败
        """
        url = f"{self.server_url}/create_workflow"
        response = requests.post(url)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "success":
                workflow_id = data["workflow_id"]
                return MaWorkflow(workflow_id, self.server_url)
            else:
                raise Exception(f"创建工作流失败: {data.get('message', 'Unknown error')}")
        else:
            raise Exception(f"请求失败，状态码：{response.status_code}, 响应：{response.text}")
    
    def get_workflow(self, workflow_id: str) -> MaWorkflow:
        """
        获取已存在的工作流对象
        
        Args:
            workflow_id: 工作流ID
            
        Returns:
            MaWorkflow: 工作流对象
        """
        return MaWorkflow(workflow_id, self.server_url)
    
    def get_ray_head_port(self) -> dict:
        """
        获取Ray头节点端口（用于worker连接）
        
        Returns:
            dict: 包含端口信息的字典
        """
        url = f"{self.server_url}/get_head_ray_port"
        response = requests.post(url)
        
        if response.status_code == 200:
            return response.json()
        else:
            raise Exception(f"获取Ray端口失败，状态码：{response.status_code}")

