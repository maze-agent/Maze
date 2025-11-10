import socket
import subprocess

def collect_gpu_info():
        try:
            result = subprocess.run(['nvidia-smi', '--query-gpu=index,name,utilization.gpu,memory.total,memory.used,memory.free', '--format=csv,noheader,nounits'], stdout=subprocess.PIPE)   
            output = result.stdout.decode('utf-8')
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
    Get a list of n available ports on the current machine.
    """
    ports = []
    sockets = []

    try:
        for _ in range(n):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(('', 0))  # Bind to any address, port automatically assigned by the system
            port = sock.getsockname()[1]
            ports.append(port)
            sockets.append(sock) 
        return ports
    finally:
        for sock in sockets:
            sock.close()
