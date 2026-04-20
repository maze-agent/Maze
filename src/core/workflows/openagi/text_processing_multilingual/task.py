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
import numpy as np
from typing import List, Dict, Any, Tuple
from core.scheduler import cpu, gpu, io
from core.utils.remote_llm_route import RemoteLlmRoute
import langid
from transformers import MarianMTModel, MarianTokenizer, pipeline, AutoTokenizer, AutoModelForCausalLM, AutoModelForSeq2SeqLM

LANGUAGE_MAP = {
    "en": "", "zh": "Chinese", "ja": "", "ko": "", "fr": "",
    "de": "", "es": "", "ru": ""
}

def estimate_tokens(text: str) -> int:
    """estimate mixed Chinese/English token count"""
    if not isinstance(text, str): return 0
    cjk_chars = sum(1 for char in text if '\u4E00' <= char <= '\u9FFF')
    non_cjk_text = re.sub(r'[\u4E00-\u9FFF]', ' ', text).replace("\n", " ")
    non_cjk_words_count = len(non_cjk_text.split())
    return cjk_chars + int(non_cjk_words_count * 1.3)

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
    
    async def _query_single(session: aiohttp.ClientSession, messages: List[Dict[str, str]]) -> str:
        payload = {
            "model": model_alias,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_token,
            "top_p": top_p,
            "repetition_penalty": repetition_penalty,
        }
        try:
            async with session.post(chat_url, json=payload, timeout=3600) as response:
                response.raise_for_status()
                response_data = await response.json()
                return response_data['choices'][0]['message']['content'].strip()
        except Exception as e:
            error_msg = f"vLLM async request failed: {str(e)}"
            print(f"❌ {error_msg}")
            return error_msg

    async with aiohttp.ClientSession(headers=headers) as session:
        tasks = [_query_single(session, messages) for messages in messages_list]
        all_responses = await asyncio.gather(*tasks, return_exceptions=True)
        return [str(resp) for resp in all_responses]

def query_vllm_batch_via_service(
    endpoint: RemoteLlmRoute,
    model_alias: str,
    messages_list: List[List[Dict[str, str]]],
    temperature: float = 0.6,
    max_token: int = 1024,
    top_p: float = 0.9,
    repetition_penalty: float = 1.1,
    **kwargs  # model_folder, batch_size, etc.
) -> Tuple[Dict, List[str]]:
    """
    [new] usevLLMservicebatchhandletext generationtask
    - viaAPIrequestbatchedhandle
    -  query_llm_batch aligned
    """
    if not messages_list:
        return {"text_length": 0.0, "token_count": 0.0, "batch_size": 0}, []

    print(f"  -> ⚡️ Starting vLLM batch processing via SERVICE: {len(messages_list)} prompts concurrently.")
    
    batch_answers = asyncio.run(_query_vllm_batch_async(
        endpoint, model_alias, messages_list, temperature, max_token, top_p, repetition_penalty
    ))
    
    #  ( query_llm_batch aligned)
    prompts = [m[0]['content'] for m in messages_list if m]
    total_text_length = np.sum([len(p) for p in prompts])
    total_token_count = np.sum([estimate_tokens(p) for p in prompts])
    
    features = {
        "text_length": float(total_text_length),
        "token_count": float(total_token_count),
        "batch_size": len(batch_answers)
    }
    
    print("  -> ✅ vLLM batch processing via service finished.")
    return features, batch_answers

