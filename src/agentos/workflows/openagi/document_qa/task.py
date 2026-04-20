import os
import re
import gc
import json
import time
import ray
import math
import torch
import asyncio
import aiohttp
import pandas as pd
import numpy as np
from typing import List, Dict, Any, Tuple
from agentos.scheduler import cpu, gpu, io
from agentos.utils.remote_llm_route import RemoteLlmRoute

def estimate_tokens(text: str) -> int:
    """[aligned] estimate mixed Chinese/English token count"""
    if not isinstance(text, str):
        return 0
    cjk_chars = sum(1 for char in text if '\u4E00' <= char <= '\u9FFF')
    non_cjk_text = re.sub(r'[\u4E00-\u9FFF]', ' ', text).replace("\n", " ")
    non_cjk_words_count = len(non_cjk_text.split())
    return cjk_chars + int(non_cjk_words_count * 1.3)

async def _query_single_vllm_endpoint(
    session: aiohttp.ClientSession,
    chat_url: str,
    payload: Dict[str, Any]
) -> str:
    """async singlerequestvLLMcoroutine"""
    try:
        async with session.post(chat_url, json=payload, timeout=3600) as response:
            response.raise_for_status()
            response_data = await response.json()
            return response_data['choices'][0]['message']['content'].strip()
    except Exception as e:
        error_msg = f"vLLM async request failed: {str(e)}"
        print(f"[bold red]{error_msg}")
        return error_msg
    
async def _query_vllm_batch_async(
    route: RemoteLlmRoute,
    model_alias: str,
    messages_list: List[List[Dict[str, str]]],
    temperature: float,
    max_token: int,
    top_p: float,
    repetition_penalty: float
) -> List[str]:
    """Concurrent vLLM chat-completions calls over a resolved :class:`RemoteLlmRoute`."""
    chat_url = route.completions_url
    headers = route.headers()

    async with aiohttp.ClientSession(headers=headers) as session:
        tasks = []
        for messages in messages_list:
            payload = {
                "model": model_alias,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_token,
                "top_p": top_p,
                "repetition_penalty": repetition_penalty,
            }
            tasks.append(_query_single_vllm_endpoint(session, chat_url, payload))
        
        # request
        all_responses = await asyncio.gather(*tasks)
        return all_responses

def query_vllm_batch(
    endpoint: RemoteLlmRoute,
    model_alias: str,
    messages_list: List[List[Dict[str, str]]],
    temperature: float = 0.6,
    max_token: int = 1024,
    top_p: float = 0.9,
    repetition_penalty: float = 1.1
) -> Tuple[Dict, List[str]]:
    """
    [new] usevLLMservicebatchhandletext generationtask
    - asyncioaiohttprequestbatchhandle
    """
    print(f"  -> Starting vLLM batch processing: {len(messages_list)} prompts concurrently.")
    
    # run async main
    batch_answers = asyncio.run(_query_vllm_batch_async(
        endpoint, model_alias, messages_list, temperature, max_token, top_p, repetition_penalty
    ))
    
    #

    total_text_length = 0
    total_token_count = 0
    for messages in messages_list:
        conversation = "".join(f"{m['role']}: {m['content']}" for m in messages)
        total_text_length += len(conversation)
        total_token_count += estimate_tokens(conversation)
        
    features = {
        "text_length": float(total_text_length),
        "token_count": float(total_token_count),
        "batch_size": len(batch_answers)
    }
    
    print("  -> vLLM batch processing finished.")
    return features, batch_answers

