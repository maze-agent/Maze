import ray
import json
import time
import os
import sys
import re
import gc
from typing import List, Dict
from agentos.scheduler import gpu, io, cpu
from agentos.utils.tbench_tools.airline.get_user_details import GetUserDetails
from agentos.utils.tbench_tools.airline.search_direct_flight import SearchDirectFlight
from agentos.utils.tbench_tools.airline.search_onestop_flight import SearchOnestopFlight
from agentos.utils.tbench_tools.airline.book_reservation import BookReservation
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

@cpu(cpu_num= 1, mem= 1024)
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
    Workflow step 1 (LLM): extract core booking fields.
    Uses the same LLM inference pattern as file/task.py.
    """
    print("--- Running Task 1: LLM ---")
    try:
        backend= task1_llm_process._task_decorator['backend']
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
                    ray.get(context.get.remote(f"task1_llm_process_request_api_url"))
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
            "end_time": time.time(),
        })

@cpu(cpu_num= 1, mem= 1024)
def task2a_search_direct_flight(context):
    """
    Workflow step 2.1 (tool): search direct flights.
    """
    
    print("--- Running Task 2a:  ---")
    try:
        start_time= time.time()
        dag_id= ray.get(context.get.remote("dag_id"))
        extracted_info = ray.get(context.get.remote("extracted_info"))
        backend_data = ray.get(context.get.remote("backend_data"))
        instruction= ray.get(context.get.remote("instruction"))
        origin = extracted_info.get("origin")
        destination = extracted_info.get("destination")
        date = extracted_info.get("date")
        #

        direct_flights_str = SearchDirectFlight.invoke(backend_data, origin, destination, date)
        direct_flights = json.loads(direct_flights_str)
        context.put.remote("direct_flights", direct_flights)
        print(f" {len(direct_flights)} ")
        print("--- Task 2a finished ---")
        # task
        all_candidates = []
        #

        for flight in direct_flights:
            all_candidates.append([flight])
        prompt = f"""
        You are a professional and meticulous flight booking decision assistant.
        Your task is to select the single most suitable itinerary from the list of candidate itineraries provided below, based on the user's original request and all constraints.
        # User's original request
        "{instruction}"
        # List of candidate itineraries (JSON format)
        Each itinerary is a list containing one or more flights.
        # Your task
        1. Carefully read and understand each of the user's requirements, including but not limited to: time preferences, price preferences (e.g., "cheapest"), airline preferences, number of layovers, etc.
        2. Strictly filter the candidate itineraries according to these requirements.
        3. From the itineraries that meet all conditions, select the single best option.
        4. Return your chosen itinerary in strict JSON format, without any additional explanations, comments, or text. The returned JSON object should be one element from the candidate itinerary list (a list containing one or more flight dictionaries).
        5. If no itinerary can satisfy the user's core requirements, return an empty JSON list `[]`.
        # JSON output
        """
        text1_length= len(str(json.dumps(all_candidates, indent=2)))
        text1_token_count= estimate_tokens(str(json.dumps(all_candidates, indent=2)))
        context.put.remote("text1_feature", {"text1_length": text1_length, "text1_token_count": text1_token_count})
        return json.dumps({
            "dag_id": dag_id,
            "curr_task_feat": None,
            "succ_task_feat": {
                "task3_llm_fuse_process_filter_and_decide":{
                    "prompt_length": len(prompt),
                    "prompt_token_count": estimate_tokens(prompt),             
                    "text1_length": text1_length,
                    "text1_token_count": text1_token_count,
                    "reason": 0
                }
            },
            "start_time": start_time,
            "end_time": time.time()
        })
    except Exception as e:
        print(f"task2a_search_direct_flight error: {str(e)}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed",
            "result": f"task2a_search_direct_flight error: {str(e)}",
            "start_time": start_time,
            "end_time": time.time(),
        })


@cpu(cpu_num= 1, mem= 1024)
def task2b_search_onestop_flight(context):
    """
    Workflow step 2.2 (tool): search one-stop flights.
    """
    print("--- Running Task 2b:  ---")
    try:
        start_time= time.time()
        dag_id= ray.get(context.get.remote("dag_id"))
        extracted_info = ray.get(context.get.remote("extracted_info"))
        backend_data = ray.get(context.get.remote("backend_data"))
        instruction= ray.get(context.get.remote("instruction"))
        origin = extracted_info.get("origin")
        destination = extracted_info.get("destination")
        date = extracted_info.get("date")
        onestop_flights_str = SearchOnestopFlight.invoke(backend_data, origin, destination, date)
        onestop_flights = json.loads(onestop_flights_str)
        context.put.remote("onestop_flights", onestop_flights)
        print(f" {len(onestop_flights)} ")
        print("--- Task 2b finished ---")
        # task
        all_candidates = []
        #

        for flight in onestop_flights:
            all_candidates.append([flight])

        prompt = f"""
        You are a professional and meticulous flight booking decision assistant.
        Your task is to select the single most suitable itinerary from the list of candidate itineraries provided below, based on the user's original request and all constraints.
        # User's original request
        "{instruction}"
        # List of candidate itineraries (JSON format)
        Each itinerary is a list containing one or more flights.
        # Your task
        1. Carefully read and understand each of the user's requirements, including but not limited to: time preferences, price preferences (e.g., "cheapest"), airline preferences, number of layovers, etc.
        2. Strictly filter the candidate itineraries according to these requirements.
        3. From the itineraries that meet all conditions, select the single best option.
        4. Return your chosen itinerary in strict JSON format, without any additional explanations, comments, or text. The returned JSON object should be one element from the candidate itinerary list (a list containing one or more flight dictionaries).
        5. If no itinerary can satisfy the user's core requirements, return an empty JSON list `[]`.
        # JSON output
        """
        text2_length= len(str(json.dumps(all_candidates, indent=2)))
        text2_token_count= estimate_tokens(str(json.dumps(all_candidates, indent=2)))
        context.put.remote("text2_feature", {"text2_length": text2_length, "text2_token_count": text2_token_count})
        return json.dumps({
            "dag_id": dag_id,
            "curr_task_feat": None,
            "succ_task_feat": {
                "task3_llm_fuse_process_filter_and_decide":{
                    "prompt_length": len(prompt),
                    "prompt_token_count": estimate_tokens(prompt),             
                    "text2_length": text2_length,
                    "text2_token_count": text2_token_count,
                    "reason": 0
                }
            },
            "start_time": start_time,
            "end_time": time.time()
        })
    except Exception as e:
        print(f"task2b_search_onestop_flight error: {str(e)}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed",
            "result": f"task2b_search_onestop_flight error: {str(e)}",
            "start_time": start_time,
            "end_time": time.time(),
        })

@cpu(cpu_num= 1, mem= 1024)
def task2c_get_user_details(context):
    """
    Workflow step 2.3 (tool): fetch user details.
    """
    print("--- Running Task 2c:  ---")
    try:
        start_time= time.time()
        dag_id= ray.get(context.get.remote("dag_id"))
        user_id = ray.get(context.get.remote("user_id"))
        backend_data = ray.get(context.get.remote("backend_data"))
        user_details_str = GetUserDetails.invoke(backend_data, user_id)
        user_details= {}
        try:
            user_details = json.loads(user_details_str)
        except Exception as e:
            print(f"error: {user_details_str}")

        context.put.remote("user_details", user_details)
        print("")
        print("--- Task 2c finished ---")
        return json.dumps({
            "dag_id": dag_id,
            "start_time": start_time,
            "end_time": time.time()
        })
    except Exception as e:
        print(f"task2c_get_user_details error: {str(e)}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed",
            "start_time": start_time,
            "end_time": time.time(),
            "result": f"task2c_get_user_details error: {str(e)}"
        })

@gpu(gpu_mem= 70000, model_name= "qwen3-32b", backend="huggingface")
def task3_llm_fuse_process_filter_and_decide(context):
    """
    Workflow step 3 (LLM): filter candidates and decide.
    useLLM
    Uses the same LLM inference pattern as file/task.py.
    """
    print("--- Running Task 3: LLM  ---")
    try:
        backend= task3_llm_fuse_process_filter_and_decide._task_decorator['backend']
        start_time= time.time()
        print(f"✅ LLMstart....")
        #

        dag_id = ray.get(context.get.remote("dag_id"))
        instruction = ray.get(context.get.remote("instruction"))
        text1_feature= ray.get(context.get.remote("text1_feature"))
        text2_feature= ray.get(context.get.remote("text2_feature"))
        use_online_model= ray.get(context.get.remote("use_online_model"))
        model_folder= ray.get(context.get.remote("model_folder"))
        tokenizer_path= os.path.join(model_folder, "Qwen/Qwen3-32B")
        temperature, max_token, repetition_penalty, top_p= None, None, None, None
        if not use_online_model:
            temperature= ray.get(context.get.remote("temperature"))
            max_token= ray.get(context.get.remote("max_tokens"))
            top_p= ray.get(context.get.remote("top_p"))
            repetition_penalty= ray.get(context.get.remote("repetition_penalty"))
        
        #  all_candidate_journeys
        direct_flights = ray.get(context.get.remote("direct_flights"))
        onestop_flights = ray.get(context.get.remote("onestop_flights"))
        all_candidates = []
        #

        for flight in direct_flights:
            all_candidates.append([flight])
        for journey in onestop_flights:
            all_candidates.append(journey)
        if not all_candidates:
            print("error: ")
            return json.dumps({
                "dag_id": dag_id,
                "status": "failed",
                "information": "error: ",
                "start_time": start_time,
                "end_time": time.time()
            })
        print(f"total {len(all_candidates)} LLM")

        # Build prompt
        prompt = f"""
        You are a professional and meticulous flight booking decision assistant.
        Your task is to select the single most suitable itinerary from the list of candidate itineraries provided below, based on the user's original request and all constraints.

        # User's original request
        "{instruction}"

        # List of candidate itineraries (JSON format)
        Each itinerary is a list containing one or more flights.
        {json.dumps(all_candidates, indent=2)}

        # Your task
        1. Carefully read and understand each of the user's requirements, including but not limited to: time preferences, price preferences (e.g., "cheapest"), airline preferences, number of layovers, etc.
        2. Strictly filter the candidate itineraries according to these requirements.
        3. From the itineraries that meet all conditions, select the single best option.
        4. Return your chosen itinerary in strict JSON format, without any additional explanations, comments, or text. The returned JSON object should be one element from the candidate itinerary list (a list containing one or more flight dictionaries).
        5. If no itinerary can satisfy the user's core requirements, return an empty JSON list `[]`.

        # JSON output
        """
        # Build API payload
        payload = {
            "model": "Qwen/Qwen3-32B",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_token,
            "response_format": {"type": "text"}          
        }
        
        # LLM API
        if use_online_model:
            inference_features, llm_output= query_llm_online(RemoteLlmRoute.from_dag_context(context), payload, tokenizer_path)
        elif backend == "vllm":
            inference_features, llm_output= query_vllm_model(
                RemoteLlmRoute.for_vllm_worker_base(
                    ray.get(context.get.remote(f"task3_llm_fuse_process_filter_and_decide_request_api_url"))
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
        json_str = _extract_json_from_llm_output(llm_output)
        selected_journey = [] #

        try:
            # JSONstring
            selected_journey = json.loads(json_str)
        except Exception as e:
            print(f"JSON: {e}")     
        print(f"Raw LLM output: {llm_output}")
        if isinstance(selected_journey, dict):
            selected_journey = [selected_journey]
        # Nonestringtask
        elif not isinstance(selected_journey, list):
            selected_journey = []            
        
        if not selected_journey or not isinstance(selected_journey, list):
            print(f"error: LLMRaw LLM output: {llm_output}")
        try:
            flight_numbers = [str(f.get('flight_number', 'UNKNOWN')) for f in selected_journey]
            print(f"LLM: {flight_numbers}")
        except (TypeError, AttributeError) as e:
            print(f": {e}")
        context.put.remote("selected_journey", selected_journey)
        return json.dumps({
            "dag_id": dag_id,
            "curr_task_feat": {
                "prompt_length": inference_features["text_length"], 
                "prompt_token_count": inference_features["token_count"], 
                "text1_length": text1_feature["text1_length"],
                "text1_token_count": text1_feature["text1_token_count"],
                "text2_length": text2_feature["text2_length"],
                "text2_token_count": text2_feature["text2_token_count"],
                "reason": 0          
            },
            "start_time": start_time,
            "end_time": time.time()
        })
    except Exception as e:
        print(f"task3_filter_and_decide error: {str(e)}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed",
            "start_time": start_time,
            "end_time": time.time(),
            "result": f"task3_filter_and_decide error: {str(e)}"
        })

@cpu(cpu_num= 1, mem= 1024)
def task4_book_reservation(context):
    """
    Workflow step 4 (tool): book reservation.
    
    """
    print("--- Running Task 4:  ---")
    try:
        start_time= time.time()
        #

        dag_id = ray.get(context.get.remote("dag_id"))
        user_id = ray.get(context.get.remote("user_id"))
        backend_data = ray.get(context.get.remote("backend_data"))
        extracted_info = ray.get(context.get.remote("extracted_info"))
        selected_journey = ray.get(context.get.remote("selected_journey"))
        user_details = ray.get(context.get.remote("user_details"))

        # 1.  - 
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

        # 2. 
        flights_for_booking = []
        for flight in selected_journey:
            flights_for_booking.append({
                "flight_number": flight["flight_number"],
                "date": extracted_info["date"]  # use
            })

        # 3. 
        cabin = extracted_info.get("cabin")
        total_price = sum(flight['prices'][cabin] for flight in selected_journey) * len(passengers)
        total_baggages = extracted_info.get("baggages", 0) * len(passengers)

        #

        nonfree_baggages = total_baggages - len(passengers) if total_baggages > len(passengers) else 0
        total_price += 50 * nonfree_baggages  # $50

        if extracted_info.get("insurance") == "yes":
            total_price += 30 * len(passengers)

        # /
        payment_methods_for_booking = []
        remaining_balance = total_price

        user_payment_methods = user_details.get("payment_methods", {})
        # use
        for pm_id, pm_details in user_payment_methods.items():
            if pm_details["source"] == "certificate" and remaining_balance > 0:
                amount_to_use = min(remaining_balance, pm_details["amount"])
                payment_methods_for_booking.append({"payment_id": pm_id, "amount": amount_to_use})
                remaining_balance -= amount_to_use

        # use
        for pm_id, pm_details in user_payment_methods.items():
            if pm_details["source"] == "gift_card" and remaining_balance > 0:
                amount_to_use = min(remaining_balance, pm_details["amount"])
                payment_methods_for_booking.append({"payment_id": pm_id, "amount": amount_to_use})
                remaining_balance -= amount_to_use

        # use
        if remaining_balance > 0:
            credit_card_id = None
            for pm_id, pm_details in user_payment_methods.items():
                if pm_details["source"] == "credit_card":
                    credit_card_id = pm_id
                    break
            if credit_card_id:
                payment_methods_for_booking.append({"payment_id": credit_card_id, "amount": remaining_balance})
                remaining_balance = 0

        if remaining_balance > 0:
            return json.dumps({
                "dag_id": dag_id,
                "status": "booking failed", 
                "result": "Payment failed: insufficient methods or balance.",
                "start_time": start_time,
                "end_time": time.time()
            })

        #

        booking_args = {
            "user_id": user_id,
            "origin": extracted_info.get("origin"),
            "destination": extracted_info.get("destination"),
            "flight_type": extracted_info.get("flight_type", "one_way"),  #

            "cabin": cabin,
            "flights": flights_for_booking,
            "passengers": passengers,
            "payment_methods": payment_methods_for_booking,
            "total_baggages": total_baggages,
            "nonfree_baggages": nonfree_baggages,
            "insurance": extracted_info.get("insurance", "no")
        }

        #

        result = BookReservation.invoke(backend_data, **booking_args)

        print(f": {result}")
        context.put.remote("booking_result", result)

        print("--- Task 4 finished ---")
        return json.dumps({
            "dag_id": dag_id,
            "status": "done",
            "result": result,
            "start_time": start_time,
            "end_time": time.time()
        })
    except Exception as e:
        print(f"task4_book_reservation error: {str(e)}")
        return json.dumps({
            "dag_id": dag_id,
            "status": "failed", 
            "result": f"task4_book_reservation error: {str(e)}",
            "start_time": start_time,
            "end_time": time.time()
        })