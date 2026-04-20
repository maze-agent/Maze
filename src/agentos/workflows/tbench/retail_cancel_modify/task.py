import ray
import json
import os
import re
import gc
import sys
import dashscope
from agentos.scheduler import gpu, io, cpu
from typing import Dict, Any, List
import time
from agentos.utils.tbench_tools.retail.find_user_id_by_email import FindUserIdByEmail
from agentos.utils.tbench_tools.retail.find_user_id_by_name_zip import FindUserIdByNameZip
from agentos.utils.tbench_tools.retail.get_user_details import GetUserDetails
from agentos.utils.tbench_tools.retail.get_order_details import GetOrderDetails
from agentos.utils.tbench_tools.retail.cancel_pending_order import CancelPendingOrder
from agentos.utils.tbench_tools.retail.modify_pending_order_items import ModifyPendingOrderItems
from agentos.utils.tbench_tools.retail.list_all_product_types import ListAllProductTypes
from agentos.utils.tbench_tools.common import _find_item_details_in_order, _find_new_product_variant_id
from agentos.utils.remote_llm_route import RemoteLlmRoute

def estimate_tokens(text):
    """
    A slightly more accurate tokenizer estimator for mixed Chinese/English text.
    It counts CJK characters and non-CJK tokens separately to avoid double counting.
    """
    # 1. CJK
    cjk_chars = sum(1 for char in text if '\u4E00' <= char <= '\u9FFF')
    # 2. CJK
    # CJK
    non_cjk_text = re.sub(r'[\u4E00-\u9FFF]', ' ', text)
    non_cjk_text = non_cjk_text.replace("\n", " ")
    # 3. CJK
    # usesplit()
    non_cjk_words_count = len(non_cjk_text.split())
    # 4. 
    # 1.3token
    estimated_tokens = cjk_chars + int(non_cjk_words_count * 1.3)
    return estimated_tokens

def query_vllm_model(
    endpoint: RemoteLlmRoute,
    model_alias: str,
    messages: List,
    temperature: float = 0.6,
    max_token: int = 1024,
    top_p: float = 0.9,
    repetition_penalty: float = 1.1,
) -> tuple[dict, str]:
    """Call a colocated vLLM worker via its OpenAI-compatible ``/v1/chat/completions`` route."""
    import requests
    from rich.console import Console
    console = Console()
    chat_url = endpoint.completions_url
    headers = endpoint.headers()
    # Build OpenAI-compatible payload
    payload = {
        "model": model_alias,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_token,
        "top_p": top_p,
        "repetition_penalty": repetition_penalty,
    }
    #

    conversation = ""
    for message in messages:
        conversation += f"{message['role']}: {message['content']}"
    try:
        response = requests.post(chat_url, json=payload, headers=headers, timeout=3600)
        response.raise_for_status()  # raise_for_status on failure
        
        response_data = response.json()
        content = response_data['choices'][0]['message']['content'].lstrip()
        
        # queryaligned
        features = {"text_length": len(conversation), "token_count": estimate_tokens(conversation)}
        return features, content

    except requests.exceptions.RequestException as e:
        error_msg = f"vLLM request failed: {str(e)}"
        console.print(f"[bold red]{error_msg}")
        features = {"text_length": len(conversation), "token_count": estimate_tokens(conversation)}
        return features, f"[bold red]{error_msg}"

def query_llm(model:str, model_folder: str, messages:List, temperature= 0.6, max_token= 1024, top_p= 0.9, repetition_penalty= 1.1):
    from transformers import AutoTokenizer, AutoModelForCausalLM
    import torch
    # Load tokenizer and weights
    tokenizer_path= os.path.join(model_folder, "Qwen/Qwen3-32B")
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_path, device_map= "auto")
    local_model = AutoModelForCausalLM.from_pretrained(
        os.path.join(model_folder, model),
        torch_dtype="float16",
        low_cpu_mem_usage=True,
        device_map= "auto"
        )

    # Flatten messages to a string
    conversation = ""
    for message in messages:
        conversation += f"{message['role']}: {message['content']}"

    # Tokenize prompt
    input_ids = tokenizer.encode(conversation + tokenizer.eos_token, return_tensors='pt').to("cuda")

    # Generate completion
    output = local_model.generate(
        input_ids, 
        pad_token_id=tokenizer.eos_token_id,
        max_new_tokens= max_token,
        temperature= temperature, 
        top_p= top_p,
        repetition_penalty= repetition_penalty
    )
    response = tokenizer.decode(output[:, input_ids.shape[-1]:][0], skip_special_tokens=True)

    del local_model
    del tokenizer
    torch.cuda.empty_cache()
    gc.collect()
    return {"text_length": len(conversation), "token_count": estimate_tokens(conversation)}, response