def query_llm_batch(
    model_folder: str, model_name: str, messages_list: List[List[Dict[str, str]]],
    temperature: float= 0.6, max_token: int= 1024, top_p: float= 0.9, repetition_penalty: float= 1.1,
    batch_size: int= 8
) -> Tuple[Dict, List[str]]:
    """uselocalLLMmodelbatchhandletext generationtaskbatchOOM"""
    model, tokenizer = None, None
    try:
        print(f"  -> Loading batch LLM: {model_name}...")
        tokenizer_path = os.path.join(model_folder, "Qwen/Qwen3-32B")
        model_path = os.path.join(model_folder, model_name)
        tokenizer = AutoTokenizer.from_pretrained(tokenizer_path, padding_side='left', trust_remote_code=True)
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token
        model = AutoModelForCausalLM.from_pretrained(
            model_path, torch_dtype=torch.float16, low_cpu_mem_usage=True,
            device_map="cuda", offload_state_dict= False, trust_remote_code=True
        )
        print("  -> Batch LLM loaded.")
        
        prompts = [tokenizer.apply_chat_template(m, tokenize=False, add_generation_prompt=True) for m in messages_list]
        all_responses = []
        num_prompts = len(prompts)
        num_batches = math.ceil(num_prompts / batch_size)
        print(f"  -> Starting LLM batch processing: {num_prompts} prompts in {num_batches} batches (size={batch_size}).")

        for i in range(0, num_prompts, batch_size):
            batch_prompts = prompts[i:i + batch_size]
            print(f"    -> Processing mini-batch {i // batch_size + 1}/{num_batches}...")
            inputs = tokenizer(batch_prompts, return_tensors='pt', padding=True).to(model.device)
            outputs = model.generate(
                **inputs, max_new_tokens=max_token, temperature=temperature,
                pad_token_id=tokenizer.eos_token_id, top_p=top_p, repetition_penalty=repetition_penalty
            )
            responses = tokenizer.batch_decode(outputs[:, inputs['input_ids'].shape[1]:], skip_special_tokens=True)
            all_responses.extend(responses)

        print("  -> All mini-batches processed.")
        total_text_length = np.sum([len(p) for p in prompts])
        total_token_count = np.sum([estimate_tokens(p) for p in prompts])
        features = {"text_length": float(total_text_length), "token_count": float(total_token_count), "batch_size": len(prompts)}
        return features, all_responses
    finally:
        if model: del model
        if tokenizer: del tokenizer
        gc.collect()
        torch.cuda.empty_cache()
        print("  -> Batch LLM resources released.")

@io(mem= 1024)
def task1_start_receive_task(context):
    """Task 1: receivetask"""
    try:
        print("✅ Task 1: Starting... Verifying initial context.")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))
        question = ray.get(context.get.remote("question"))
        
        target_language = "en"
        if "chinese" in question.lower() or "Chinese" in question.lower(): target_language = "zh"
        elif "german" in question.lower() or "deutsch" in question.lower(): target_language = "de"
        context.put.remote("target_language", target_language)

        print(f"  -> Received dag_id: {dag_id}, target_language: {target_language}")
        return json.dumps({
            "dag_id": dag_id, "status": "success",
            "curr_task_feat": {"instruction_length": len(question)},
            "start_time": start_time, "end_time": time.time()
        })
    except Exception as e:
        print(f"❌ Task 1 failed: {e}")
        return json.dumps({"dag_id": "unknown", "status": "failed", "result": f"Task 1 Error: {e}", "start_time": start_time, "end_time": time.time()})

@io(mem= 1024)
def task2_read_file_and_split_questions(context):
    """Task 2: """
    try:
        print("✅ Task 2: Reading document and splitting questions...")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))
        supplementary_files = ray.get(context.get.remote("supplementary_files"))
        question_str = ray.get(context.get.remote("question"))
        
        document_content = supplementary_files['text.txt'].decode('utf-8')
        context.put.remote("document_content", document_content)
        
        questions_list = [q.strip() for q in question_str.split('\n') if q.strip()]
        num_batches = 3
        batch_size = math.ceil(len(questions_list) / num_batches)
        batches = [questions_list[i:i + batch_size] for i in range(0, len(questions_list), batch_size)] if batch_size > 0 else []
        while len(batches) < num_batches:
            batches.append([])
        context.put.remote("question_batches", batches)
        
        print(f"  -> Read document ({len(document_content)} chars), split {len(questions_list)} questions into {len(batches)} batches.")
        
        return json.dumps({
            "dag_id": dag_id, "status": "success",
            "start_time": start_time, "end_time": time.time()
        })
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id")); start_time = time.time()
        print(f"❌ Task 2 failed: {e}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 2 Error: {e}", "start_time": start_time, "end_time": time.time()})

@cpu(cpu_num= 1, mem= 1024)
def task3_language_detect(context):
    """Task 3: content"""
    try:
        print("✅ Task 3: Detecting document language...")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))
        document_content = ray.get(context.get.remote("document_content"))
        
        lang_code, confidence = langid.classify(document_content)
        context.put.remote("source_language", lang_code)
        
        print(f"  -> Language detected: {LANGUAGE_MAP.get(lang_code, lang_code)} (Confidence: {confidence:.2f})")
        
        return json.dumps({
            "dag_id": dag_id, "status": "success",
            "start_time": start_time, "end_time": time.time()
        })
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id")); start_time = time.time()
        print(f"❌ Task 3 failed: {e}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 3 Error: {e}", "start_time": start_time, "end_time": time.time()})

