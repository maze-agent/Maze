import time
import requests
import json
import argparse
import os
import sys
from typing import List, Dict, Any, Optional
from datetime import datetime
import threading
import random
import numpy as np
import queue

# ==========================================================================
#  Part 0: Helper Functions (newclear_console)
# ==========================================================================
def clear_console():
    """"""
    os.system('cls' if os.name == 'nt' else 'clear')

def rich_print(text: str):
    color_map = {"RED": "\033[91m", "GREEN": "\033[92m", "YELLOW": "\033[93m", "BLUE": "\033[94m", "MAGENTA": "\033[95m", "CYAN": "\033[96m", "WHITE": "\033[97m", "BOLD": "\033[1m", "END": "\033[0m"}
    for key, value in color_map.items(): text = text.replace(key, value)
    print(text + color_map["END"])

def load_dags_from_jsonl(file_path: str) -> List[Dict[str, Any]]:
    dag_definitions = []
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    try:
                        dag_definitions.append(json.loads(line))
                    except json.JSONDecodeError: pass
        return dag_definitions
    except FileNotFoundError:
        return []

# ==========================================================================
#  Part 1: API Client (new get_status_text )
# ==========================================================================
class TaskLevelClient:
    """ (Task-level) API"""
    def __init__(self, master_addr: str):
        self.base_url = f"http://{master_addr}"
        rich_print(f"✅  WHITE Initialized client for OUR SYSTEM at {self.base_url} WHITE ")

    def submit_dag(self, query: Dict, sub_time: float) -> Optional[Dict]:
        payload = { "dag_ids": [query["dag_id"]], "dag_sources": [query["dag_source"]], "dag_types": [query["dag_type"]], "dag_supplementary_files": [query["dag_supplementary_files"]], "sub_time": sub_time }
        url = f"{self.base_url}/dag/"
        try:
            response = requests.post(url, json=payload, timeout=30)
            response.raise_for_status()
            res = response.json()
            submitted = [dag for dag in res.get("data", []) if "error" not in dag]
            return submitted[0] if submitted else None
        except requests.exceptions.RequestException:
            return None

    def is_finished(self, handle: Dict) -> bool:
        # ()
        run_id = handle['run_id']
        url = f"{self.base_url}/status/"
        try:
            response = requests.post(url, json={"run_id": run_id}, timeout=5)
            if response.status_code == 200:
                status_dict = response.json().get("data", {}).get("dag_status")
                if status_dict:
                    total = len(status_dict)
                    completed = sum(1 for task in status_dict.values() if task.get("status") != "unfinished")
                    return total > 0 and completed == total
            return False
        except requests.exceptions.RequestException:
            return False

    def get_status_text(self, handle: Dict) -> str:
        """"""
        run_id = handle['run_id']
        url = f"{self.base_url}/status/"
        try:
            response = requests.post(url, json={"run_id": run_id}, timeout=2)
            if response.status_code == 200:
                status_dict = response.json().get("data", {}).get("dag_status")
                if status_dict:
                    total = len(status_dict)
                    completed = sum(1 for task in status_dict.values() if task.get("status") != "unfinished")
                    if total > 0 and completed == total:
                        return "Finished"
                    return f"{completed}/{total} tasks completed"
            return "Polling status..."
        except requests.exceptions.RequestException:
            return "Connection error..."

    def release(self, handle: Dict):
        url = f"{self.base_url}/release/"
        try:
            payload = {"run_id": handle['run_id'], "dag_id": handle['dag_id'], "task2id": handle['task2id']}
            requests.post(url, json=payload, timeout=10)
        except requests.exceptions.RequestException: pass

    def get_and_print_results(self, handle: Dict):
        """DAG"""
        run_id = handle['run_id']
        dag_id = handle['dag_id']
        task2id = handle['task2id']
        url = f"{self.base_url}/get/"
        results = {}
        
        rich_print(f"\n BOLD MAGENTA 🔧  DAG '{dag_id}' (Run: {run_id}) ... BOLD MAGENTA ")
        for task_name, task_id in task2id.items():
            payload = {"run_id": run_id, "func_name": task_name, "task_id": task_id}
            try:
                response = requests.post(url, json=payload, timeout=20)
                if response.status_code == 200:
                    res = response.json()
                    if res.get("data") and "task_ret_data" in res["data"]:
                        # JSONstring
                        try: results[task_name] = json.loads(res['data']['task_ret_data'])
                        except (json.JSONDecodeError, TypeError): results[task_name] = res['data']['task_ret_data']
                    else: results[task_name] = f"Error: {res.get('msg')}"
                else: results[task_name] = f"Error: Status {response.status_code}"
            except requests.exceptions.RequestException as e:
                results[task_name] = f"Error: {e}"
        
        pretty_result = json.dumps(results, indent=4, ensure_ascii=False)
        rich_print(f" BOLD GREEN --- emit for DAG '{dag_id}' --- BOLD GREEN \n{pretty_result}\n BOLD GREEN ----------------------------------------- BOLD GREEN ")