def query_llm_online(endpoint: RemoteLlmRoute, payload: Dict[str, str], tokenizer_path: str) -> tuple:
    """POST to a hosted OpenAI-compatible chat endpoint (credentials live on the route object)."""
    import requests
    from rich.console import Console
    conversation = ""
    for message in payload["messages"]:
        conversation += f"{message['role']}: {message['content']}"

    console = Console()
    headers = endpoint.headers()
    try:
        response = requests.post(
            endpoint.completions_url,
            json=payload,
            headers=headers,
            timeout=600,
        )
        response.raise_for_status()
        return {"text_length": len(conversation), "token_count": estimate_tokens(conversation)}, response.json()['choices'][0]['message']['content'].lstrip()
    except Exception as e:
        console.print(f"[bold red]API request failed: {str(e)}")
        return {"text_length": len(conversation), "token_count": estimate_tokens(conversation)}, f"[bold red]API request failed: {str(e)}"

def _extract_json_from_llm_output(llm_output: str) -> str:
    """
    Extract a JSON payload from LLM output.
    Supports raw JSON, fenced JSON, and annotated JSON.
    """
    import re
    
    # JSON
    try:
        json.loads(llm_output.strip())
        return llm_output.strip()
    except:
        pass
    
    # JSON
    json_block_pattern = r'```(?:json)?\s*(\{.*?\})\s*```'
    match = re.search(json_block_pattern, llm_output, re.DOTALL)
    if match:
        try:
            json.loads(match.group(1))
            return match.group(1)
        except:
            pass
    
    # JSONobject
    json_pattern = r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}'
    matches = re.findall(json_pattern, llm_output, re.DOTALL)
    for match in matches:
        try:
            json.loads(match)
            return match
        except:
            continue
    
    # emit
    return llm_output.strip()

# --- taskdefine ---
@io(mem= 1024)
def task0_init(context):
    """
    Workflow step 0: initialize environment and context.
    1.Load backend datasets (flights, users, reservations).
    2.Store the raw user instruction and id in context.
    """
    start_time= time.time()
    print("--- Running Task 0: Initialize environment ---")
    try:
        dag_id = ray.get(context.get.remote("dag_id"))
        instruction = ray.get(context.get.remote("question"))
        
        #

        print(f"DAG ID: {dag_id}")
        print(f"Question field: {instruction}")
        print(f"Question field type: {type(instruction)}")
        print(f"Question field length: {len(instruction) if instruction else 0}")
        if not instruction:
            print("⚠️ warning: questionfield empty")
            raise ValueError(f"task {dag_id}  question field is empty")

        print(f"Received instruction: {instruction}")

        # tau-benchjson
        try:
            supplementary_files = ray.get(context.get.remote("supplementary_files"))
            products_data, users_data, orders_data= json.loads(supplementary_files['products.json']), json.loads(supplementary_files['users.json']), json.loads(supplementary_files['orders.json'])
            backend_data = {
                "products": products_data,
                "users": users_data,
                "orders": orders_data
            }
            context.put.remote("backend_data", backend_data)
            context.put.remote("instruction", instruction)
            print("--- Task 0 finished: environment ready ---")

            prompt = f"""
            You are a top-tier retail order processing assistant. Carefully review the user's instructions and return a single JSON object containing all operation details in strict JSON format.

            The JSON object should include the following optional fields: "user_info", "cancellation", "modification".

            1. "user_info": (object) [if provided] Contains user information.
            - "email": (string)
            - "user_name": (string)
            - "zip_code": (string)

            2. "cancellation": (object) [if cancellation requested]
            - "order_id": (string) Order ID to cancel (starts with "#").
            - "reason": (string) [optional] Cancellation reason.

            3. "modification": (object) [if modification requested]
            - "order_id": (string) Order ID to modify.
            - "item_to_modify": (object) Item to modify, including "name" and "attributes".
            - "new_item_spec": (object) New item specifications (only includes changed "attributes").
            - "payment_method_id": (string) [optional] Payment method ID.

            User instructions:
            "{instruction}"

            JSON output:
            """
            llm_process_feat= {"text_length": len(prompt), "token_count": estimate_tokens(prompt)}

            return json.dumps({
                "dag_id": dag_id,
                "succ_task_feat": {
                    "task1_llm_process": {"text_length": llm_process_feat["text_length"], "token_count": llm_process_feat["token_count"], "reason": 0}
                },
                "curr_task_feat": None,
                "start_time": start_time,
                "end_time": time.time()
            })
        except FileNotFoundError as e:
            print(f"error:data file not found{e}")
            raise
    except Exception as e:
        print(f"task0_init error: {str(e)}")
        return json.dumps({
            "dag_id": dag_id,
            "start_time": start_time,
            "end_time": time.time()
        })