@gpu(gpu_mem=8192)
def task4_translate_text(context):
    """Task 4: content"""
    try:
        print("✅ Task 4: Translating document...")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))
        document_content = ray.get(context.get.remote("document_content"))
        source_language = ray.get(context.get.remote("source_language"))
        target_language = ray.get(context.get.remote("target_language"))
        max_token=ray.get(context.get.remote("max_tokens"))
        translated_text = document_content
        if source_language != target_language and document_content:
            print(f"  -> Translating from {source_language} to {target_language}...")
            model_name = f"Helsinki-NLP/opus-mt-{source_language}-{target_language}"
            tokenizer = MarianTokenizer.from_pretrained(model_name)
            model = MarianMTModel.from_pretrained(model_name).to("cuda")
            inputs = tokenizer(document_content, return_tensors="pt", padding=True, truncation=True, max_length= max_token).to(model.device)
            translated_ids = model.generate(**inputs)
            translated_text = tokenizer.decode(translated_ids[0], skip_special_tokens=True)
            del model, tokenizer; gc.collect(); torch.cuda.empty_cache()
            print("  -> Translation complete.")
        else:
            print("  -> Source and target language are the same, skipping translation.")
            
        context.put.remote("translated_text", translated_text)
        
        analysis_features = {"text_length": len(translated_text), "token_count": estimate_tokens(translated_text)}
        return json.dumps({
            "dag_id": dag_id, "status": "success",
            "curr_task_feat": {"original_len": len(document_content), "translated_len": len(translated_text)},
            "succ_task_feat": {
                "task5a_text_analysis_summarize": analysis_features,
                "task5b_text_analysis_sentiment": analysis_features
            },
            "start_time": start_time, "end_time": time.time()
        })
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id")); start_time = time.time()
        print(f"❌ Task 4 failed: {e}")
        # task
        context.put.remote("translated_text", ray.get(context.get.remote("document_content")))
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 4 Error: {e}", "start_time": start_time, "end_time": time.time()})

@gpu(gpu_mem=8192)
def task5a_text_analysis_summarize(context):
    """Task 5a (parallel): """
    try:
        print("✅ Task 5a: Summarizing text...")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))
        translated_text = ray.get(context.get.remote("translated_text"))
        model_folder = ray.get(context.get.remote("model_folder"))
        model_path = os.path.join(model_folder, "t5-base")
        tokenizer = AutoTokenizer.from_pretrained(model_path)
        model = AutoModelForSeq2SeqLM.from_pretrained(
            model_path,
            torch_dtype=torch.float16,
            device_map="auto"
        )
        inputs = tokenizer(
            "summarize: " + translated_text,
            return_tensors="pt",
            max_length=512,
            truncation=True
        ).to(model.device)
        outputs = model.generate(
            **inputs,
            max_length=150,
            min_length=30,
            num_beams=4,
            early_stopping=True
        )
        summary = tokenizer.decode(outputs[0], skip_special_tokens=True)
        context.put.remote("summary", summary)
        
        print(f"  -> Summarization complete. Length: {len(summary)}")
        del model, tokenizer; gc.collect(); torch.cuda.empty_cache()
        return json.dumps({
            "dag_id": dag_id, "status": "success",
            "curr_task_feat": {"input_length": len(translated_text), "summary_length": len(summary)},
            "start_time": start_time, "end_time": time.time()
        })
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id")); start_time = time.time()
        print(f"❌ Task 5a failed: {e}")
        context.put.remote("summary", f"Error in summarization: {e}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 5a Error: {e}", "start_time": start_time, "end_time": time.time()})

@gpu(gpu_mem=8192)
def task5b_text_analysis_sentiment(context):
    """Task 5b (parallel): """
    try:
        print("✅ Task 5b: Analyzing text sentiment...")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))
        translated_text = ray.get(context.get.remote("translated_text"))
        model_folder = ray.get(context.get.remote("model_folder"))
        model_path = os.path.join(model_folder, "nlptown", "bert-base-multilingual-uncased-sentiment")
        sentiment_analyzer = pipeline(
            "sentiment-analysis", 
            model=model_path, 
            device="cuda"
        )
        # handle
        max_chunk_size = 512
        chunks = [translated_text[i:i + max_chunk_size] for i in range(0, len(translated_text), max_chunk_size)]
        if not chunks: chunks = [""] # Handle empty text
        results = sentiment_analyzer(chunks)
        
        avg_score = np.mean([res['score'] for res in results])
        #

        main_label = max(set([res['label'] for res in results]), key=[res['label'] for res in results].count)
        sentiment = {'label': main_label, 'score': float(avg_score)}
        context.put.remote("sentiment", sentiment)
        del sentiment_analyzer; gc.collect(); torch.cuda.empty_cache()
        print(f"  -> Sentiment analysis complete. Label: {sentiment['label']}, Score: {sentiment['score']:.2f}")
        return json.dumps({
            "dag_id": dag_id, "status": "success",
            "curr_task_feat": {"text_length": len(translated_text), "sentiment_score": sentiment['score']},
            "start_time": start_time, "end_time": time.time()
        })
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id")); start_time = time.time()
        print(f"❌ Task 5b failed: {e}")
        context.put.remote("sentiment", {"label": "ERROR", "score": 0.0})
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 5b Error: {e}", "start_time": start_time, "end_time": time.time()})

