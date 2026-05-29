from typing import Any,Dict
import subprocess
import time
import ray
import logging
import requests
import socket
from maze.utils.utils import collect_gpu_info

logger = logging.getLogger(__name__)

class Worker():
    @staticmethod
    def _registration_summary(response: Dict[str, Any] | None) -> str:
        worker = response.get("worker") if isinstance(response, dict) else None
        if not isinstance(worker, dict):
            return "registration response unavailable"
        return (
            f"status={worker.get('registration_status', 'unknown')} "
            f"node_id={worker.get('node_id', 'unknown')} "
            f"node_ip={worker.get('node_ip', 'unknown')}"
        )

    @staticmethod
    def _parse_core_response(response, url: str) -> Dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Maze core returned invalid JSON for {url}: {exc}") from exc

        if not isinstance(payload, dict):
            raise RuntimeError(f"Maze core returned a non-object response for {url}: {payload!r}")

        status = payload.get("status")
        if status not in (None, "success"):
            detail = payload.get("detail") or payload.get("message") or payload.get("error") or payload
            raise RuntimeError(f"Maze core returned non-success status for {url}: {detail}")

        return payload

    @staticmethod
    def _send_post_request(
        url: str,
        data: Dict[str, Any] | None = None,
        *,
        retries: int = 3,
        retry_delay: float = 1.0,
        timeout: float = 10.0,
    ):
        last_error = None
        payload = data or {}
        for attempt in range(1, max(1, int(retries)) + 1):
            try:
                response = requests.post(url, json=payload, timeout=timeout)
                if response.status_code == 200:
                    return Worker._parse_core_response(response, url)
                last_error = RuntimeError(f"Failed to send request: {response.status_code}, {response.text}")
            except requests.RequestException as exc:
                last_error = exc
            except RuntimeError as exc:
                last_error = exc

            if attempt < retries:
                time.sleep(max(0.0, float(retry_delay)) * attempt)

        raise RuntimeError(f"Failed to send request after {retries} attempt(s): {last_error}")

    @staticmethod
    def _ray_addr(addr: str, head_ray_port: int) -> str:
        return addr.split(":")[0] + ":" + str(head_ray_port)

    @staticmethod
    def _local_ip_for_target(ray_addr: str) -> str | None:
        host, port_text = ray_addr.rsplit(":", 1)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((host, int(port_text)))
                return sock.getsockname()[0]
        except Exception:
            try:
                return ray.util.get_node_ip_address()
            except Exception:
                return None

    @staticmethod
    def _local_ray_runtime_active() -> bool:
        if ray.is_initialized():
            return True
        command = ["ray", "status", "--address", "auto"]
        try:
            result = subprocess.run(
                command,
                check=False,
                text=True,
                capture_output=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "Timed out checking local Ray runtime with `ray status --address auto`; "
                "run `ray stop --force` on this worker before starting Maze again."
            ) from exc
        return result.returncode == 0

    @staticmethod
    def _ensure_local_node_joined(ray_addr: str, local_ip: str | None):
        if local_ip is None:
            return
        live_nodes = [
            node
            for node in ray.nodes()
            if node.get("Alive")
        ]
        if any(node.get("NodeManagerAddress") == local_ip for node in live_nodes):
            return

        live_ips = sorted({str(node.get("NodeManagerAddress")) for node in live_nodes if node.get("NodeManagerAddress")})
        raise RuntimeError(
            "Ray is reachable, but this worker does not appear as an alive node in the target cluster. "
            f"local_ip={local_ip} target={ray_addr} live_node_ips={live_ips}. "
            "If Ray is already running on this machine for another cluster, run `ray stop --force` "
            "before `maze start --worker`."
        )
 
    @staticmethod
    def _join_ray(addr: str, head_ray_port: int):
        ray_addr = Worker._ray_addr(addr, head_ray_port)
        local_ip = Worker._local_ip_for_target(ray_addr)
        local_ray_active = Worker._local_ray_runtime_active()

        if local_ray_active:
            print("Local Ray runtime already active; reusing it instead of starting another raylet.")
        else:
            command = [
                "ray", "start", "--address", ray_addr,
            ]
            if local_ip:
                command.extend(["--node-ip-address", local_ip])
            result = subprocess.run(
                command,
                check=False,
                text=True,
                capture_output=True,
            )
            output = result.stderr + result.stdout
            if result.returncode != 0 and "already" not in output.lower():
                raise RuntimeError(result.stderr or result.stdout or "Failed to join Ray cluster")
        if not ray.is_initialized():
            ray.init(address=ray_addr, ignore_reinit_error=True)
        Worker._ensure_local_node_joined(ray_addr, local_ip)
        return ray_addr

    @staticmethod
    def _current_node_resources():
        current_node_id = ray.get_runtime_context().get_node_id()
        current_node_ip = None
        cur_node = None

        while True:
            nodes = ray.nodes()
            for node in nodes:
                if node['NodeID'] == current_node_id and node['Alive']:
                    cur_node = node
                    current_node_ip = node['NodeManagerAddress']
                    break
            if cur_node is not None:
                break
            time.sleep(0.2)

        resources = {
            "cpu":cur_node["Resources"]["CPU"],
            "cpu_mem":cur_node["Resources"]["memory"],
            "gpu_resource":{}
        }
        gpu_info = collect_gpu_info()
        if len(gpu_info) > 0:
            for gpu in gpu_info:
                gpu_id = gpu["index"]
                gpu_mem = gpu["memory_free"]
                resources["gpu_resource"][gpu_id] = {
                    "gpu_id" : gpu_id,
                    "gpu_mem":gpu_mem,
                    "gpu_num":1
                }
        return current_node_id, current_node_ip, resources

    @staticmethod
    def _register_worker(addr: str):
        current_node_id, current_node_ip, resources = Worker._current_node_resources()
        response = Worker._send_post_request(
            url=f"http://{addr}/start_worker",
            data={
                "node_ip":current_node_ip,
                "node_id":current_node_id,
                "resources":resources,
            },
            retries=5,
            retry_delay=1,
        )
        worker = response.get("worker")
        if not isinstance(worker, dict):
            raise RuntimeError(f"Maze core /start_worker response missing worker payload: {response}")

        if worker.get("node_id") != current_node_id:
            raise RuntimeError(
                "Maze worker registration node_id mismatch: "
                f"expected {current_node_id}, got {worker.get('node_id')}"
            )
        if worker.get("node_ip") != current_node_ip:
            raise RuntimeError(
                "Maze worker registration node_ip mismatch: "
                f"expected {current_node_ip}, got {worker.get('node_ip')}"
            )

        registration_status = worker.get("registration_status", "unknown")
        if registration_status not in {"created", "updated", "already_registered"}:
            raise RuntimeError(f"Unexpected Maze worker registration status: {registration_status}")
        print(
            "===Success to register worker=== "
            f"{Worker._registration_summary(response)} "
            f"cpu={resources['cpu']} gpu={len(resources['gpu_resource'])}"
        )
        return response

    @staticmethod
    def _connect_and_register(addr: str):
        data = Worker._send_post_request(f"http://{addr}/get_head_ray_port")
        head_ray_port = data["port"]
        ray_addr = Worker._join_ray(addr, head_ray_port)
        print(f"Head URL: http://{addr}")
        print(f"Connected to Ray cluster: {ray_addr}")
        response = Worker._register_worker(addr)
        return {
            "ray_addr": ray_addr,
            "registration": response,
        }

    @staticmethod
    def _agent_loop(
        addr: str,
        heartbeat_interval: float = 10,
        *,
        stop_after_iterations: int | None = None,
    ):
        iteration = 0
        print(f"Worker agent mode enabled, heartbeat_interval={heartbeat_interval}s")
        while True:
            if stop_after_iterations is not None and iteration >= stop_after_iterations:
                return
            iteration += 1
            time.sleep(max(1.0, float(heartbeat_interval)))
            try:
                Worker._register_worker(addr)
            except Exception as exc:
                print(f"Worker heartbeat failed: {exc}")
                try:
                    reconnect_result = Worker._connect_and_register(addr)
                    registration = reconnect_result.get("registration") if isinstance(reconnect_result, dict) else None
                    print(f"Worker reconnect succeeded: {Worker._registration_summary(registration)}")
                except Exception as reconnect_exc:
                    print(f"Worker reconnect failed: {reconnect_exc}")
                    print(
                        "Recovery hint: run "
                        f"`maze cluster reconcile-workers --server-url http://{addr}` "
                        "to inspect Ray nodes that are not registered with Maze."
                    )

    @staticmethod
    def start_worker(addr: str, agent: bool = False, heartbeat_interval: float = 10):
        try:
            Worker._connect_and_register(addr)
            if agent:
                Worker._agent_loop(addr, heartbeat_interval=heartbeat_interval)
        except Exception as e:
            stdout = getattr(e, "stdout", None)
            stderr = getattr(e, "stderr", None)
            if stdout:
                print(stdout)
            if stderr:
                print(stderr)
            print(f"Failed to start worker: {e}")
            raise
    
    @staticmethod
    def stop_worker():
        try:
            command = [
                "ray", "stop",
            ]
            result = subprocess.run(
                command,
                check=True,                  
                text=True,                 
                capture_output=True,      
            )
            if result.returncode != 0:
                raise RuntimeError(f"Failed to start Ray: {result.stderr}")
            print("===Success to stop worker===")
        except Exception as e:
            print(e.stdout)
            print(e.stderr)
