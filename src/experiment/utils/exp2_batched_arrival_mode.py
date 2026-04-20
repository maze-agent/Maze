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

# ==========================================================================
#  Part 0: Helper Functions
# ==========================================================================
def clear_console():
    """"""
    os.system('cls' if os.name == 'nt' else 'clear')

def rich_print(text: str):
    """emit"""
    color_map = {"RED": "\033[91m", "GREEN": "\033[92m", "YELLOW": "\033[93m", "BLUE": "\033[94m", "MAGENTA": "\033[95m", "CYAN": "\033[96m", "WHITE": "\033[97m", "BOLD": "\033[1m", "END": "\033[0m"}
    for key, value in color_map.items():
        text = text.replace(key, value)
    print(text + color_map["END"])

def load_dags_from_jsonl(file_path: str) -> List[Dict[str, Any]]:
    """jsonlfileDAGdefine"""
    dag_definitions = []
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    try:
                        dag_definitions.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return dag_definitions
    except FileNotFoundError:
        rich_print(f"❌ RED Error: Query file not found at {file_path}. RED")
        return []

# ==========================================================================
#  Part 1: API Clients
# ==========================================================================
class TaskLevelClient:
    """Westworld (Task-level) API"""
    def __init__(self, master_addr: str):
        self.base_url = f"http://{master_addr}"
        rich_print(f"✅  WHITE Initialized client for Westworld at {self.base_url} WHITE ")

    def submit_batch(self, queries: List[Dict], sub_time: float) -> List[Dict]:
        """viaAPIDAG"""
        payload = {
            "dag_ids": [q["dag_id"] for q in queries],
            "dag_sources": [q["dag_source"] for q in queries],
            "dag_types": [q["dag_type"] for q in queries],
            "dag_supplementary_files": [q["dag_supplementary_files"] for q in queries],
            "sub_time": sub_time
        }
        url = f"{self.base_url}/dag/"
        try:
            response = requests.post(url, json=payload, timeout=60)
            response.raise_for_status()
            res = response.json()
            submitted = [dag for dag in res.get("data", []) if "error" not in dag]
            return submitted
        except requests.exceptions.RequestException as e:
            rich_print(f" RED Batch submission failed: {e} RED ")
            return []

    def get_status_text(self, handle: Dict) -> str:
        run_id = handle['run_id']
        url = f"{self.base_url}/status/"
        try:
            response = requests.post(url, json={"run_id": run_id}, timeout=5)
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

    def get_and_print_results(self, handle: Dict):
        """DAG"""
        run_id = handle['run_id']
        dag_id = handle['dag_id']
        task2id = handle['task2id']
        url = f"{self.base_url}/get/"
        results = {}
        
        rich_print(f"\n BOLD MAGENTA 🔧 Getting results for DAG '{dag_id}' (Run: {run_id})... BOLD MAGENTA ")
        for task_name, task_id in task2id.items():
            payload = {"run_id": run_id, "func_name": task_name, "task_id": task_id}
            try:
                response = requests.post(url, json=payload, timeout=20)
                if response.status_code == 200:
                    res = response.json()
                    if res.get("data") and "task_ret_data" in res["data"]:
                        try: results[task_name] = json.loads(res['data']['task_ret_data'])
                        except (json.JSONDecodeError, TypeError): results[task_name] = res['data']['task_ret_data']
                    else: results[task_name] = f"Error: {res.get('msg')}"
                else: results[task_name] = f"Error: Status {response.status_code}"
            except requests.exceptions.RequestException as e:
                results[task_name] = f"Error: {e}"
        
        pretty_result = json.dumps(results, indent=4, ensure_ascii=False)
        rich_print(f" BOLD GREEN --- Final Output for DAG '{dag_id}' --- BOLD GREEN \n{pretty_result}\n BOLD GREEN ----------------------------------------- BOLD GREEN ")

    def release(self, handle: Dict):
        url = f"{self.base_url}/release/"
        try:
            payload = {"run_id": handle['run_id'], "dag_id": handle['dag_id'], "task2id": handle['task2id']}
            requests.post(url, json=payload, timeout=10)
        except requests.exceptions.RequestException: pass