@gpu(gpu_mem= 70000, model_name= "qwen3-32b", backend="huggingface")
def task1_llm_process(context):
    """
    Workflow step 1: use an LLM to extract mixed cancel/modify intent.
    Uses the same LLM inference pattern as file/task.py.
    """
    print("--- Running Task 1: LLM ---")
    try:
        backend= task1_llm_process._task_decorator["backend"]
        print(f"✅ LLMstart....")
        start_time = time.time()  #

        
        #

        dag_id = ray.get(context.get.remote("dag_id"))
        instruction = ray.get(context.get.remote("instruction"))

        use_online_model= ray.get(context.get.remote("use_online_model"))
        model_folder= ray.get(context.get.remote("model_folder"))
        tokenizer_path= os.path.join(model_folder, "Qwen/Qwen3-32B")
        temperature, max_token, repetition_penalty, top_p= None, None, None, None
        if not use_online_model:
            temperature= ray.get(context.get.remote("temperature"))
            max_token= ray.get(context.get.remote("max_tokens"))
            top_p= ray.get(context.get.remote("top_p"))
            repetition_penalty= ray.get(context.get.remote("repetition_penalty"))

        if not instruction:
            raise ValueError(f"task {dag_id} missing instruction")

        # Build prompt
        prompt = f"""
        You are a top-tier retail order processing assistant. Carefully review the user's instructions and return a single JSON object containing all operation details in strict JSON format.

        The JSON object should include the following optional fields: "user_info", "cancellation", "modification".

        1. "user_info": (object) [if provided] Contains user information.
        - "email": (string)
        - "user_name": (string)
        - "zip_code": (string)

        2. "cancellation": (object) [if cancellation requested]
        - "order_id": (string) Order ID to cancel (starts with "#").
        - "reason": (string) [optional] Cancellation reason.

        3. "modification": (object) [if modification requested]
        - "order_id": (string) Order ID to modify.
        - "item_to_modify": (object) Item to modify, including "name" and "attributes".
        - "new_item_spec": (object) New item specifications (only includes changed "attributes").
        - "payment_method_id": (string) [optional] Payment method ID.

        User instructions:
        "{instruction}"

        JSON output:
        """
        
        # Build API payload
        payload = {
            "model": "Qwen/Qwen3-32B",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_token,
            "enable_thinking": False,
            "response_format": {"type": "text"}               
        }
        
        inference_features, llm_output= None, None
        if use_online_model:
            inference_features, llm_output= query_llm_online(RemoteLlmRoute.from_dag_context(context), payload, tokenizer_path)
        elif backend == "vllm":
            inference_features, llm_output = query_vllm_model(
                RemoteLlmRoute.for_vllm_worker_base(
                    ray.get(context.get.remote(f"task1_llm_process_request_api_url"))
                ),
                model_alias= "qwen3-32b", 
                messages= payload["messages"], 
                temperature= temperature, 
                max_token= max_token, 
                top_p= top_p, 
                repetition_penalty= repetition_penalty)
        else:
            inference_features, llm_output= query_llm(model_folder= model_folder, model= "Qwen/Qwen3-32B", messages= [{"role": "user", "content": prompt}], temperature= temperature, max_token= max_token, top_p= top_p, repetition_penalty= repetition_penalty)

        
        print(f"Raw LLM output: {llm_output}")

        # JSON
        json_str = _extract_json_from_llm_output(llm_output)
        if not json_str:
            print("LLM output was not valid JSON.")
            json_str= {}

        extracted_info = json.loads(json_str)
        
        # Persist extracted fields to context
        context.put.remote("extracted_info", extracted_info)
        print(f"Extracted fields: {json.dumps(extracted_info, indent=2, ensure_ascii=False)}")
        return json.dumps({
            "dag_id": dag_id,
            "curr_task_feat": inference_features,
            "status": "",
            "start_time": start_time,
            "end_time": time.time(),
        })
    except Exception as e:
        print(f"task1_llm_process error: {str(e)}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed",
            "result": f"task1_llm_process error: {str(e)}",
            "start_time": start_time,
            "end_time": time.time(),
        })