@cpu(cpu_num= 1, mem= 1024)
def task6_prepare_llm_batches(context):
    """Task 6 (merge): LLMbatchtask"""
    try:
        print("✅ Task 6: Preparing final LLM batches and successor features...")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))
        
        #

        translated_text = ray.get(context.get.remote("translated_text"))
        summary = ray.get(context.get.remote("summary"))
        sentiment = ray.get(context.get.remote("sentiment"))
        question_batches = ray.get(context.get.remote("question_batches"))
        instruction = ray.get(context.get.remote("question"))

        succ_task_feat = {}
        batch_keys = ["task7a_llm_process_batch_1", "task7b_llm_process_batch_2", "task7c_llm_process_batch_3"]
        
        for i, batch in enumerate(question_batches):
            if i < len(batch_keys):
                messages_list = []
                if batch:
                    for question in batch:
                        #prompt = f"""task\n\n---  () ---\n{translated_text[:4000]}\n\n---  ---\n: {summary}\n: {sentiment}\n\n---  ---\n{instruction}\n\n---  ---\n{question}\n\n"""
                        #prompt
                        prompt = f"Please generate the final and complete answer strictly according to the user's instruction and specific question.\n\n--- Original Text (possibly translated) ---\n{translated_text[:4000]}\n\n--- Preliminary Analysis ---\nSummary: {summary}\nSentiment: {sentiment}\n\n--- User Instruction ---\n{instruction}\n\n--- Specific Question ---\n{question}"
                        messages_list.append([{"role": "user", "content": prompt}])
                #

                total_len = np.sum([len(m[0]['content']) for m in messages_list])
                total_tok = np.sum([estimate_tokens(m[0]['content']) for m in messages_list])
                succ_task_feat[batch_keys[i]] = {
                    "text_length": float(total_len),
                    "token_count": float(total_tok),
                    "batch_size": len(messages_list),
                    "reason": 0  # usemodel
                }
                #  messages taskuse
                context.put.remote(f"messages_batch_{i+1}", messages_list)

        print(f"  -> Prepared {len(batch_keys)} batches for parallel LLM processing.")
        return json.dumps({
            "dag_id": dag_id, "status": "success",
            "succ_task_feat": succ_task_feat,
            "start_time": start_time, "end_time": time.time()
        })
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id")); start_time = time.time()
        print(f"❌ Task 6 failed: {e}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 6 Error: {e}", "start_time": start_time, "end_time": time.time()})

def _process_llm_batch(context, batch_index: int, batch_name: str, vllm_worker_base: str, backend: str = "huggingface") -> str:
    """handleLLMbatch"""
    print(f"⚙️  Starting LLM processing for {batch_name}...")
    start_time = time.time()
    dag_id = ray.get(context.get.remote("dag_id"))
    messages_list = ray.get(context.get.remote(f"messages_batch_{batch_index+1}"))
    
    if not messages_list:
        print(f"  -> {batch_name} is empty, skipping.")
        context.put.remote(f"{batch_name}_answers", [])
        return json.dumps({
            "dag_id": dag_id, "status": "skipped",
            "curr_task_feat": {"text_length": 0, "token_count": 0, "batch_size": 0, "reason": 0},
            "start_time": start_time, "end_time": time.time()
        })
    if backend== "vllm":
        # usevLLMservicehandle
        features, batch_answers = query_vllm_batch_via_service(
            RemoteLlmRoute.for_vllm_worker_base(vllm_worker_base),
            model_alias="qwen3-32b",
            messages_list=messages_list,
            temperature=ray.get(context.get.remote("temperature")),
            max_token=ray.get(context.get.remote("max_tokens")),
            top_p=ray.get(context.get.remote("top_p")),
            repetition_penalty=ray.get(context.get.remote("repetition_penalty"))
        )
    else:
        features, batch_answers = query_llm_batch(
            model_folder=ray.get(context.get.remote("model_folder")),
            model_name="Qwen/Qwen3-32B",
            messages_list=messages_list,
            temperature=ray.get(context.get.remote("temperature")),
            max_token=ray.get(context.get.remote("max_tokens")),
            top_p=ray.get(context.get.remote("top_p")),
            repetition_penalty=ray.get(context.get.remote("repetition_penalty")),
            batch_size=ray.get(context.get.remote("text_batch_size"))
        )
    context.put.remote(f"{batch_name}_answers", batch_answers)
    
    curr_task_feat = {**features, "reason": 0}
    print(f"✅ {batch_name} finished. Processed {len(batch_answers)} prompts.")
    return json.dumps({
        "dag_id": dag_id, "status": "success",
        "curr_task_feat": curr_task_feat,
        "start_time": start_time, "end_time": time.time()
    })