def query_llm_batch(
    model_folder: str,
    model_name: str,
    messages_list: List[List[Dict[str, str]]],
    temperature: float= 0.6,
    max_token: int= 1024,
    top_p: float= 0.9,
    repetition_penalty: float= 1.1,
    batch_size: int = 8
) -> Tuple[Dict, List[str]]:
    """
    [] uselocalLLMmodelbatchhandletext generationtask
    - newbatchmini-batchloop to avoidOOM
    """
    from transformers import AutoTokenizer, AutoModelForCausalLM
    import math
    
    model, tokenizer = None, None
    try:
        print(f"  -> Loading batch LLM: {model_name}...")
        tokenizer_path = os.path.join(model_folder, "Qwen/Qwen3-32B")
        model_path = os.path.join(model_folder, model_name)
        tokenizer = AutoTokenizer.from_pretrained(tokenizer_path, padding_side= 'left', trust_remote_code= True)
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token
        model = AutoModelForCausalLM.from_pretrained(
            model_path,
            torch_dtype=torch.float16,
            low_cpu_mem_usage=True,
            device_map="auto",
            trust_remote_code=True
        )
        print("  -> Batch LLM loaded.")
        #  messages  prompt string
        prompts = [tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True) for messages in messages_list]
        all_cleaned_responses = []
        num_prompts = len(prompts)
        num_batches = math.ceil(num_prompts / batch_size)

        print(f"  -> Starting LLM batch processing: {num_prompts} prompts in {num_batches} batches (size={batch_size}).")

        for i in range(0, num_prompts, batch_size):
            batch_prompts = prompts[i:i + batch_size]
            print(f"    -> Processing mini-batch {i // batch_size + 1}/{num_batches}...")
            inputs = tokenizer(batch_prompts, return_tensors='pt', padding=True).to(model.device)
            outputs = model.generate(
                **inputs,
                max_new_tokens=max_token,
                temperature=temperature,
                pad_token_id=tokenizer.eos_token_id,
                top_p=top_p,
                repetition_penalty=repetition_penalty
            )
            responses = tokenizer.batch_decode(outputs[:, inputs['input_ids'].shape[1]:], skip_special_tokens=True)
            
            all_cleaned_responses.extend(responses)
        print("  -> All mini-batches processed.")
        
        avg_text_length = np.sum([len(p) for p in prompts]) if prompts else 0
        avg_token_count = np.sum([estimate_tokens(p) for p in prompts]) if prompts else 0
        features = {"text_length": float(avg_text_length), "token_count": float(avg_token_count), "batch_size": len(all_cleaned_responses)}
        return features, all_cleaned_responses
    finally:
        if model: del model
        if tokenizer: del tokenizer
        gc.collect()
        torch.cuda.empty_cache()
        print("  -> Batch LLM resources released.")

@io(mem= 1024)
def task1_start_receive_task(context):
    """[aligned] Task 1: receivetaskconfirm core parameters"""
    try:
        print("✅ Task 1: Starting... Verifying initial context.")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))
        question = ray.get(context.get.remote("question"))
        supplementary_files = ray.get(context.get.remote("supplementary_files"))
        
        print(f"  -> Received dag_id: {dag_id}, question: '{question[:100]}...', {len(supplementary_files)} files.")
        return json.dumps({
            "dag_id": dag_id, 
            "status": "success",
            "start_time": start_time, 
            "end_time": time.time()
        })
    except Exception as e:
        dag_id = "unknown"; start_time = time.time()
        try: dag_id = ray.get(context.get.remote("dag_id"))
        except: pass
        print(f"❌ task1_start_receive_task failed: {str(e)}")
        return json.dumps({
            "dag_id": dag_id, 
            "status": "failed", 
            "result": f"Task 1 Error: {e}", 
            "start_time": start_time, 
            "end_time": time.time()
            })

@cpu(cpu_num= 1, mem= 1024)
def task2_read_file(context):
    """[aligned] Task 2: filecontent"""
    try:
        print("✅ Task 2: Reading file content...")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))
        supplementary_files = ray.get(context.get.remote("supplementary_files"))
        
        document_content = supplementary_files['context.txt'].decode('utf-8')
        context.put.remote("rare_content", document_content)
        context.put.remote("document_content", document_content)

        prompt_for_task3b = f"Analyze the structure of the following document and provide a brief summary.\n\nDocument (first 3000 chars):\n{document_content[:3000]}"
        succ_text_length = len(prompt_for_task3b)
        succ_token_count = estimate_tokens(prompt_for_task3b)
        print(f"✅ Task 2: Finished. Text content length: {len(document_content)}")
        return json.dumps({
            "dag_id": dag_id, 
            "status": "success",
            "succ_task_feat": {
                "task3b_extract_structure_info": {"text_length": succ_text_length, "token_count": succ_token_count, "reason": 0, "batch_size": 1}
            },
            "start_time": start_time, 
            "end_time": time.time()
        })
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id"))
        start_time = time.time()
        print(f"❌ task2_read_file failed: {str(e)}")
        return json.dumps({"dag_id": dag_id, 
                           "status": "failed", 
                           "result": f"Task 2 Error: {e}", 
                           "start_time": start_time, 
                           "end_time": time.time()
                           })

