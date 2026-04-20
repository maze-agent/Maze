import time
import requests
import json
from typing import List, Dict, Any, Optional
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
#  API ()
# --------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(description="Client for submitting and monitoring DAGs.")
    parser.add_argument("--master_addr", default="127.0.0.1:6382", #<-- 
                        help="Address (IP:port) of the master API server.")
    # ---  ---: 
    _default_root = str(Path(__file__).resolve().parents[3])
    parser.add_argument(
        "--proj_path",
        default=_default_root,
        help="Path to the maze-sc repository root (directory that contains src/core).",
    )
    # ---  ---: 
    parser.add_argument(
        "--query_file", 
        default="data/tbench/tbench_query.jsonl",
        help="Path to the query file relative to the project path."
    )
    return parser.parse_args()

class MasterApiClient:
    """
     master_api.py 
    DAGtask
    """
    def __init__(self, master_addr: str):
        self.base_url = f"http://{master_addr}"
        print(f"✅  Master API: {self.base_url}")

    def submit_dags(self, payload) -> List[Dict[str, Any]]:
        """ master_api.py DAGtask"""
        url = f"{self.base_url}/dag/"
        try:
            response = requests.post(url, json= payload, timeout=30)
            response.raise_for_status()
            res = response.json()
            
            #

            submitted = [dag for dag in res.get("data", []) if "error" not in dag]
            failed = [dag for dag in res.get("data", []) if "error" in dag]

            for dag_info in submitted:
                print(f"  -> ✅ : DAG ID '{dag_info['dag_id']}'")
            for dag_info in failed:
                print(f"  -> ❌ : DAG ID '{dag_info.get('dag_id')}', : {dag_info.get('error')}")

            return submitted
        except requests.exceptions.RequestException as e:
            print(f"❌ HTTP request: {e}")
            return []

    def check_dag_status(self, run_id: str) -> Optional[Dict[str, Any]]:
        """DAG"""
        url = f"{self.base_url}/status/"
        try:
            #  run_id
            response = requests.post(url, json={"run_id": run_id}, timeout=5)
            if response.status_code == 200:
                return response.json().get("data", {}).get("dag_status")
            return None
        except requests.exceptions.RequestException:
            return None

    def get_final_result(self, dag_info: Dict) -> Optional[Dict[str, Any]]:
        """DAG"""
        run_id = dag_info['run_id']
        task2id = dag_info['task2id']
        url = f"{self.base_url}/get/"
        results = {}
        print(f"\n🔧  DAG Run '{run_id}' ...")
        for task_name, task_id in task2id.items():
            #  run_id  task_id
            payload = {"run_id": run_id, "func_name": task_name, "task_id": task_id}
            try:
                response = requests.post(url, json=payload, timeout=10)
                if response.status_code == 200:
                    res = response.json()
                    if res.get("data") and "task_ret_data" in res["data"]:
                        try:
                           results[task_name] = json.loads(res['data']['task_ret_data'])
                        except (json.JSONDecodeError, TypeError):
                           results[task_name] = res['data']['task_ret_data']
                    else:
                        results[task_name] = f"Error: {res.get('msg')}"
                else:
                    results[task_name] = f"Error: Status {response.status_code}"
            except requests.exceptions.RequestException as e:
                 results[task_name] = f"Error: {e}"
        return results


    def release_dag(self, dag_info: Dict):
        """DAG"""
        url = f"{self.base_url}/release/"
        try:
            #  run_id  task2id
            payload = {"run_id": dag_info['run_id'], "dag_id": dag_info['dag_id'], "task2id": dag_info['task2id']}
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            print(f"  -> ✅ : Run ID '{dag_info['run_id']}'")
        except requests.exceptions.RequestException as e:
            print(f"  -> ❌ : Run ID '{dag_info['run_id']}', : {e}")



# --------------------------------------------------------------------------
#

# --------------------------------------------------------------------------

def load_dags_from_jsonl(file_path: str) -> List[Dict[str, Any]]:
    """
    --- new ---
     .jsonl fileDAGdefine
    JSONobjectss
    """
    print(f"\n📂 fileDAGdefine: {file_path}")
    dag_definitions = []
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    try:
                        dag_definitions.append(json.loads(line))
                    except json.JSONDecodeError:
                        print(f"⚠️ warning: : {line.strip()}")
        print(f"  ->  {len(dag_definitions)} DAGdefine")
        return dag_definitions
    except FileNotFoundError:
        print(f"❌ error: file!: {file_path}")
        return []

# --------------------------------------------------------------------------
#

# --------------------------------------------------------------------------