class AgentLevelClient:
    """ AutoGen / AgentScope (Agent-level) API"""
    def __init__(self, master_addr: str):
        self.base_url = f"http://{master_addr}"
        rich_print(f"✅  WHITE Initialized client for AUTOGEN/AGENTSCOPE at {self.base_url} WHITE ")

    def submit_dag(self, query: Dict, sub_time: float) -> Optional[str]:
        payload = { "dag_ids": [query["dag_id"]], "dag_sources": [query["dag_source"]], "dag_types": [query["dag_type"]], "dag_supplementary_files": [query["dag_supplementary_files"]], "sub_time": sub_time }
        url = f"{self.base_url}/submit_dag"
        try:
            response = requests.post(url, json=payload, timeout=30)
            response.raise_for_status()
            res = response.json()
            submitted = res.get("submitted", [])
            if submitted and "error" not in submitted[0]:
                return submitted[0].get("uuid")
            return None
        except requests.exceptions.RequestException:
            return None

    def get_status_text(self, handle: str) -> str:
        """"""
        uuid = handle
        try:
            response = requests.get(f"{self.base_url}/dag_status/{uuid}", timeout=2)
            if response.status_code == 200:
                return response.json().get("status", "Unknown")
            return "Polling status..."
        except requests.exceptions.RequestException:
            return "Connection error..."

    def is_finished(self, handle: str) -> bool:
        # ()
        uuid = handle
        try:
            response = requests.get(f"{self.base_url}/dag_status/{uuid}", timeout=5)
            if response.status_code == 200:
                return response.json().get("status").lower() == "finished"
            return False
        except requests.exceptions.RequestException:
            return False

    def get_and_print_results(self, handle: str):
        """ DAG """
        uuid = handle
        rich_print(f"\n BOLD MAGENTA 🔧  DAG (UUID: {uuid}) ... BOLD MAGENTA ")
        try:
            response = requests.get(f"{self.base_url}/get_final_result/{uuid}", timeout=20)
            if response.status_code == 200:
                result_data = response.json()
                pretty_result = json.dumps(result_data, indent=4, ensure_ascii=False)
                rich_print(f" BOLD GREEN --- emit for DAG (UUID: {uuid}) --- BOLD GREEN \n{pretty_result}\n BOLD GREEN ----------------------------------------- BOLD GREEN ")
            else:
                rich_print(f"  -> ⚠️  YELLOW  for {uuid}: Status {response.status_code} YELLOW ")
        except requests.exceptions.RequestException as e:
            rich_print(f"❌  RED  {uuid} : {e} RED ")

    def release(self, handle: Any):
        pass