@cpu(cpu_num= 1, mem= 1024)
def task3a_extract_text_content(context):
    """[modify] Task 3a: (parallel) contenthandle (CPU)"""
    try:
        print("✅ Task 3a: Normalizing extracted text content...")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))
        document_content = ray.get(context.get.remote("document_content"))
        
        unique_lines = [line.strip() for line in document_content.split('\n') if line.strip()]
        processed_text = '\n'.join(dict.fromkeys(unique_lines)) # usedict.fromkeys
        context.put.remote("extracted_text", processed_text)
        
        print(f"✅ Task 3a: Text normalization complete. Length from {len(document_content)} to {len(processed_text)}.")
        return json.dumps({
            "dag_id": dag_id, "status": "success",
            "start_time": start_time, "end_time": time.time()
        })
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id"))
        start_time = time.time()
        print(f"❌ task3a_extract_text_content failed: {str(e)}")
        return json.dumps({
            "dag_id": dag_id, "status": "failed",
            "result": f"Task 3a Error: {e}", "start_time": start_time, 
            "end_time": time.time()})

@gpu(gpu_mem= 80000, model_name= "qwen3-32b", backend="huggingface")
def task3b_llm_process_extract_structure_info(context):
    """[modify] Task 3b: (parallel) useLLM"""
    try:
        backend= task3b_llm_process_extract_structure_info._task_decorator['backend']
        print("✅ Task 3b: Analyzing document structure with LLM...")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))
        document_content = ray.get(context.get.remote("document_content"))
        text_batch_size= ray.get(context.get.remote("text_batch_size"))
        structure_prompt = f"Analyze the structure of the following document and provide a brief summary.\n\nDocument (first 3000 chars):\n{document_content[:3000]}"
        messages = [{"role": "user", "content": structure_prompt}]
        model_folder= ray.get(context.get.remote("model_folder"))
        temperature= ray.get(context.get.remote("temperature"))
        max_token= ray.get(context.get.remote("max_tokens"))
        top_p= ray.get(context.get.remote("top_p"))
        repetition_penalty= ray.get(context.get.remote("repetition_penalty"))
        features, structure_summary = {}, ""
        if backend== "vllm":
            # usevLLMhandle
            features, structure_summary = query_vllm_batch(
                RemoteLlmRoute.for_vllm_worker_base(
                    ray.get(context.get.remote("task3b_llm_process_extract_structure_info_request_api_url"))
                ),
                model_alias="qwen3-32b",
                messages_list= [messages],
                temperature= temperature, max_token= max_token, top_p= top_p, repetition_penalty= repetition_penalty
            )
        else:
            features, structure_summary = query_llm_batch(
                model_folder= model_folder,
                model_name= "Qwen/Qwen3-32B",
                messages_list= [messages],
                temperature= temperature, max_token= max_token, top_p= top_p, repetition_penalty= repetition_penalty,
                batch_size= text_batch_size
            )
        structure_summary = structure_summary[0]
        context.put.remote("doc_structure", structure_summary)

        curr_task_feat = {**features, "reason": 0}
        print("✅ Task 3b: Structure analysis finished.")
        return json.dumps({
            "dag_id": dag_id, "status": "success",
            "curr_task_feat": curr_task_feat,
            "start_time": start_time, "end_time": time.time()
        })
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id"))
        start_time = time.time()
        print(f"❌ task3b_extract_structure_info failed: {str(e)}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 3b Error: {e}", "start_time": start_time, "end_time": time.time()})

@cpu(cpu_num= 1, mem= 1024)
def task3c_load_questions_batch(context):
    """[aligned] Task 3c: (parallel) """
    try:
        print("✅ Task 3c: Loading and batching questions...")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))
        question_str = ray.get(context.get.remote("question"))
        questions_list = [q.strip() for q in question_str.split('\n') if q.strip()]
        num_questions= len(questions_list)
        batch1_size = int(0.2 * num_questions)
        batch2_size = int(0.2 * num_questions)
        batches = [questions_list[:batch1_size], questions_list[batch1_size: batch1_size + batch2_size], questions_list[batch1_size + batch2_size:]]
        
        while len(batches) < 3:
            batches.append([])
            
        context.put.remote("question_batches", batches)
        print(f"✅ Task 3c: Loaded {len(questions_list)} questions into {len(batches)} batches.")
        return json.dumps({
            "dag_id": dag_id, "status": "success",
            "start_time": start_time, "end_time": time.time()
        })
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id"))
        start_time = time.time()
        print(f"❌ task3c_load_questions_batch failed: {str(e)}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 3c Error: {e}", "start_time": start_time, "end_time": time.time()})