def clear_console():
    """"""
    os.system('cls' if os.name == 'nt' else 'clear')

def monitor_and_process_dags(client: MasterApiClient, dags: List[Dict[str, Any]], poll_interval: int):
    # --- MODIFICATION START ---
    #  run_id 
    running_dags = {dag['run_id']: dag for dag in dags}
    dag_progress = {dag['run_id']: "Pending..." for dag in running_dags.values()}
    # --- MODIFICATION END ---
    start_time=time.time()
    total_dags=len(running_dags)

    while running_dags:
        clear_console()
        print("="*20 + " task " + "="*20)
        print(f": {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | /: {len(running_dags)}/{total_dags}")
        print("-"*65)
        
        finished_this_poll = set()

        # --- MODIFICATION START ---
        for run_id, dag_info in running_dags.items():
            status_dict = client.check_dag_status(run_id)
            
            if status_dict:
                total = len(status_dict)
                completed = sum(1 for task in status_dict.values() if task.get("status") != "unfinished")
                dag_progress[run_id] = f"{completed}/{total} tasks completed"
                if completed == total and total > 0:
                    finished_this_poll.add(run_id)
            
            # IDID
            print(f"  DAG: {dag_info['dag_id']:<38} | Run ID: {run_id:<38} | : {dag_progress[run_id]}")

        print("-"*65)
        
        if finished_this_poll:
            print("\ntaskhandle...")
            for run_id in finished_this_poll:
                dag_to_process = running_dags.pop(run_id)
                final_results = client.get_final_result(dag_to_process)
                
                print(f"\n--- emit for DAG '{dag_to_process['dag_id']}' (Run: {run_id}) ---")
                if final_results:
                    pretty_result = json.dumps(final_results, indent=4, ensure_ascii=False)
                    print(pretty_result)
                else:
                    print("")
                print("---------------------------------------------------------")
                
                client.release_dag(dag_to_process)

        if running_dags:
            time.sleep(poll_interval)
    total_time = time.time() - start_time
    print(f"\n✅✅✅ DAGtaskfinished: {total_time:.2f} s")

# --------------------------------------------------------------------------
#

