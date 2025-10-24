import socket
import subprocess

def collect_gpu_info():
        try:
            # 执行 nvidia-smi 命令并捕获输出
            result = subprocess.run(['nvidia-smi', '--query-gpu=index,name,utilization.gpu,memory.total,memory.used,memory.free', '--format=csv,noheader,nounits'], stdout=subprocess.PIPE)
            
            # 解码输出为字符串
            output = result.stdout.decode('utf-8')
            
            # 按行分割输出
            lines = output.strip().split('\n')
            
            info = []
            for line in lines:
                values = line.split(', ')
                gpu_index = int(values[0])
                name = values[1]
                utilization = int(values[2])
                memory_total = int(values[3])
                memory_used = int(values[4])
                memory_free = int(values[5])
                
                gpu_info = {
                    'index': gpu_index,
                    'name': name,
                    'utilization': utilization,
                    'memory_total': memory_total,
                    'memory_used': memory_used,
                    'memory_free': memory_free
                }
                info.append(gpu_info)
            return info

        except Exception as e:
            return []

def get_available_ports(n=2):
    """
    获取当前机器上 n 个可用的 TCP 端口。
    
    参数:
        n (int): 需要获取的端口数量，默认为2。
    
    返回:
        List[int]: 包含 n 个可用端口号的列表。
    """
    ports = []
    sockets = []

    try:
        for _ in range(n):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(('', 0))  # 绑定到任意地址，端口由系统自动分配
            port = sock.getsockname()[1]
            ports.append(port)
            sockets.append(sock)  # 保持 socket 打开，防止端口被立即复用
        return ports
    finally:
        # 关闭所有打开的 sockets
        for sock in sockets:
            sock.close()
