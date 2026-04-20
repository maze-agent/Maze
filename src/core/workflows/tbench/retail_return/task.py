import ray
import json
import os
import sys
import dashscope
from core.scheduler import io, gpu,cpu
import time
import re
import gc
from typing import Dict, Any, List

from core.utils.remote_llm_route import RemoteLlmRoute
from core.utils.tbench_tools.common import _extract_json_from_llm_output
from core.utils.tbench_tools.retail.find_user_id_by_email import FindUserIdByEmail
from core.utils.tbench_tools.retail.find_user_id_by_name_zip import FindUserIdByNameZip
from core.utils.tbench_tools.retail.get_user_details import GetUserDetails
from core.utils.tbench_tools.retail.get_order_details import GetOrderDetails
from core.utils.tbench_tools.retail.return_delivered_order_items import ReturnDeliveredOrderItems

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
            You are a professional retail order processing assistant. Please carefully review the user's instructions and extract all key information required for **returns** in strict JSON format.

            Required fields to extract:
            - "order_id": (string) The order ID the user wants to process (starting with '#').
            - "items": (list of strings) List of product the user wants to return, if all items should return, you should only return ['all'].
            - "reason": (string) [optional] Reason provided by the user.
            - "user_name": (string) [optional] User's name.
            - "zip_code": (string) [optional] User's postal code.
            - "email": (string) [optional] User's email address.
            - "payment_method_id": (string) [optional] Refund method ID explicitly specified by the user.

            User instructions: "{instruction}"

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
            "status": "failed",
            "result": f"task0_init error: {str(e)}",
            "start_time": start_time,
            "end_time": time.time(),
        })