@io(mem= 1024)
def task4a_merge_document_analysis(context):
    """[aligned] Task 4a: (merge) content"""
    try:
        print("✅ Task 4a: Merging document analysis results...")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))
        extracted_text = ray.get(context.get.remote("extracted_text"))
        doc_structure = ray.get(context.get.remote("doc_structure"))
        
        merged_analysis = {"content": extracted_text, "structure": doc_structure}
        context.put.remote("merged_document_analysis", merged_analysis)
        
        print("✅ Task 4a: Document analysis merged successfully.")
        return json.dumps({
            "dag_id": dag_id, "status": "success",
            "start_time": start_time, "end_time": time.time()
        })
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id"))
        start_time = time.time()
        print(f"❌ task4a_merge_document_analysis failed: {str(e)}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 4a Error: {e}", "start_time": start_time, "end_time": time.time()})

@cpu(cpu_num= 1, mem= 1024)
def task4b_prepare_qa_context(context):
    """[modify] Task 4b: (merge) QAtask"""
    try:
        print("✅ Task 4b: Preparing final QA context and successor features...")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))
        merged_doc = ray.get(context.get.remote("merged_document_analysis"))
        question_batches = ray.get(context.get.remote("question_batches"))
        
        doc_content = merged_doc["content"][:12000]
        doc_structure = merged_doc["structure"]
        
        qa_context = {
            "document_content": doc_content,
            "document_structure": doc_structure,
            "question_batches": question_batches
        }
        context.put.remote("qa_context", qa_context)
        
        # --- parallelQAtask (succ_task_feat) ---
        succ_task_feat = {}
        batch_keys = ["task5a_llm_process_batch_1", "task5b_llm_process_batch_2", "task5c_llm_process_batch_2"]
        rare_content = ray.get(context.get.remote("rare_content"))
        for i, batch in enumerate(question_batches):
            if i < len(batch_keys):
                if not batch:  #

                    prompt_len, token_count = 0, 0
                    batch_size = 0
                else:
                    prompts = [
                        f"Answer the question.rare_content: {rare_content}\n\nDocument Structure:\n{doc_structure}\n\nContent:\n{doc_content}\n\nQuestion: {question}\n\nAnswer:"
                        for question in batch
                    ]
                    prompt_len = np.sum([len(p) for p in prompts])
                    token_count = np.sum([estimate_tokens(p) for p in prompts])
                    batch_size = len(prompts)

                succ_task_feat[batch_keys[i]] = {
                    "text_length": float(prompt_len),
                    "token_count": float(token_count),
                    "batch_size": batch_size,
                    "reason": 0
                }

        print("✅ Task 4b: Final QA context is ready. Successor features prepared.")
        return json.dumps({
            "dag_id": dag_id, "status": "success",
            "succ_task_feat": succ_task_feat,
            "start_time": start_time, "end_time": time.time()
        })
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id"))
        start_time = time.time()
        print(f"❌ task4b_prepare_qa_context failed: {str(e)}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 4b Error: {e}", "start_time": start_time, "end_time": time.time()})

def _process_question_batch(context, batch_index: int, batch_name: str, vllm_worker_base: str, backend: str = "huggingface"):
    """[] handlebatch (usehandle)"""
    print(f"⚙️  Starting QA processing for {batch_name}...")
    start_time = time.time()
    dag_id = ray.get(context.get.remote("dag_id"))
    qa_context = ray.get(context.get.remote("qa_context"))
    rare_content = ray.get(context.get.remote("rare_content"))
    model_folder = ray.get(context.get.remote("model_folder"))
    temperature = ray.get(context.get.remote("temperature"))
    max_token = ray.get(context.get.remote("max_tokens"))
    repetition_penalty = ray.get(context.get.remote("repetition_penalty"))
    top_p = ray.get(context.get.remote("top_p"))
    batches = qa_context["question_batches"]
    if batch_index >= len(batches) or not batches[batch_index]:
        print(f"  -> {batch_name} is empty, skipping.")
        context.put.remote(f"{batch_name}_answers", [])
        return json.dumps({
            "dag_id": dag_id, "status": "skipped",
            "curr_task_feat": {"text_length": 0, "token_count": 0, "reason": 0, "batch_size": 0},
            "start_time": start_time, "end_time": time.time()
        })

    questions_in_batch = batches[batch_index]
    #  context
    doc_content = qa_context["document_content"]
    context_lines = doc_content.split('\n')
    doc_structure = qa_context["document_structure"]

    messages_list = []
    for idx, question in enumerate(questions_in_batch):
        context_for_this_question = context_lines[idx] if idx < len(context_lines) else ""
        prompt = f"Answer the question.rare_content: {rare_content}\n\nDocument Structure:\n{doc_structure}\n\nContent:\n{context_for_this_question}\n\nQuestion: {question}\n\nAnswer:"
        messages_list.append([{"role": "user", "content": prompt}])
    if backend == "vllm":
        # usevLLMhandle
        features, batch_answers = query_vllm_batch(
            RemoteLlmRoute.for_vllm_worker_base(vllm_worker_base),
            model_alias="qwen3-32b",
            messages_list=messages_list,
            temperature=temperature,
            max_token=max_token,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
        )
    else:
        features, batch_answers = query_llm_batch(
            model_folder=model_folder,
            model_name="Qwen/Qwen3-32B",
            messages_list=messages_list,
            temperature=temperature,
            max_token=max_token,
            top_p=top_p,
            repetition_penalty=repetition_penalty
        )
    context.put.remote(f"{batch_name}_answers", batch_answers)
    curr_task_feat = {**features, "reason": 0}
    print(f"✅ {batch_name} finished. Answered {len(batch_answers)} questions via batch processing.")
    return json.dumps({
        "dag_id": dag_id, "status": "success",
        "curr_task_feat": curr_task_feat,
        "start_time": start_time, "end_time": time.time()
    })