# --------------------------------------------------------------------------
if __name__ == "__main__":
    args = parse_args()

    # # 1. DAG
    query_file_full_path = os.path.join(args.proj_path, args.query_file)
    dag_definitions = load_dags_from_jsonl(query_file_full_path)

    if not dag_definitions:
        print("\nfileDAG")
        sys.exit(1)

    # #2. payload
    submission_payload = {
        "dag_ids": [d["dag_id"] for d in dag_definitions],
        "dag_sources": [d["dag_source"] for d in dag_definitions],
        "dag_types": [d["dag_type"] for d in dag_definitions],
        "dag_supplementary_files": [d["dag_supplementary_files"] for d in dag_definitions],
        "sub_time": time.time()
    }



    # dag_ids = [
        # "0c4f9fd8-01c4-4fbe-b933-3570a3cd771a",
        # "0d2ec70b-46f3-4c95-8172-c383c7539a94",
        # "e39a422f-f24c-4cde-97f8-790b69507962",
        # "0a33f7a3-5cfa-42c7-8cab-19260908720b"
    # ]
    # submission_payload = {
    #     "dag_ids": dag_ids,
    #     "dag_sources": ["openagi"] * len(dag_ids),
    #     "dag_types": [
            # "document_qa",
            # "image_captioning_complex",
            # "multimodal_vqa_complex",
            # "text_processing_multilingual"
            # ], # , "vision", "speech", "file", "reason"
        # "dag_supplementary_files": [
            # ["context.txt", "question.txt", "questions.txt"],
            # ["images/27.jpg", "images/28.jpg", "images/29.jpg", "images/3.jpg", "images/30.jpg", "images/31.jpg", "images/32.jpg", "images/33.jpg", "images/34.jpg", "images/35.jpg", "images/36.jpg", "images/37.jpg", "images/38.jpg", "images/39.jpg", "images/4.jpg", "images/40.jpg", "images/41.jpg", "images/42.jpg", "images/43.jpg", "images/44.jpg", "question.txt"],
            # ["images/27.jpg", "images/28.jpg", "images/29.jpg", "images/3.jpg", "images/30.jpg", "images/31.jpg", "images/32.jpg", "images/33.jpg", "images/34.jpg", "images/35.jpg", "images/36.jpg", "images/37.jpg", "images/38.jpg", "images/39.jpg", "images/4.jpg", "images/40.jpg", "images/41.jpg", "images/42.jpg", "images/43.jpg", "images/44.jpg", "question.txt"],
            # ["question.txt", "text.txt"]
        # ],
        # "sub_time": time.time()
    # }

    # dag_ids = [
    #     "9a9e376c-0089-4a3e-8480-f05df35ae465",
    #     "0a3f0429-68e2-4343-94d1-d7e0b30abb39",
    #     "55f2e5e7-6a59-4802-9fa8-179f1a6a4e85",
    #     "d53b77e9-9ef1-4289-ae9e-25f31fcff6ac",
    #     "faff32d4-f4cf-4ff7-a504-2ea31f58830f",
    #     "db912f98-84ad-4591-90d3-f3087eaea832"
    # ]
    # submission_payload = {
    #     "dag_ids": dag_ids,
    #     "dag_sources": ["tbench"] * len(dag_ids),
    #     "dag_types": ["airline_book", "airline_cancel", "retail_cancel", "retail_cancel_modify", "retail_modify", "retail_return"], # "airline_book", "airline_cancel", "retail_cancel", "retail_cancel_modify", "retail_modify", "retail_return"
    #     "dag_supplementary_files": [
    #         ["flights.json", "users.json", "reservations.json"],
    #         ["flights.json", 'users.json', 'reservations.json'],
    #         ["products.json", "users.json", "orders.json"],
    #         ["products.json", "users.json", "orders.json"],
    #         ["products.json", "users.json", "orders.json"],
    #         ["products.json", "users.json", "orders.json"]
    #     ],
    #     "sub_time": time.time()
    # }

    dag_ids = [
        "99c9cc74-fdc8-46c6-8f8d-3ce2d3bfeea3",
        "df6561b2-7ee5-4540-baab-5095f742716a",
        "67e8878b-5cef-4375-804e-e6291fdbe78a",
        "ec09fa32-d03f-4bf8-84b0-1f16922c3ae4",
    ]
    submission_payload = {
        "dag_ids": dag_ids,
        "dag_sources": ["gaia"] * len(dag_ids),
        "dag_types": ["speech", "vision", "file", "reason"], # "speech", "vision", "file", "reason"
        "dag_supplementary_files": [
            ["99c9cc74-fdc8-46c6-8f8d-3ce2d3bfeea3.mp3"],
            ["df6561b2-7ee5-4540-baab-5095f742716a.png"],
            ["67e8878b-5cef-4375-804e-e6291fdbe78a.pdf"],
            [],
        ],
        "sub_time": time.time()
    }
    
    # submission_payload = {
    #     "dag_ids": [
    #         "9a9e376c-0089-4a3e-8480-f05df35ae465",
    #         "9a9e376c-0089-4a3e-8480-f05df35ae465",
    #         "9a9e376c-0089-4a3e-8480-f05df35ae465",
    #         "9a9e376c-0089-4a3e-8480-f05df35ae465",
    #         "9a9e376c-0089-4a3e-8480-f05df35ae465",
    #         "9a9e376c-0089-4a3e-8480-f05df35ae465",
    #         "9a9e376c-0089-4a3e-8480-f05df35ae465",
    #         "9a9e376c-0089-4a3e-8480-f05df35ae465",
    #         "9a9e376c-0089-4a3e-8480-f05df35ae465",
    #         "9a9e376c-0089-4a3e-8480-f05df35ae465",
    #         "9a9e376c-0089-4a3e-8480-f05df35ae465",
    #         "9a9e376c-0089-4a3e-8480-f05df35ae465",
    #         "9a9e376c-0089-4a3e-8480-f05df35ae465",
    #         "9a9e376c-0089-4a3e-8480-f05df35ae465"
    #     ],
    #     "dag_sources": [
    #         "tbench", "tbench", "tbench", "tbench",
    #         "tbench", "tbench", "tbench", "tbench", "tbench", "tbench",
    #         "tbench", "tbench", "tbench", "tbench"
    #     ],
    #     "dag_types": [
    #         "airline_book",
    #         "airline_book",
    #         "airline_book",
    #         "airline_book",
    #         "airline_book",
    #         "airline_book",
    #         "airline_book",
    #         "airline_book",
    #         "airline_book",
    #         "airline_book",
    #         "airline_book",
    #         "airline_book",
    #         "airline_book",
    #         "airline_book"
    #     ],
    #     "dag_supplementary_files": [
    #         ["flights.json", "users.json", "reservations.json"],
    #         ["flights.json", "users.json", "reservations.json"],
    #         ["flights.json", "users.json", "reservations.json"],
    #         ["flights.json", "users.json", "reservations.json"],
    #         ["flights.json", "users.json", "reservations.json"],
    #         ["flights.json", 'users.json', 'reservations.json'],
    #         ["flights.json", "users.json", "reservations.json"],
    #         ["flights.json", "users.json", "reservations.json"],
    #         ["flights.json", "users.json", "reservations.json"],
    #         ["flights.json", "users.json", "reservations.json"],
    #         ["flights.json", "users.json", "reservations.json"],
    #         ["flights.json", "users.json", "reservations.json"],
    #         ["flights.json", "users.json", "reservations.json"],
    #         ["flights.json", "users.json", "reservations.json"]
    #     ],
    #     "sub_time": time.time()
    # }


    # submission_payload = {
    #     "dag_ids": [
    #         "0c4f9fd8-01c4-4fbe-b933-3570a3cd771a", # 
    #         "0d2ec70b-46f3-4c95-8172-c383c7539a94", # 
    #         "e39a422f-f24c-4cde-97f8-790b69507962", # 
    #         "0a33f7a3-5cfa-42c7-8cab-19260908720b", # 
    #         "9a9e376c-0089-4a3e-8480-f05df35ae465",
    #         "0a3f0429-68e2-4343-94d1-d7e0b30abb39", # 
    #         "55f2e5e7-6a59-4802-9fa8-179f1a6a4e85", # 
    #         "d53b77e9-9ef1-4289-ae9e-25f31fcff6ac", # 
    #         "faff32d4-f4cf-4ff7-a504-2ea31f58830f", # 
    #         "db912f98-84ad-4591-90d3-f3087eaea832", # 
    #         "99c9cc74-fdc8-46c6-8f8d-3ce2d3bfeea3", # 
    #         "df6561b2-7ee5-4540-baab-5095f742716a", # 
    #         "67e8878b-5cef-4375-804e-e6291fdbe78a", # 
    #         "ec09fa32-d03f-4bf8-84b0-1f16922c3ae4" #
    #     ],
    #     "dag_sources": [
    #         "openagi", "openagi", "openagi", "openagi",
    #         "tbench", "tbench", "tbench", "tbench", "tbench", "tbench",
    #         "gaia", "gaia", "gaia", "gaia"
    #     ],
    #     "dag_types": [
    #         "document_qa",
    #         "image_captioning_complex",
    #         "multimodal_vqa_complex",
    #         "text_processing_multilingual",
    #         "airline_book",
    #         "airline_cancel",
    #         "retail_cancel",
    #         "retail_cancel_modify",
    #         "retail_modify",
    #         "retail_return",
    #         "speech",
    #         "vision",
    #         "file",
    #         "reason"
    #     ],
    #     "dag_supplementary_files": [
    #         ["context.txt", "question.txt", "questions.txt"],
    #         ["images/27.jpg", "images/28.jpg", "images/29.jpg", "images/3.jpg", "images/30.jpg", "images/31.jpg", "images/32.jpg", "images/33.jpg", "images/34.jpg", "images/35.jpg", "images/36.jpg", "images/37.jpg", "images/38.jpg", "images/39.jpg", "images/4.jpg", "images/40.jpg", "images/41.jpg", "images/42.jpg", "images/43.jpg", "images/44.jpg", "question.txt"],
    #         ["images/27.jpg", "images/28.jpg", "images/29.jpg", "images/3.jpg", "images/30.jpg", "images/31.jpg", "images/32.jpg", "images/33.jpg", "images/34.jpg", "images/35.jpg", "images/36.jpg", "images/37.jpg", "images/38.jpg", "images/39.jpg", "images/4.jpg", "images/40.jpg", "images/41.jpg", "images/42.jpg", "images/43.jpg", "images/44.jpg", "question.txt"],
    #         ["question.txt", "text.txt"],
    #         ["flights.json", "users.json", "reservations.json"],
    #         ["flights.json", 'users.json', 'reservations.json'],
    #         ["products.json", "users.json", "orders.json"],
    #         ["products.json", "users.json", "orders.json"],
    #         ["products.json", "users.json", "orders.json"],
    #         ["products.json", "users.json", "orders.json"],
    #         ["99c9cc74-fdc8-46c6-8f8d-3ce2d3bfeea3.mp3"],
    #         ["df6561b2-7ee5-4540-baab-5095f742716a.png"],
    #         ["67e8878b-5cef-4375-804e-e6291fdbe78a.pdf"],
    #         []
    #     ],
    #     "sub_time": time.time()
    # }
    # 3. 
    client = MasterApiClient(master_addr=args.master_addr)
    submitted_dags = client.submit_dags(submission_payload)

    # 4. 
    if submitted_dags:
        monitor_and_process_dags(client, submitted_dags, poll_interval= 20)

    print(f"\n🎉 finished")