import ray
import oss2
import json
import time
import torch
import numpy as np
from PIL import Image
from io import BytesIO
from typing import List, Dict
from core.scheduler import gpu, io, cpu
import os
import re
import gc
import sys
import dashscope

from core.utils.remote_llm_route import RemoteLlmRoute
from core.utils.tbench_tools.common import _extract_json_from_llm_output
from core.utils.tbench_tools.retail.cancel_pending_order import CancelPendingOrder

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
            You are a professional retail order processing assistant. Please carefully review the user's instructions and extract all orders that need to be **canceled** in strict JSON format.
            If the user mentions multiple orders to cancel, return a JSON list containing multiple objects.

            Each JSON object must include the following fields:
            - "order_id": (string) The order ID the user wants to cancel (starting with '#').
            - "reason": (string) [optional] The cancellation reason provided by the user.

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
            "status": "failed",
            "start_time": start_time,
            "end_time": time.time(),            
            "result": f"task0_init error: {str(e)}"
        })

@gpu(gpu_mem= 70000, model_name= "qwen3-32b", backend="huggingface")
def task1_llm_process(context):
    """
    Workflow step 1: use an LLM to extract cancellation intent.
    Uses the same LLM inference pattern as file/task.py.
    """
    print("--- Running Task 1: LLM intent extraction ---")
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
        You are a professional retail order processing assistant. Please carefully review the user's instructions and extract all orders that need to be **canceled** in strict JSON format.
        If the user mentions multiple orders to cancel, return a JSON list containing multiple objects.

        Each JSON object must include the following fields:
        - "order_id": (string) The order ID the user wants to cancel (starting with '#').
        - "reason": (string) [optional] The cancellation reason provided by the user.

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
        extracted_info= {}
        if not json_str:
            print("LLM output was not valid JSON.")
        else:
            try:
                extracted_info = json.loads(json_str)
            except Exception as e:
                print(f"LLM output was not valid JSON.")
        print(f"LLMextracted_info: {extracted_info}")
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
def task2_execute_cancel(context):
    """
    Workflow step 2: execute cancellation from extracted fields.
    """
    # ToolsCancelPendingOrder

    print("--- Running Task 2:  ---")
    try:
        start_time= time.time()
        dag_id= ray.get(context.get.remote('dag_id'))
        #

        extracted_info = ray.get(context.get.remote("extracted_info"))
        backend_data = ray.get(context.get.remote("backend_data"))
        # handle cancellation_requests 
        if isinstance(extracted_info, dict):
            cancellation_requests = [extracted_info]
        elif isinstance(extracted_info, list):
            cancellation_requests = extracted_info
        else:
            raise ValueError("LLMobject")

        all_results = []
        for request in cancellation_requests:
            order_id = request.get("order_id")
            if not order_id:
                print(f"requestmissing'order_id': {request}")
                all_results.append({"error": "Missing order_id", "request": request})
                continue
            
            params = {
                "order_id": order_id,
                "reason": request.get("reason", "")
            }

            print(f" {order_id}: '{params['reason']}'")
            #

            result_str = CancelPendingOrder.invoke(backend_data, **params)
            
            if result_str.startswith("Error:"):
                print(f" > tool invocation failed for order {order_id}: {result_str}")
                all_results.append({"order_id": order_id, "status": "error", "details": result_str})
            else:
                result_json = json.loads(result_str)
                print(f" >  {order_id}")
                all_results.append({"order_id": order_id, "status": "success", "result": result_json})

        #

        context.put.remote("final_result", all_results)
        print("--- Task 2 finished ---")
        return json.dumps({
            "dag_id": dag_id,
            "status": "done",
            "start_time": start_time,
            "end_time": time.time(),
        })
    except Exception as e:
        print(f"task2_execute_cancel error: {str(e)}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed",
            "result": f"task2_execute_cancel error: {str(e)}",
            "start_time": start_time,
            "end_time": time.time(),
        })

@io(mem=1024)
def task3_output_result(context):
    """
    Workflow step 3: emit cancellation outcome.
    """
    print("--- Running Task 3: emit final output ---")
    try:
        start_time= time.time()
        dag_id= ray.get(context.get.remote("dag_id"))
        final_result = ray.get(context.get.remote("final_result"))
        print("✅ workflow completed successfully")
        # JSONemit
        final_output = f"final outcome:\n{json.dumps(final_result, indent=2, ensure_ascii=False)}"
        print(final_output)
        return json.dumps({
            "dag_id": dag_id,
            "status": "done", 
            "result": final_output,
            "start_time": start_time,
            "end_time": time.time(),
        })
    except Exception as e:
        print(f"task3_output_result error: {str(e)}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed",
            "result": f"task3_output_result error: {str(e)}",
            "start_time": start_time,
            "end_time": time.time(),
        })