class AgentLevelClient:
    """ Agent-level API"""
    def __init__(self, master_addr: str):
        self.base_url = f"http://{master_addr}"
        rich_print(f"✅  WHITE Initialized client for Agent-level system at {self.base_url} WHITE ")

    def submit_batch(self, queries: List[Dict], sub_time: float) -> List[Dict]:
        """viaAPIDAG"""
        payload = {
            "dag_ids": [q["dag_id"] for q in queries],
            "dag_sources": [q["dag_source"] for q in queries],
            "dag_types": [q["dag_type"] for q in queries],
            "dag_supplementary_files": [q["dag_supplementary_files"] for q in queries],
            "sub_time": sub_time
        }
        url = f"{self.base_url}/submit_dag"
        try:
            response = requests.post(url, json=payload, timeout=60)
            response.raise_for_status()
            res = response.json()
            # Agent-levelhandleuuidstringquery
            submitted_handles = []
            submitted_responses = res.get("submitted", [])
            for i, resp in enumerate(submitted_responses):
                if "error" not in resp:
                    # uuidqueryhandle
                    handle = {'uuid': resp.get('uuid'), 'query': queries[i]}
                    submitted_handles.append(handle)
            return submitted_handles
        except requests.exceptions.RequestException as e:
            rich_print(f" RED Batch submission failed: {e} RED ")
            return []

    def get_status_text(self, handle: Dict) -> str:
        uuid = handle['uuid']
        try:
            response = requests.get(f"{self.base_url}/dag_status/{uuid}", timeout=5)
            if response.status_code == 200:
                status = response.json().get("status", "Unknown")
                return "Finished" if status.lower() == "finished" else status.capitalize()
            return "Polling status..."
        except requests.exceptions.RequestException:
            return "Connection error..."

    def get_and_print_results(self, handle: Dict):
        """ DAG """
        uuid = handle['uuid']
        rich_print(f"\n BOLD MAGENTA 🔧 Getting results for DAG (UUID: {uuid})... BOLD MAGENTA ")
        try:
            response = requests.get(f"{self.base_url}/get_final_result/{uuid}", timeout=20)
            if response.status_code == 200:
                result_data = response.json()
                pretty_result = json.dumps(result_data, indent=4, ensure_ascii=False)
                rich_print(f" BOLD GREEN --- Final Output for DAG (UUID: {uuid}) --- BOLD GREEN \n{pretty_result}\n BOLD GREEN ----------------------------------------- BOLD GREEN ")
            else:
                rich_print(f"  -> ⚠️  YELLOW Failed to get results for {uuid}: Status {response.status_code} YELLOW ")
        except requests.exceptions.RequestException as e:
            rich_print(f"❌  RED Network error getting results for {uuid}: {e} RED ")

    def release(self, handle: Any):
        pass

