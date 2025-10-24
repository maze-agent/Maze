import subprocess
import requests

class Worker():
    @staticmethod
    def get_head_ray_port(addr):
        # 定义请求的 URL
        url = f"http://{addr}/get_head_ray_port"

        try:
            # 发送 POST 请求
            response = requests.post(url)

            # 检查响应状态码
            if response.status_code == 200:
                data = response.json()
                head_ray_port = data["head_ray_port"]
                return head_ray_port
            else:
                print(f"请求失败，状态码：{response.status_code}")
                print("响应内容：", response.text)
                return None
        except requests.exceptions.RequestException as e:
            print("请求发生错误：", e)
            return None

    @staticmethod
    def conect_to_head(addr: str):
        url = f"http://{addr}/conect_to_head"

        try:
            response = requests.post(url)
            if response.status_code == 200:
                data = response.json()
            else:
                print(f"请求失败，状态码：{response.status_code}")
                print("响应内容：", response.text)
        except requests.exceptions.RequestException as e:
            print("请求发生错误：", e)

    @staticmethod
    def connect_to_head(addr: str):
        head_ray_port = Worker.get_head_ray_port(addr)
        
 
        command = [
            "ray", "start", "--address", addr.split(":")[0] + ":" + str(head_ray_port) ,  
        ]
        result = subprocess.run(
            command,
            check=True,                    # 如果命令失败（返回码非0），抛出异常
            text=True,                     # 以字符串形式处理输出
            capture_output=True,           # 捕获 stdout 和 stderr
        )
      
        print("标准输出:\n", result.stdout)
        print("标准错误:\n", result.stderr)

        Worker.connect_to_head(addr)