# ==========================================================================
#  Part 2:  (BenchmarkRunner - )
# ==========================================================================
class BenchmarkRunner:
    def __init__(self, client: Any, all_queries: List[Dict], load_profile: List[tuple], num_workers: int, random_seed: int= 42):
        # (V2.2)
        self.client = client
        self.all_queries = all_queries
        self.load_profile = load_profile
        self.num_workers = num_workers
        self.request_queue = queue.Queue()
        self.results_lock = threading.Lock()
        self.results = []
        self.stop_event = threading.Event()
        self.start_time = 0
        self.live_dags_status = {}
        self.live_dags_lock = threading.Lock()
        self.completed_count = 0
        self.submitted_count = 0
        self.random_seed = random_seed
        random.seed(random_seed)
        np.random.seed(random_seed)
        self.current_phase_info = "Initializing..."
        rich_print(f" BOLD BLUE 🔮 : {random_seed} BOLD BLUE ")

    def _request_generator(self):
        local_random = random.Random(self.random_seed)
        local_np_random = np.random.RandomState(self.random_seed)
        rich_print(" BOLD CYAN 🚀 Request generator started... BOLD CYAN ")
        self.start_time = time.time()
        for i, (duration, lambda_rate) in enumerate(self.load_profile):
            if self.stop_event.is_set(): break
            phase_text = f"Phase {i+1}/{len(self.load_profile)}: Duration={duration}s, Rate={lambda_rate:.4f} req/s"
            with self.live_dags_lock:
                self.current_phase_info = phase_text
            rich_print(f"---  YELLOW {phase_text}  YELLOW  ---")
            stage_end_time = time.time() + duration
            while time.time() < stage_end_time:
                if self.stop_event.is_set(): break
                if lambda_rate > 0:
                    wait_time = local_np_random.exponential(1.0 / lambda_rate)
                    time.sleep(wait_time)
                else:
                    time.sleep(1)
                if time.time() >= stage_end_time: break
                self.request_queue.put(local_random.choice(self.all_queries))
        with self.live_dags_lock:
            self.current_phase_info = "Generation finished."
        self.stop_event.set()
        rich_print(f" BOLD CYAN 🏁 Request generator finished. BOLD CYAN ")

    def _monitoring_loop(self):
        while not self.stop_event.is_set():
            clear_console()
            rich_print("="*25 + "  BOLD WHITE task WHITE  BOLD  " + "="*25)
            with self.live_dags_lock:
                elapsed_runtime = time.time() - self.start_time if self.start_time > 0 else 0.0
                running_count = len(self.live_dags_status)
                phase_info = self.current_phase_info
                rich_print(f" WHITE --------------------------------- [ General ] ---------------------------------- WHITE ")
                rich_print(f": {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | : {elapsed_runtime:.1f}s | WHITE : {self.submitted_count} | : {running_count} | : {self.completed_count} WHITE ")
                rich_print(f" YELLOW Current Phase: {phase_info} YELLOW ")
                rich_print(f" WHITE --------------------------------- [ Live DAGs ] ---------------------------------- WHITE ")
                for i, (unique_id, data) in enumerate(self.live_dags_status.items()):
                    if i >= 10:
                        rich_print(f"  ... and {running_count - 10} more running ...")
                        break
                    dag_id, status_text, submit_time = data['dag_id'], data['status_text'], data['submit_time']
                    elapsed = time.time() - submit_time
                    rich_print(f"  CYAN DAG: {dag_id:<30} WHITE |  CYAN UUID: {unique_id:<37} WHITE |  CYAN : {status_text:<25} ({elapsed:.1f}s) WHITE ")
            time.sleep(1)

    def _worker(self, worker_id: int):
        """
        --- Worker ---
        """
        while not self.stop_event.is_set() or not self.request_queue.empty():
            handle = None # handledefine
            unique_id = None
            try:
                query = self.request_queue.get(timeout=1)
                submission_time = time.time()
                
                handle = self.client.submit_dag(query, submission_time)
                
                if not handle:
                    self._log_result(query["dag_id"], "SUBMIT_FAILED", submission_time, time.time())
                    continue
                
                unique_id = handle['run_id'] if isinstance(handle, dict) else handle
                with self.live_dags_lock:
                    self.submitted_count += 1
                    self.live_dags_status[unique_id] = {
                        'dag_id': query['dag_id'],
                        'status_text': 'Submitted...',
                        'submit_time': submission_time
                    }

                #

                while not self.stop_event.is_set():
                    # --- BUG FIX 2: task ---
                    status_text = self.client.get_status_text(handle)
                    with self.live_dags_lock:
                        if unique_id in self.live_dags_status:
                            self.live_dags_status[unique_id]['status_text'] = status_text
                    
                    if status_text.lower() == "finished":
                        self._log_result(query["dag_id"], "SUCCESS", submission_time, time.time())
                        self.client.get_and_print_results(handle)
                        break
                    time.sleep(10)
            except queue.Empty:
                continue
            except Exception as e:
                rich_print(f" RED [Worker {worker_id}] Error: {e} RED ")
            finally:
                # --- BUG FIX 3: task ---
                if unique_id:
                    with self.live_dags_lock:
                        if unique_id in self.live_dags_status:
                            del self.live_dags_status[unique_id]
                            self.completed_count += 1 #

                if handle:
                    self.client.release(handle)
                time.sleep(0.01)

    # _log_result, run, _analyze_results 
    def _log_result(self, dag_id: str, status: str, start_time: float, end_time: float):
        latency = end_time - start_time
        with self.results_lock:
            self.results.append({"dag_id": dag_id, "status": status, "latency": latency, "end_time": end_time})
            
    def run(self):
        generator_thread = threading.Thread(target=self._request_generator)
        worker_threads = [threading.Thread(target=self._worker, args=(i,)) for i in range(self.num_workers)]
        monitor_thread = threading.Thread(target=self._monitoring_loop)
        generator_thread.start()
        for t in worker_threads: t.start()
        monitor_thread.start()
        generator_thread.join()
        for t in worker_threads: t.join()
        self.stop_event.set()
        monitor_thread.join()