@gpu(gpu_mem=80000, model_name= "qwen3-32b", backend="huggingface")
def task7a_llm_process_batch_1(context):
    """Task 7a (parallel): process batch1question batch"""
    try:
        backend= task7a_llm_process_batch_1._task_decorator['backend']
        return _process_llm_batch(context, 0, "batch1", ray.get(context.get.remote("task7a_llm_process_batch_1_request_api_url")), backend)
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id")); start_time = time.time()
        print(f"❌ Task 7a failed: {e}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 7a Error: {e}", "start_time": start_time, "end_time": time.time()})

@gpu(gpu_mem=80000, model_name= "qwen3-32b", backend="huggingface")
def task7b_llm_process_batch_2(context):
    """Task 7b (parallel): process batch2question batch"""
    try:
        backend= task7b_llm_process_batch_2._task_decorator['backend']
        return _process_llm_batch(context, 1, "batch2", ray.get(context.get.remote("task7b_llm_process_batch_2_request_api_url")), backend)
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id")); start_time = time.time()
        print(f"❌ Task 7b failed: {e}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 7b Error: {e}", "start_time": start_time, "end_time": time.time()})

@gpu(gpu_mem=80000, model_name= "qwen3-32b", backend="huggingface")
def task7c_llm_process_batch_3(context):
    """Task 7c (parallel): process batch3question batch"""
    try:
        backend= task7c_llm_process_batch_3._task_decorator['backend']
        return _process_llm_batch(context, 2, "batch3", ray.get(context.get.remote("task7c_llm_process_batch_3_request_api_url")), backend)
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id")); start_time = time.time()
        print(f"❌ Task 7c failed: {e}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 7c Error: {e}", "start_time": start_time, "end_time": time.time()})

@io(mem= 1024)
def task8_merge_answers(context):
    """Task 8 (merge): parallelLLManswer"""
    try:
        print("✅ Task 8: Merging all answers from LLM batches...")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))

        batch1 = ray.get(context.get.remote("batch1_answers")) or []
        batch2 = ray.get(context.get.remote("batch2_answers")) or []
        batch3 = ray.get(context.get.remote("batch3_answers")) or []
        
        final_answers = batch1 + batch2 + batch3
        context.put.remote("final_answers", final_answers)
        
        print(f"✅ Task 8: Merged a total of {len(final_answers)} answers.")
        return json.dumps({
            "dag_id": dag_id, "status": "success",
            "curr_task_feat": {"total_answers": len(final_answers)},
            "start_time": start_time, "end_time": time.time()
        })
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id")); start_time = time.time()
        print(f"❌ Task 8 failed: {e}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 8 Error: {e}", "start_time": start_time, "end_time": time.time()})

@io(mem= 1024)
def task9_output_final_answer(context):
    """Task 9: emitfinal answer"""
    try:
        print("✅ Task 9: Formatting final output.")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))
        final_answers = ray.get(context.get.remote("final_answers"))
        
        final_answer_text = '\n'.join(final_answers)
        
        print(f"🏁 Final Answer for DAG {dag_id}:\n{final_answer_text}")
        return json.dumps({
            "dag_id": dag_id, "status": "success", "final_answer": final_answer_text,
            "curr_task_feat": {"final_answer_length": len(final_answer_text), "num_answers": len(final_answers)},
            "start_time": start_time, "end_time": time.time()
        })
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id")); start_time = time.time()
        print(f"❌ Task 9 failed: {e}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 9 Error: {e}", "start_time": start_time, "end_time": time.time()})