@gpu(gpu_mem= 70000, model_name= "qwen3-32b", backend="huggingface")
def task1_llm_process(context):
    """
    Workflow step 1: extract intent.
    Uses the same LLM inference pattern as file/task.py.
    """
    print("--- Running Task 1: LLM intent extraction ---")
    try:
        backend= task1_llm_process._task_decorator["backend"]
        print(f"✅ LLM return-intent extraction started....")
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

        # Build prompt
        prompt = f"""
        You are a professional retail order processing assistant. Please carefully review the user's instructions and extract all key information required for **returns** in strict JSON format.

        Required fields to extract:
        - "order_id": (string) The order ID the user wants to process (starting with '#').
        - "items": (list of strings) List of product the user wants to return, if all items should return, you should only return ['all'].
        - "reason": (string) [optional] Reason provided by the user.
        - "user_name": (string) [optional] User's name.
        - "zip_code": (string) [optional] User's postal code.
        - "email": (string) [optional] User's email address.
        - "payment_method_id": (string) [optional] Refund method ID explicitly specified by the user.

        User instructions: "{instruction}"

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
            inference_features, llm_output = query_llm_online(
                RemoteLlmRoute.from_dag_context(context),
                payload,
                tokenizer_path,
            )
        elif backend == "vllm":
            inference_features, llm_output = query_vllm_model(
                RemoteLlmRoute.for_vllm_worker_base(
                    ray.get(context.get.remote("task1_llm_process_request_api_url"))
                ),
                model_alias="qwen3-32b",
                messages=payload["messages"],
                temperature=temperature,
                max_token=max_token,
                top_p=top_p,
                repetition_penalty=repetition_penalty,
            )
        else:
            inference_features, llm_output= query_llm(model_folder= model_folder, model= "Qwen/Qwen3-32B", messages= [{"role": "user", "content": prompt}], temperature= temperature, max_token= max_token, top_p= top_p, repetition_penalty= repetition_penalty)

        print(f"Raw LLM output: {llm_output}")
        # JSON
        json_str = _extract_json_from_llm_output(llm_output)
        if not json_str:
            print("⭐ LLM output was not valid JSON.")
            json_str= {}
        # print(f"⭐ instruction: {instruction}, json_str: {json_str}")
        extracted_info = json.loads(json_str)
        
        # Persist extracted fields to context
        context.put.remote("extracted_info", extracted_info)
        print(f"Extracted fields: {json.dumps(extracted_info, indent=2, ensure_ascii=False)}")
        
        return json.dumps({
            "dag_id": dag_id,
            "curr_task_feat": inference_features,
            "start_time": start_time,
            "end_time": time.time(),
            "status": "return_intent_ok"
        })
    except Exception as e:
        print(f"task1_llm_process error: {str(e)}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed",
            "start_time": start_time,
            "end_time": time.time(),            
            "result": f"task1_llm_process error: {str(e)}"
        })

@cpu(cpu_num= 1, mem= 1024)
def task2_find_user(context):
    # ---  ---

    print("--- Running Task 2: find user ---")
    try:
        start_time= time.time()
        dag_id= ray.get(context.get.remote('dag_id'))
        extracted_info = ray.get(context.get.remote("extracted_info"))
        backend_data = ray.get(context.get.remote("backend_data"))
        user_id = None
        email = extracted_info.get("email")
        if email:
            print(f"Lookup user by email: {email}")
            user_id = FindUserIdByEmail.invoke(backend_data, email=email)
        
        user_name = extracted_info.get("user_name")
        zip_code = extracted_info.get("zip_code")
        if user_name and zip_code:
            name_parts = user_name.split()
            first_name = name_parts[0]
            last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
            print(f"Lookup user by name and zip, first_name: {first_name}, last_name: {last_name}, zip_code: {zip_code}")
            user_id = FindUserIdByNameZip.invoke(backend_data, first_name=first_name, last_name=last_name, zip=zip_code)
            
        if not user_id or user_id.startswith("Error:"): 
            print(f"Could not uniquely resolve user from provided fields: {user_id}")
            return json.dumps({
                "dag_id": dag_id,
                "status": "find_user_failed",
                "start_time": start_time,
                "end_time": time.time()
            })
        user_details_str = GetUserDetails.invoke(backend_data, user_id=user_id)
        if "Error" in user_details_str: 
            print(f"Could not load user details for resolved id: {user_details_str}")
            return json.dumps({
                "dag_id": dag_id,
                "status":  f"Could not load user details for resolved id: {user_details_str}",
                "start_time": start_time,
                "end_time": time.time()
            })
        user_details = json.loads(user_details_str)
        context.put.remote("user_details", user_details)
        print(f"Loaded user details: {user_details}")
        print("--- Task 2 finished ---")
        return json.dumps({
            "dag_id": dag_id,
            "status": "find_user_ok",
            "start_time": start_time,
            "end_time": time.time()
        })
    except Exception as e:
        print(f"Task 2 (find user) error: {e}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed",
            "start_time": start_time,
            "end_time": time.time(),
            "result": f"task2_find_user error: {str(e)}"
        })

@cpu(cpu_num= 1, mem= 1024)
def task3_get_order_details(context):
    # ---  --

    print("--- Running Task 3: get order details ---")
    try:
        start_time= time.time()
        dag_id= ray.get(context.get.remote('dag_id'))
        extracted_info = ray.get(context.get.remote("extracted_info"))
        order_id = extracted_info.get("order_id")
        if not order_id: 
            print("LLM did not extract order_id")
            return json.dumps({
                "dag_id": dag_id,
                "status": "get_order_details_failed",
                "start_time": start_time,
                "end_time": time.time()
            })
        backend_data = ray.get(context.get.remote("backend_data"))
        order_details_str = GetOrderDetails.invoke(backend_data, order_id)
        if "Error" in order_details_str: 
            print(f"Could not load order details: {order_details_str}")
            return json.dumps({
                "dag_id": dag_id,
                "status": f"Could not load order details: {order_details_str}",
                "start_time": start_time,
                "end_time": time.time()
            })
        order_details = json.loads(order_details_str)
        context.put.remote("order_details", order_details)
        print(f"Loaded order {order_id} details.")
        print("--- Task 3 finished ---")
        return json.dumps({
            "dag_id": dag_id,
            "status": "get_order_details_ok",
            "start_time": start_time,
            "end_time": time.time()
        })
    except Exception as e:
        print(f"Task 3 (get order details) error: {e}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed",
            "start_time": start_time,
            "end_time": time.time(),
            "result": f"task3_get_order_details error: {str(e)}"
        })

@cpu(cpu_num= 1, mem= 1024)
def task4_execute_return(context):
    # ---  ---
    start_time = time.time()  #

    print("--- Running Task 4: execute return ---")
    try:
        dag_id= ray.get(context.get.remote('dag_id'))
        extracted_info = ray.get(context.get.remote("extracted_info"))
        order_details = ray.get(context.get.remote("order_details"))
        user_details = ray.get(context.get.remote("user_details"))
        backend_data = ray.get(context.get.remote("backend_data"))
        payment_method_id = extracted_info.get("payment_method_id")
        if not payment_method_id:
            payment_methods_dict = user_details.get("payment_methods")
            if payment_methods_dict:
                payment_method_id = list(payment_methods_dict.values())[0]["id"]
                print(f"No refund method in instruction; using default payment method: {payment_method_id}")
            else: 
                print(f"No payment method available for refund.")
                return json.dumps({
                    "dag_id": dag_id,
                    "final_result": "No payment method available for refund.",
                    "start_time": start_time,
                    "end_time": time.time()
                })
        
        item_names_to_return = [name.lower() for name in extracted_info.get("items", [])]
        if not item_names_to_return: 
            print(f"Return requires explicit items.")
            return json.dumps({
                "dag_id": dag_id,
                "final_result": "Return requires explicit items.",
                "start_time": start_time,
                "end_time": time.time()
            })
        item_ids_to_return = [item["item_id"] for item in order_details.get("items", []) if item["name"].lower() in item_names_to_return]
        if not item_ids_to_return:
            print(f"In order {order_details.get('order_id')} missing return line items: {item_names_to_return}")
            return json.dumps({
                "dag_id": dag_id,
                "final_result": f"In order {order_details.get('order_id')} missing return line items: {item_names_to_return}",
                "start_time": start_time,
                "end_time": time.time()
            })
        params = {"order_id": order_details.get('order_id'), "item_ids": item_ids_to_return, "payment_method_id": payment_method_id}
        print(f"Executing return with params: {params}")
        result_str = ReturnDeliveredOrderItems.invoke(backend_data, **params)
        if result_str.startswith("Error:"):
            print(f"tool invocation failed: {result_str}")
            return json.dumps({
                "dag_id": dag_id,
                "final_result": f"tool invocation failed: {result_str}",
                "start_time": start_time,
                "end_time": time.time()
            })
        final_result = {"action": "return", "result": json.loads(result_str)}
        context.put.remote("final_result", final_result)
        print("--- Task 4 finished ---")
        return json.dumps({
            "dag_id": dag_id,
            "final_result": final_result,
            "start_time": start_time,
            "end_time": time.time()
        })
    except Exception as e:
        print(f"Task 4 (execute return) error: {e}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed",
            "result": f"task4_execute_return error: {str(e)}",
            "start_time": start_time,
            "end_time": time.time()
        })

@io(mem= 1024)
def task5_output_result(context):
    print("--- Running Task 5: emit final output ---")
    start_time = time.time()  #

    try:
        dag_id= ray.get(context.get.remote('dag_id'))
        final_result = ray.get(context.get.remote("final_result"))
        print("✅ workflow completed successfully")
        final_output = f"final outcome:\n{json.dumps(final_result, indent=2, ensure_ascii=False)}"
        print(final_output)
        return json.dumps({
            "dag_id": dag_id,
            "final_result": final_result,
            "start_time": start_time,
            "end_time": time.time()
        })
    except Exception as e:
        print(f"task5_output_result error: {e}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed",
            "result": f"task5_output_result error: {str(e)}",
            "start_time": start_time,
            "end_time": time.time()
        })