@cpu(cpu_num= 1, mem= 1024)
def task2a_find_user(context):
    print("--- Running Task 2a: find user ---")
    #

    try:
        dag_id = ray.get(context.get.remote("dag_id"))
        start_time= time.time()
        extracted_info = ray.get(context.get.remote("extracted_info"))
        user_info = extracted_info.get("user_info")
        if not user_info:
            print("Instruction lacks user hints; skipping user lookup.")
            context.put.remote("user_details", None)
            return json.dumps({
                "dag_id": dag_id,
                "status": "Instruction lacks user hints; skipping user lookup.",
                "start_time": start_time,
                "end_time": time.time()
            })
        
        backend_data = ray.get(context.get.remote("backend_data"))
        user_id = None
        if user_info.get("email"):
            user_id = FindUserIdByEmail.invoke(backend_data, email=user_info["email"])
        elif user_info.get("user_name") and user_info.get("zip_code"):
            name_parts = user_info["user_name"].split()
            user_id = FindUserIdByNameZip.invoke(backend_data, first_name=name_parts[0], last_name=" ".join(name_parts[1:]), zip=user_info["zip_code"])
        
        if not user_id or user_id.startswith("Error:"): raise ValueError(f"found: {user_info}")
        user_details_str = GetUserDetails.invoke(backend_data, user_id=user_id)
        if user_details_str.startswith("Error:"): raise ValueError(f": {user_details_str}")
        
        user_details = json.loads(user_details_str)
        context.put.remote("user_details", user_details)
        print(f"found: {user_id}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "success in task2a",
            "start_time": start_time,
            "end_time": time.time(),
        })
    except Exception as e:
        print(f"Task 2a (find user) error: {e}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed",
            "result": f"task2a_find_user error: {str(e)}",
            "start_time": start_time,
            "end_time": time.time(),
        })

@cpu(cpu_num= 1, mem= 1024)
def task2b_get_order_details(context):
    #

    print("--- Running Task 2b:  ---")
    try:
        start_time= time.time()
        dag_id = ray.get(context.get.remote("dag_id"))
        extracted_info = ray.get(context.get.remote("extracted_info"))
        backend_data = ray.get(context.get.remote("backend_data"))
        order_ids_to_fetch = set()
        if "cancellation" in extracted_info and extracted_info["cancellation"] and extracted_info["cancellation"].get("order_id"):
            order_ids_to_fetch.add(extracted_info["cancellation"]["order_id"])
        if "modification" in extracted_info and extracted_info["modification"] and extracted_info["modification"].get("order_id"):
            order_ids_to_fetch.add(extracted_info["modification"]["order_id"])

        if not order_ids_to_fetch:
            print("No order_id found in instruction.")
            context.put.remote("order_details_map", {})
            return json.dumps({
                "dag_id": dag_id,
                "status": "No order_id found in instruction.",
                "start_time": start_time,
                "end_time": time.time(),
            })
        
        all_order_details = {}
        for order_id in order_ids_to_fetch:
            details_str = GetOrderDetails.invoke(backend_data, order_id)
            if not details_str.startswith("Error:"):
                all_order_details[order_id] = json.loads(details_str)
            else:
                print(f"warning:  {order_id}  details: {details_str}")
        
        context.put.remote("order_details_map", all_order_details)
        print(f" {len(all_order_details)} details.")
        return json.dumps({
            "dag_id": dag_id,
            "status": "success in task2b",
            "start_time": start_time,
            "end_time": time.time(),
        })
    except Exception as e:
        print(f"Task 2b (get order details) error: {e}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed",
            "result": f"task2b_get_order_details error: {str(e)}",
            "start_time": start_time,
            "end_time": time.time()
        })

