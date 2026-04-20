import ray
import json
import time
from core.scheduler import gpu, io, cpu
import os
import gc
import sys
import re
from core.utils.tbench_tools.airline.cancel_reservation import CancelReservation
from core.utils.tbench_tools.airline.get_user_details import GetUserDetails
from core.utils.tbench_tools.airline.get_reservation_details import GetReservationDetails
from core.utils.tbench_tools.airline.search_direct_flight import SearchDirectFlight
from core.utils.tbench_tools.airline.book_reservation import BookReservation
from core.utils.remote_llm_route import RemoteLlmRoute
from typing import List, Dict
# ---  ---
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

@io(mem=1024)
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
            flights_data, users_data, reservations_data= json.loads(supplementary_files['flights.json']), json.loads(supplementary_files['users.json']), json.loads(supplementary_files['reservations.json'])
            backend_data = {
                "flights": flights_data,
                "users": users_data,
                "reservations": reservations_data
            }
            context.put.remote("backend_data", backend_data)
            context.put.remote("instruction", instruction)
            print("--- Task 0 finished: environment ready ---")

            prompt = f"""
            You are a professional flight booking assistant. Please carefully read the user's flight booking instructions below and extract the key information.  
            You need to return the extracted information in a strict JSON format, without any additional explanations or text.  

            The fields to be extracted are as follows:  
            - "user_id": User ID (e.g., "mia_jackson_2156").  
            - "origin": Origin airport code (3 letters, e.g., "JFK", "SFO").  
            - "destination": Destination airport code (3 letters, e.g., "SEA", "LAX").  
            - "date": Departure date (format: "YYYY-MM-DD", default year is 2024).  
            - "cabin": Cabin class (must be one of "basic_economy", "economy", "business").  
            - "baggages": Number of baggage items (integer).  
            - "insurance": Whether insurance is needed ("yes" or "no").  
            - "constraints": A list of strings containing all other constraints and preferences.  

            User instructions:  
            "{instruction}"  

            JSON output:  
            """
            llm_process_feat= {"text_length": len(prompt), "token_count": estimate_tokens(prompt)}

            return json.dumps({
                "succ_task_feat": {
                    "task1_llm_process1": {"text_length": llm_process_feat["text_length"], "token_count": llm_process_feat["token_count"], "reason": 0}
                },
                "dag_id": dag_id,
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
def task1_llm_process1(context):
    """
    Workflow step 1 (LLM): extract core booking fields.
    Uses the same LLM inference pattern as file/task.py.
    """
    print("--- Running Task 1: LLM ---")
    try:
        backend= task1_llm_process1._task_decorator["backend"]
        print(f"✅ LLMstart....")
        start_time= time.time()

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
        You are a professional flight booking assistant. Please carefully read the user's instructions below and extract the key information.
        You must return the extracted information in strict JSON format without any additional explanations or text.

        Required fields to extract:
        - "user_id": User ID (e.g., "mia_kim_4397")
        - "cancel_reservation_id": Reservation ID to be canceled
        - "origin": New booking origin airport code (3-letter, e.g., "JFK", "EWR")
        - "destination": New booking destination airport code (3-letter, e.g., "SEA", "LAX")
        - "departure_date": Departure date (format: "YYYY-MM-DD", default year is 2024)
        - "return_date": Return date (format: "YYYY-MM-DD")
        - "cabin": Cabin class (must be one of: "basic_economy", "economy", "business")
        - "baggages": Number of baggage items (integer)
        - "insurance": Whether insurance is needed ("yes" or "no")
        - "payment_preference": Payment preference (e.g., "use_smaller_gift_card_first")
        - "constraints": A list of strings containing all other constraints and preferences

        User instructions:
        "{instruction}"

        JSON output:
        """
        print(f"🚫DEBUG LLM prompt: {prompt}")
        # Build API payload
        payload = {
            "model": "Qwen/Qwen3-32B",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_token,
            "enable_thinking": False,
            "response_format": {"type": "text"}               
        }
        inference_features, answer= None, None
        if use_online_model:
            inference_features, answer= query_llm_online(RemoteLlmRoute.from_dag_context(context), payload, tokenizer_path)
        elif backend == "vllm":
            inference_features, answer= query_vllm_model(
                RemoteLlmRoute.for_vllm_worker_base(
                    ray.get(context.get.remote(f"task1_llm_process1_request_api_url"))
                ),
                model_alias= "qwen3-32b",
                messages= payload["messages"],
                temperature= temperature,
                max_token= max_token,
                top_p= top_p,
                repetition_penalty= repetition_penalty
            )
        else:
            inference_features, answer= query_llm(model_folder= model_folder, model= "Qwen/Qwen3-32B", messages= [{"role": "user", "content": prompt}], temperature= temperature, max_token= max_token, top_p= top_p, repetition_penalty= repetition_penalty)

        # print(f"Raw LLM output: {answer}")

        # JSON
        json_str = _extract_json_from_llm_output(answer)
        extracted_info= {}
        if not json_str:
            print("LLM output was not valid JSON.")
        else:
            try:
                extracted_info = json.loads(json_str)
            except Exception as e:
                print(f"LLM output was not valid JSON.")
        print(f"LLMextracted_info: {extracted_info}")
        # Persist extracted fields to context
        context.put.remote("extracted_info", extracted_info)
        user_id = extracted_info["user_id"]
        context.put.remote("user_id", user_id)
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
            "end_time": time.time()
        })

@cpu(cpu_num= 1, mem= 1024)
def task2_get_user_and_reservation_details(context):
    """
    Workflow step 2 (tool): fetch user and reservation details.
    1. 
    2. 
    """

    print("--- Running Task 2:  ---")
    try:
        start_time= time.time()
        dag_id= ray.get(context.get.remote("dag_id"))
        backend_data = ray.get(context.get.remote("backend_data"))
        extracted_info = ray.get(context.get.remote("extracted_info"))
        user_id = ray.get(context.get.remote("user_id"))
        print(f"currentlyID: {user_id}  details...")
        #

        user_details_str = GetUserDetails.invoke(backend_data, user_id)
        print(f"GetUserDetails: {user_details_str}")
        
        if not user_details_str or user_details_str.strip() == "":
            raise ValueError("GetUserDetails returned empty payload")
        
        if "Error" in user_details_str:
            raise ValueError(f"failed: {user_details_str}")
        
        try:
            user_details = json.loads(user_details_str)
        except json.JSONDecodeError as e:
            print(f"JSONerrorstring: '{user_details_str}'")
            raise ValueError(f"JSON: {e}")
        
        context.put.remote("user_details", user_details)
        print(f"Loaded user details: {user_details.get('name', 'Unknown')}")
        
        #

        reservation_id = extracted_info.get("cancel_reservation_id")
        if reservation_id:
            print(f"currentlyID: {reservation_id}  details...")
            reservation_details_str = GetReservationDetails.invoke(backend_data, reservation_id)
            print(f"GetReservationDetails: {reservation_details_str}")
            
            if not reservation_details_str or reservation_details_str.strip() == "":
                raise ValueError("GetReservationDetails returned empty payload")
            
            if "Error" in reservation_details_str:
                raise ValueError(f"failed: {reservation_details_str}")
            
            try:
                reservation_details = json.loads(reservation_details_str)
            except json.JSONDecodeError as e:
                print(f"JSONerrorstring: '{reservation_details_str}'")
                raise ValueError(f"JSON: {e}")
            
            context.put.remote("reservation_details", reservation_details)
            print(f": {reservation_details.get('reservation_id', 'Unknown')}")
        else:
            print("ID")
        
        print("--- Task 2 finished ---")
        return json.dumps({
            "dag_id": dag_id,
            "status": "",
            "start_time": start_time,
            "end_time": time.time(),
        })
    except Exception as e:
        print(f"task2_get_user_and_reservation_details error: {str(e)}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed",
            "result": f"task2_get_user_and_reservation_details error: {str(e)}",
            "start_time": start_time,
            "end_time": time.time()
        })

@io(mem= 1024)
def task3_cancel_reservation(context):
    """
    Workflow step 3 (tool): cancel reservation.
    
    """
    print("--- Running Task 3:  ---")
    try:
        start_time= time.time()
        dag_id= ray.get(context.get.remote("dag_id"))
        backend_data = ray.get(context.get.remote("backend_data"))
        extracted_info = ray.get(context.get.remote("extracted_info"))
        reservation_id = extracted_info.get("cancel_reservation_id")
        if not reservation_id:
            print("ID")
            context.put.remote("cancel_result", "no cancellation needed")
            return json.dumps({
                "status": "no cancellation needed",
                "dag_id": dag_id,
                "start_time": start_time,
                "end_time": time.time()
                })
        
        #

        result = CancelReservation.invoke(backend_data, reservation_id)
        context.put.remote("cancel_result", result)
        print(f": {result}")
        print("--- Task 3 finished ---")
        return json.dumps({
            "status": "",
            "dag_id": dag_id,
            "start_time": start_time,
            "end_time": time.time()
        })
    except Exception as e:
        print(f"task3_cancel_reservation error: {str(e)}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed",
            "result": f"task3_cancel_reservation error: {str(e)}",
            "start_time": start_time,
            "end_time": time.time()
        })

@cpu(cpu_num= 1, mem= 1024)
def task4_search_new_flights(context):
    """
    Workflow step 4 (tool): search new flights.
    
    """

    print("--- Running Task 4:  ---")
    try:
        start_time= time.time()
        dag_id= ray.get(context.get.remote("dag_id"))
        backend_data = ray.get(context.get.remote("backend_data"))
        extracted_info = ray.get(context.get.remote("extracted_info"))
        origin = extracted_info.get("origin")
        destination = extracted_info.get("destination")
        departure_date = extracted_info.get("departure_date")
        return_date = extracted_info.get("return_date")
        
        print(f":  {origin}  {destination}, : {departure_date}, : {return_date}")
        
        # use SearchDirectFlight 
        outbound_flights = []
        return_flights = []
        
        #

        outbound_flights_str = SearchDirectFlight.invoke(backend_data, origin, destination, departure_date)
        outbound_flights = json.loads(outbound_flights_str)
        if not outbound_flights:
            print(f"WARNING:found {origin}{destination}")
        else:
            print(f"found {len(outbound_flights)} ")
            for flight in outbound_flights:
                print(f": {flight['flight_number']}, : {flight['prices']}")
        
        #

        if return_date:
            return_flights_str = SearchDirectFlight.invoke(backend_data, destination, origin, return_date)
            return_flights = json.loads(return_flights_str)
            if not return_flights:
                print(f"WARNING: found {destination}  {origin} ")
            else:
                print(f"found {len(return_flights)} ")
                for flight in return_flights:
                    print(f": {flight['flight_number']}, : {flight['prices']}")
        
        context.put.remote("outbound_flights", outbound_flights)
        if return_date:
            context.put.remote("return_flights", return_flights)
        
        print(f"found {len(outbound_flights)}  {len(return_flights) if return_date else 0} ")
        print("--- Task 4 finished ---")
        prompt = f"""
        You are a professional flight booking decision assistant.
        Your task is to select the most suitable flights from the candidate options based on user preferences.

        # User Preferences
        {json.dumps(extracted_info, indent=2)}

        # Candidate Flights
        Outbound Flights:
        {json.dumps(outbound_flights, indent=2)}

        Return Flights:
        {json.dumps(return_flights, indent=2)}

        # Your Task
        1. Carefully read and understand each of the user's requirements
        2. Select the most appropriate flight combination from the candidates
        3. Return the chosen flight numbers in strict JSON format as follows:
        {{
            "outbound_flight_number": "xxx",
            "return_flight_number": "xxx"
        }}

        Notes:
        - If no suitable flight is found, the corresponding value can be null.
        - Ensure the returned JSON format is correct.
        """
        llm_process_feat= {"text_length": len(prompt), "token_count": estimate_tokens(prompt)}

        return json.dumps({
            "dag_id": dag_id,
            "succ_task_feat": {
                "task5_llm_process2": {"text_length": llm_process_feat["text_length"], "token_count": llm_process_feat["token_count"], "reason": 0}
            },
            "curr_task_feat": None,
            "status": "",
            "start_time": start_time,
            "end_time": time.time(),
        })
    except Exception as e:
        print(f"task4_search_new_flights error: {str(e)}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed",
            "result": f"task4_search_new_flights error: {str(e)}",
            "start_time": start_time,
            "end_time": time.time(),
        })


@gpu(gpu_mem= 70000, model_name= "qwen3-32b", backend="huggingface")
def task5_llm_process2(context):
    """
    Workflow step 5 (LLM): choose flights.
    useLLM
    Uses the same LLM inference pattern as file/task.py.
    """
    print("--- Running Task 5: LLM ---")
    try:
        backend = task5_llm_process2._task_decorator["backend"]
        print(f"✅ LLMstart....")
        start_time = time.time()  #

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
        #

        if not instruction:
            raise ValueError(f"task {dag_id} missing instruction")

        extracted_info = ray.get(context.get.remote("extracted_info"))
        outbound_flights = ray.get(context.get.remote("outbound_flights"))
        
        # return_flightsuse
        try:
            return_flights = ray.get(context.get.remote("return_flights"))
        except:
            return_flights = []
        
        if not outbound_flights:
            return json.dumps({
                "dag_id": dag_id,
                "status": "no matching outbound flight",
                "start_time": start_time,
                "end_time": time.time()
            })
        
        # Build prompt
        prompt = f"""
        You are a professional flight booking decision assistant.
        Your task is to select the most suitable flights from the candidate options based on user preferences.

        # User Preferences
        {json.dumps(extracted_info, indent=2)}

        # Candidate Flights
        Outbound Flights:
        {json.dumps(outbound_flights, indent=2)}

        Return Flights:
        {json.dumps(return_flights, indent=2)}

        # Your Task
        1. Carefully read and understand each of the user's requirements
        2. Select the most appropriate flight combination from the candidates
        3. Return the chosen flight numbers in strict JSON format as follows:
        {{
            "outbound_flight_number": "xxx",
            "return_flight_number": "xxx"
        }}

        Notes:
        - If no suitable flight is found, the corresponding value can be null.
        - Ensure the returned JSON format is correct.
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
            inference_features, llm_output= query_vllm_model(
                RemoteLlmRoute.for_vllm_worker_base(
                    ray.get(context.get.remote(f"task5_llm_process2_request_api_url"))
                ),
                model_alias= "qwen3-32b",
                messages= payload["messages"],
                temperature= temperature,
                max_token= max_token,
                top_p= top_p,
                repetition_penalty= repetition_penalty
            )
        else:
            inference_features, llm_output= query_llm(model_folder= model_folder, model= "Qwen/Qwen3-32B", messages= [{"role": "user", "content": prompt}], temperature= temperature, max_token= max_token, top_p= top_p, repetition_penalty= repetition_penalty)

        print(f"Raw LLM output: {llm_output}")

        # JSON
        selected_flights = None
        json_str = _extract_json_from_llm_output(llm_output)
        if json_str:
            try:
                selected_flights = json.loads(json_str)
            except json.JSONDecodeError:
                print(f"❌ JSONfailedstring: '{json_str}'")
        #

        context.put.remote("selected_flights", selected_flights)
        print(f"LLM: {json.dumps(selected_flights, indent=2, ensure_ascii=False)}")
        
        return json.dumps({
            "dag_id": dag_id,
            "curr_task_feat": inference_features,
            "status": "",
            "start_time": start_time,
            "end_time": time.time(),
        })
    except Exception as e:
        print(f"task5_llm_process2 error: {str(e)}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed",
            "result": f"task5_llm_process2 error: {str(e)}",
            "start_time": start_time,
            "end_time": time.time(),
        })

@cpu(cpu_num= 1, mem= 1024)
def task6_book_new_reservation(context):
    """
    Workflow step 6 (tool): create new booking.
    use
    """

    print("--- Running Task 6:  ---")
    try:
        start_time= time.time()
        dag_id= ray.get(context.get.remote("dag_id"))
        backend_data = ray.get(context.get.remote("backend_data"))
        extracted_info = ray.get(context.get.remote("extracted_info"))
        user_details = ray.get(context.get.remote("user_details"))
        outbound_flights = ray.get(context.get.remote("outbound_flights"))
        try:
            selected_flights = ray.get(context.get.remote("selected_flights"))
        except:
            return json.dumps({
                "dag_id": dag_id,
                "start_time": start_time,
                "end_time": time.time(),
                "status": "no matching outbound flight"
            })

        # return_flightsuse
        try:
            return_flights = ray.get(context.get.remote("return_flights"))
        except:
            return_flights = []
        
        outbound_flight_num = selected_flights.get("outbound_flight_number")
        return_flight_num = selected_flights.get("return_flight_number")
        
        if not isinstance(selected_flights, dict):
            return json.dumps({"status": "new booking completed", "dag_id": dag_id, "start_time": start_time, "end_time": time.time(), "result": ""})            
            # raise ValueError("")

        if not outbound_flight_num:
            return json.dumps({"status": "new booking completed", "dag_id": dag_id, "start_time": start_time, "end_time": time.time(), "result": "LLM"})            
            # raise ValueError("LLM")

        #

        selected_outbound_flight = next((f for f in outbound_flights if f.get("flight_number") == outbound_flight_num), None)
        
        selected_return_flight = None
        if return_flight_num:
            selected_return_flight = next((f for f in return_flights if f.get("flight_number") == return_flight_num), None)

        if not selected_outbound_flight:
            return json.dumps({"status": "new booking completed", "start_time": start_time, "end_time": time.time(), "result": f"found {outbound_flight_num}"})
            # raise ValueError(f" {outbound_flight_num}")
        if return_flight_num and not selected_return_flight:
            return json.dumps({"status": "new booking completed", "start_time": start_time, "end_time": time.time(), "result": f"found {return_flight_num}"})
            # raise ValueError(f" {return_flight_num}")
        
        #  - 
        passengers = []
        # 1
        num_passengers = extracted_info.get("num_passengers", 1)
        
        # use
        if "passengers" in extracted_info:
            passengers = extracted_info["passengers"]
        else:
            # use
            for _ in range(num_passengers):
                passengers.append({
                    "first_name": user_details["name"]["first_name"],
                    "last_name": user_details["name"]["last_name"],
                    "dob": user_details["dob"]
                })

        #

        cabin = extracted_info.get("cabin", "basic_economy")
        total_price = 0
        if selected_outbound_flight:
            total_price += selected_outbound_flight.get("prices", {}).get(cabin, 0)
        if selected_return_flight:
            total_price += selected_return_flight.get("prices", {}).get(cabin, 0)
        
        total_price *= len(passengers)
        total_baggages = extracted_info.get("baggages", 0)
        #

        nonfree_baggages = total_baggages - len(passengers) if total_baggages > len(passengers) else 0
        total_price += 50 * nonfree_baggages  # $50

        if extracted_info.get("insurance") == "yes":
            total_price += 30 * len(passengers)

        #

        payment_methods_for_booking = []
        remaining_balance = total_price

        user_payment_methods = user_details.get("payment_methods", {})
        # use
        certificates = sorted(
            [pm for pm in user_payment_methods.values() if isinstance(pm, dict) and pm.get("source") == "certificate"],
            key=lambda x: x.get("amount", 0)
        )
        for pm in certificates:
            if remaining_balance > 0:
                amount_to_use = min(remaining_balance, pm.get("amount", 0))
                payment_methods_for_booking.append({"payment_id": pm.get("id"), "amount": amount_to_use})
                remaining_balance -= amount_to_use
        
        # use
        gift_cards = sorted(
            [pm for pm in user_payment_methods.values() if isinstance(pm, dict) and pm.get("source") == "gift_card"],
            key=lambda x: x.get("amount", 0)
        )
        if extracted_info.get("payment_preference") == "use_larger_gift_card_first":
            gift_cards.reverse()
        
        for pm in gift_cards:
            if remaining_balance > 0:
                amount_to_use = min(remaining_balance, pm.get("amount", 0))
                payment_methods_for_booking.append({"payment_id": pm.get("id"), "amount": amount_to_use})
                remaining_balance -= amount_to_use

        # use
        if remaining_balance > 0.01:
            credit_card = next((pm for pm in user_payment_methods.values() if isinstance(pm, dict) and pm.get("source") == "credit_card"), None)
            if credit_card:
                payment_methods_for_booking.append({"payment_id": credit_card.get("id"), "amount": round(remaining_balance, 2)})
                remaining_balance = 0
        
        if remaining_balance > 0.01:
            return json.dumps({"status": "failed", "start_time": start_time, "end_time": time.time(), "result": f"Payment failed: insufficient methods or balance.: {remaining_balance}"})

        #

        flights_for_booking_simple = []
        if outbound_flight_num:
            flights_for_booking_simple.append({
                "flight_number": outbound_flight_num,
                "date": extracted_info.get("departure_date")
            })
        if return_flight_num:
            flights_for_booking_simple.append({
                "flight_number": return_flight_num,
                "date": extracted_info.get("return_date")
            })

        #

        booking_args = {
            "user_id": ray.get(context.get.remote("user_id")),
            "origin": extracted_info.get("origin"),
            "destination": extracted_info.get("destination"),
            "flight_type": "round_trip" if return_flight_num else "one_way",
            "cabin": cabin,
            "flights": flights_for_booking_simple,
            "passengers": passengers,
            "payment_methods": payment_methods_for_booking,
            "total_baggages": total_baggages,
            "nonfree_baggages": nonfree_baggages,
            "insurance": extracted_info.get("insurance", "no")
        }
        
        #

        result = BookReservation.invoke(backend_data, **booking_args)
        context.put.remote("booking_result", result)
        
        print("--- Task 6 finished ---")
        return json.dumps({
            "status": "new booking completed",
            "dag_id": dag_id,
            "result": result,
            "start_time": start_time,
            "end_time": time.time(),
        })
    except Exception as e:
        print(f"task6_book_new_reservation error: {str(e)}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed",
            "result": f"task6_book_new_reservation error: {str(e)}",
            "start_time": start_time,
            "end_time": time.time(),
        })