@gpu(gpu_mem= 80000, model_name= "qwen3-32b", backend="huggingface")
def task5a_llm_process_batch_1(context):
    """[aligned] Task 5a: process batch1question batch"""
    try:
        backend= task5a_llm_process_batch_1._task_decorator['backend']
        return _process_question_batch(context, 0, "batch1", ray.get(context.get.remote("task5a_llm_process_batch_1_request_api_url")), backend)
    except Exception as e:
        start_time = time.time(); dag_id = ray.get(context.get.remote("dag_id"))
        print(f"❌ task5a failed: {e}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 5a Error: {e}", "start_time": start_time, "end_time": time.time()})

@gpu(gpu_mem= 80000, model_name= "qwen3-32b", backend="huggingface")
def task5b_llm_process_batch_2(context):
    """[aligned] Task 5b: process batch2question batch"""
    try:
        backend= task5a_llm_process_batch_1._task_decorator['backend']
        return _process_question_batch(context, 1, "batch2", ray.get(context.get.remote("task5b_llm_process_batch_2_request_api_url")), backend)
    except Exception as e:
        start_time = time.time(); dag_id = ray.get(context.get.remote("dag_id"))
        print(f"❌ task5b failed: {e}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 5b Error: {e}", "start_time": start_time, "end_time": time.time()})

@gpu(gpu_mem= 80000, model_name= "qwen3-32b", backend="huggingface")
def task5c_llm_process_batch_3(context, backend="huggingface"):
    """[aligned] Task 5c: process batch3question batch"""
    try:
        backend= task5a_llm_process_batch_1._task_decorator['backend']
        return _process_question_batch(context, 2, "batch3", ray.get(context.get.remote("task5c_llm_process_batch_3_request_api_url")), backend)
    except Exception as e:
        start_time = time.time(); dag_id = ray.get(context.get.remote("dag_id"))
        print(f"❌ task5c failed: {e}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 5c Error: {e}", "start_time": start_time, "end_time": time.time()})

@io(mem= 1024)
def task7_merge_all_answers(context):
    """[aligned] Task 7: (merge) answer"""
    try:
        print("✅ Task 7: Merging all answers from QA batches...")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))

        # use try-except 
        batch1 = ray.get(context.get.remote("batch1_answers")) or []
        batch2 = ray.get(context.get.remote("batch2_answers")) or []
        batch3 = ray.get(context.get.remote("batch3_answers")) or []
        
        final_answers = batch1 + batch2 + batch3
        context.put.remote("final_answers", final_answers)
        
        print(f"✅ Task 7: Merged a total of {len(final_answers)} answers.")
        return json.dumps({
            "dag_id": dag_id, "status": "success",
            "start_time": start_time, "end_time": time.time()
        })
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id")); start_time = time.time()
        print(f"❌ task7_merge_all_answers failed: {str(e)}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 7 Error: {e}", "start_time": start_time, "end_time": time.time()})

@io(mem= 1024)
def task8_output_final_answer(context):
    """[aligned] Task 8: emitfinal answer"""
    try:
        print("✅ Task 8: Formatting final output.")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))
        final_answers = ray.get(context.get.remote("final_answers"))
        final_answer_text = '\n'.join(final_answers)
        print(f"🏁 Final Answer for DAG {dag_id}:\n{final_answer_text}")
        return json.dumps({
            "dag_id": dag_id, "status": "success", "final_answer": final_answer_text,
            "start_time": start_time, "end_time": time.time()
        })
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id")); start_time = time.time()
        print(f"❌ task8_output_final_answer failed: {str(e)}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 8 Error: {e}", "start_time": start_time, "end_time": time.time()})