@cpu(cpu_num= 1, mem= 1024)
def task3_execute_operations(context):
    #

    print("--- Running Task 3: (/modify) ---")
    try:
        start_time= time.time()
        dag_id = ray.get(context.get.remote("dag_id"))
        extracted_info = ray.get(context.get.remote("extracted_info"))
        backend_data = ray.get(context.get.remote("backend_data"))
        user_details = ray.get(context.get.remote("user_details"))
        order_details_map = ray.get(context.get.remote("order_details_map"))
        
        final_results = {}

        # 1. 
        if "cancellation" in extracted_info and extracted_info["cancellation"]:
            cancellation_ops = extracted_info["cancellation"]
            # handle
            if isinstance(cancellation_ops, dict):
                cancellation_ops = [cancellation_ops]

            cancellation_results = []
            for op_info in cancellation_ops:
                order_id = op_info.get("order_id")
                if order_id:
                    reason = op_info.get("reason", "")
                    result_str = CancelPendingOrder.invoke(backend_data, order_id=order_id, reason=reason)
                    result = {"status": "success", "result": json.loads(result_str)} if not result_str.startswith("Error:") else {"status": "error", "details": result_str}
                else:
                    result = {"status": "skipped", "reason": "No order_id for cancellation."}
                cancellation_results.append(result)
            #

            if cancellation_results:
                 final_results["cancellation_result"] = cancellation_results[0] if len(cancellation_results) == 1 else cancellation_results

        # 2. 
        if "modification" in extracted_info and extracted_info["modification"]:
            modification_ops = extracted_info["modification"]
            # handle
            if isinstance(modification_ops, dict):
                modification_ops = [modification_ops]
            
            modification_results = []
            for op_info in modification_ops:
                order_id = op_info.get("order_id")
                if not order_id:
                    result = {"status": "skipped", "reason": "No order_id for modification."}
                else:
                    order_details = order_details_map.get(order_id)
                    if not order_details:
                        result = {"status": "error", "details": f"Could not fetch order {order_id} details."}
                    else:
                        item_to_modify = op_info.get("item_to_modify", {})
                        new_item_spec = op_info.get("new_item_spec", {})
                        
                        product_map_str = ListAllProductTypes.invoke(backend_data)
                        product_map = json.loads(product_map_str) if not product_map_str.startswith("Error:") else {}
                        original_item = _find_item_details_in_order(order_details, item_to_modify)
                        
                        if not original_item:
                            error = f"item not found: {item_to_modify}"
                            result = {"status": "error", "details": error}
                        else:
                            product_id = product_map.get(original_item["name"])
                            new_id = _find_new_product_variant_id(backend_data, product_id, original_item.get("options", {}), new_item_spec)
                            if not new_id:
                                result = {"status": "error", "details": f"No catalog variant matches requested spec: {new_item_spec}"}
                            else:
                                payment_id = op_info.get("payment_method_id") or (list(user_details["payment_methods"].values())[0]["id"] if user_details and user_details.get("payment_methods") else None)
                                if not payment_id:
                                    result = {"status": "error", "details": "Could not determine payment method for modification."}
                                else:
                                    params = {"order_id": order_id, "item_ids": [original_item['item_id']], "new_item_ids": [new_id], "payment_method_id": payment_id}
                                    result_str = ModifyPendingOrderItems.invoke(backend_data, **params)
                                    result = {"status": "success", "result": json.loads(result_str)} if not result_str.startswith("Error:") else {"status": "error", "details": result_str}
                modification_results.append(result)
            #

            if modification_results:
                final_results["modification_result"] = modification_results[0] if len(modification_results) == 1 else modification_results

        context.put.remote("final_result", final_results)
        print("--- Task 3 finished ---")
        return json.dumps({
            "dag_id": dag_id,
            "status": "success in task3",
            "start_time": start_time,
            "end_time": time.time()
        })
    except Exception as e:
        print(f"Task 3 () error: {e}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed",
            "result": f"task3_execute_operations error: {str(e)}",
            "start_time": start_time,
            "end_time": time.time()
        })

@io(mem= 1024)
def task4_output_result(context):
    print("--- Running Task 4: emit final output ---")
    try:
        dag_id = ray.get(context.get.remote("dag_id"))
        start_time= time.time()
        final_result = ray.get(context.get.remote("final_result"))
        if not final_result:
            print("Workflow finished without side effects.")
            return json.dumps({
                "dag_id": dag_id,
                "status": "Workflow finished without side effects.",
                "start_time": start_time,
                "end_time": time.time()
            })
        
        print("✅ workflow completed successfully")
        final_output = f"final outcome:\n{json.dumps(final_result, indent=2, ensure_ascii=False)}"
        print(final_output)
        return json.dumps({
            "dag_id": dag_id,
            "status": "success", 
            "result": final_output,
            "start_time": start_time,
            "end_time": time.time()
            })
    except Exception as e:
        print(f"Task 4 (emit) error: {e}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed",
            "result": f"task4_output_result error: {str(e)}",
            "start_time": start_time,
            "end_time": time.time()
        })