# ==========================================================================
#  Part 2:  (BenchmarkRunner - Batched Version)
# ==========================================================================
class BenchmarkRunner:
    def __init__(self, client: Any, all_queries: List[Dict], batch_size: int, random_seed: int):
        self.client = client
        self.all_queries = all_queries
        self.batch_size = batch_size
        self.random_seed = random_seed
        self.stop_event = threading.Event()
        self.start_time = 0
        self.live_dags_status = {}
        self.live_dags_lock = threading.Lock()
        self.completed_count = 0
        self.submitted_count = 0
        self.current_phase_info = "Waiting to start..."

    def _monitoring_loop(self):
        """"""
        while not self.stop_event.is_set():
            clear_console()
            rich_print("="*25 + "  BOLD WHITE Batched Arrival - task WHITE  BOLD  " + "="*25)
            with self.live_dags_lock:
                running_count = len(self.live_dags_status)
                phase_info = self.current_phase_info
                elapsed_runtime = time.time() - self.start_time if self.start_time > 0 else 0.0

                rich_print(f" WHITE --------------------------------- [ General ] ---------------------------------- WHITE ")
                rich_print(f": {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | : {elapsed_runtime: >7.1f}s |  WHITE : {self.submitted_count}/{self.batch_size} | : {running_count} | : {self.completed_count} WHITE ")
                rich_print(f" YELLOW Status: {phase_info} YELLOW ")
                rich_print(f" WHITE --------------------------------- [ Live DAGs ({running_count} Running) ] ------------------------- WHITE ")
                
                for i, (unique_id, data) in enumerate(list(self.live_dags_status.items())):
                    dag_id, status_text = data['dag_id'], data['status_text']
                    rich_print(f"  CYAN DAG: {dag_id:<30} WHITE |  CYAN Handle: {str(unique_id):<20} WHITE |  CYAN : {status_text:<25} WHITE ")
            time.sleep(1)

    def _poll_and_get_results(self, handle: Any, query: Dict):
        """
        task
        """
        # Task-leveldict, Agent-leveldict()
        unique_id = handle.get('run_id') or handle.get('uuid')
        dag_id = query['dag_id']
        
        try:
            with self.live_dags_lock:
                self.live_dags_status[unique_id] = {'dag_id': dag_id, 'status_text': 'Polling...'}

            while not self.stop_event.is_set():
                status_text = self.client.get_status_text(handle)
                with self.live_dags_lock:
                    if unique_id in self.live_dags_status:
                        self.live_dags_status[unique_id]['status_text'] = status_text
                
                if status_text == "Finished":
                    self.client.get_and_print_results(handle)
                    break
                
                if "error" in status_text.lower():
                    self.client.get_and_print_results(handle)
                    break
                    
                time.sleep(5)
        
        except Exception as e:
            rich_print(f" RED [Polling Thread for {unique_id}] Unhandled Exception: {e} RED ")
        finally:
            with self.live_dags_lock:
                if unique_id in self.live_dags_status:
                    del self.live_dags_status[unique_id]
                self.completed_count += 1
            self.client.release(handle)

    def run(self):
        """parallel"""
        rich_print(" BOLD CYAN 🚀 Preparing requests... BOLD CYAN ")
        local_random = random.Random(self.random_seed)
        if len(self.all_queries) < self.batch_size:
            rich_print(f" RED Error: Batch size ({self.batch_size}) is larger than query pool. RED")
            return
        batch_queries = local_random.sample(self.all_queries, self.batch_size)
        rich_print(f" BOLD CYAN 🏁 {self.batch_size} queries ready. BOLD CYAN ")

        monitor_thread = threading.Thread(target=self._monitoring_loop, daemon=True)
        monitor_thread.start()

        self.start_time = time.time()
        self.current_phase_info = "Submitting batch requests in a single API call..."
        
        # task
        submitted_handles = self.client.submit_batch(batch_queries, self.start_time)
        
        self.submitted_count = len(submitted_handles)
        self.current_phase_info = f"{self.submitted_count} requests submitted, now polling for results..."
        
        # task
        polling_threads = []
        for handle in submitted_handles:
            # handlequery
            if isinstance(self.client, TaskLevelClient):
                original_query = next((q for q in batch_queries if q['dag_id'] == handle['dag_id']), None)
            else: # AgentLevelClient
                original_query = handle['query']

            if original_query:
                thread = threading.Thread(target=self._poll_and_get_results, args=(handle, original_query))
                polling_threads.append(thread)
                thread.start()

        #

        for t in polling_threads:
            t.join()

        #

        self.stop_event.set()
        time.sleep(1.1)
        monitor_thread.join()
        
        rich_print("\n" + "="*80)
        rich_print(" BOLD GREEN ✅ Benchmark finished. All tasks processed. BOLD GREEN ")
        rich_print("="*80)

# ==========================================================================
#  Part 3: 
# ==========================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="handle")
    parser.add_argument("--target_system", type=str, required=True, choices=['ours', 'autogen', 'agentscope', 'vllm'], help="")
    parser.add_argument("--proj_path", default="/root/workspace/d23oa7cp420c73acue30/AgentOS", help="")
    parser.add_argument("--seed", type=int, default=42, help=" (: 42, 43, 44)")
    parser.add_argument("--batch_size", type=int, default=40, help="handletask")
    args = parser.parse_args()

    server_addresses = { 'ours': '127.0.0.1:6382', 'autogen': 'localhost:5002', 'agentscope': 'localhost:5002', 'vllm': 'localhost:5002'}
    target_addr = server_addresses.get(args.target_system)
    if not target_addr:
        rich_print(f" RED error:  '{args.target_system}' RED ")
        sys.exit(1)
        
    client = TaskLevelClient(master_addr=target_addr) if args.target_system == 'ours' else AgentLevelClient(master_addr=target_addr)

    rich_print(" BOLD BLUE 📂 ... BOLD BLUE ")
    all_queries = []
    dataset_paths = ["data/gaia/gaia_query.jsonl"] # , "data/tbench/tbench_query.jsonl", "data/openagi/openagi_query.jsonl"
    for rel_path in dataset_paths:
        full_path = os.path.join(args.proj_path, rel_path)
        if os.path.exists(full_path):
            all_queries.extend(load_dags_from_jsonl(full_path))
    
    if not all_queries:
        rich_print(" RED error:  --proj_path file RED ")
        sys.exit(1)
        
    rich_print(f" BOLD GREEN ✅ request: {len(all_queries)} BOLD GREEN ")

    benchmark = BenchmarkRunner(
        client=client, all_queries=all_queries, batch_size=args.batch_size,
        random_seed=args.seed
    )
    benchmark.run()