# ==========================================================================
#  Part 3:  ()
# ==========================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="")
    parser.add_argument("--target_system", type=str, required=True, choices=['ours', 'autogen', 'agentscope', 'vllm'], help=" ('ours'  'autogen')")
    parser.add_argument("--proj_path", default="/root/workspace/d23oa7cp420c73acue30/AgentOS", help="AgentOS")
    parser.add_argument("--seed", type=int, default=42, help=" (: 42)")
    parser.add_argument("--workers", type=int, default=20, help="")
    args = parser.parse_args()

    server_addresses = { 'ours': '127.0.0.1:6382', 'autogen': 'localhost:5002', 'agentscope': 'localhost:5002', 'vllm': 'localhost:5002'}
    target_addr = server_addresses.get(args.target_system)
    if not target_addr:
        rich_print(f" RED error:  '{args.target_system}' RED ")
        sys.exit(1)
        
    client = TaskLevelClient(master_addr=target_addr) if args.target_system == 'ours' else AgentLevelClient(master_addr=target_addr)

    AVG_DAG_COMPLETE_TIME= 45
    load_profile = [
        (600, (0.5 / AVG_DAG_COMPLETE_TIME)),
        (600, (0.75 / AVG_DAG_COMPLETE_TIME)),
        (600, (1.0 / AVG_DAG_COMPLETE_TIME)),
        (600, (1.25 / AVG_DAG_COMPLETE_TIME)),
        (600, (0.5 / AVG_DAG_COMPLETE_TIME))
        ]
    # load_profile = [
    #     (1800, (0.85 / AVG_DAG_COMPLETE_TIME))
    # ]

    rich_print(" BOLD BLUE 📂 ... BOLD BLUE ")
    all_queries = []
    dataset_paths = ["data/gaia/gaia_query.jsonl", "data/tbench/tbench_query.jsonl", "data/openagi/openagi_query.jsonl"]
    # dataset_paths = ["data/tbench/tbench_query.jsonl"]
    for rel_path in dataset_paths:
        full_path = os.path.join(args.proj_path, rel_path)
        if os.path.exists(full_path):
            all_queries.extend(load_dags_from_jsonl(full_path))
    if not all_queries: sys.exit(1)
    rich_print(f" BOLD GREEN ✅ request: {len(all_queries)} BOLD GREEN ")

    benchmark = BenchmarkRunner(
        client=client, all_queries=all_queries, load_profile=load_profile,
        num_workers=args.workers, random_seed=args.seed
    )
    benchmark.run()