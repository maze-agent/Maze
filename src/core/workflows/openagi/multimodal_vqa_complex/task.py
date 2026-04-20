import os
import re
import gc
import json
import time
import ray
from typing import List, Dict, Any
from io import BytesIO
import math
import asyncio
import aiohttp
#  AgentOS 
from core.scheduler import cpu, gpu, io
from core.utils.remote_llm_route import RemoteLlmRoute

# handlemodel
import numpy as np
import cv2
from PIL import Image
import torch
from transformers import AutoProcessor, AutoTokenizer, Qwen2_5_VLForConditionalGeneration
from scipy.stats import entropy
import pandas as pd

# ==============================================================================
#  1.  (Utilities)
# ==============================================================================

import base64

def encode_image_bytes_base64(img_bytes: bytes) -> str:
    """[new] encode image bytes asbase64string"""
    return base64.b64encode(img_bytes).decode('utf-8')

# =================================================================
#               ⬇️ new vLLM  ⬇️
# =================================================================
async def _query_vlm_batch_async(
    route: RemoteLlmRoute,
    model_alias: str,
    image_requests: List[Dict[str, Any]],
    temperature: float,
    max_token: int,
    top_p: float,
    repetition_penalty: float
) -> List[str]:
    """Concurrent VLM chat-completions calls over a resolved :class:`RemoteLlmRoute`."""
    chat_url = route.completions_url
    headers = route.headers()

    async def _query_single(session: aiohttp.ClientSession, request: Dict[str, Any]) -> str:
        """async singleVLMrequestcoroutine"""
        # 1. Base64
        base64_image = encode_image_bytes_base64(request['content'])
        
        # 2. OpenAIpayload
        payload = {
            "model": model_alias,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            }
                        },
                        {
                            "type": "text",
                            "text": request['prompt']
                        }
                    ]
                }
            ],
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
            error_msg = f"VLM async request failed: {str(e)}"
            print(f"❌ {error_msg}")
            return error_msg

    async with aiohttp.ClientSession(headers=headers) as session:
        tasks = [_query_single(session, req) for req in image_requests]
        all_responses = await asyncio.gather(*tasks, return_exceptions=True)
        return [str(resp) for resp in all_responses]

def query_vlm_batch_via_service(
    endpoint: RemoteLlmRoute,
    model_alias: str,
    image_requests: List[Dict[str, Any]],
    temperature: float = 0.6,
    max_token: int = 1024,
    top_p: float = 0.9,
    repetition_penalty: float = 1.1,
    **kwargs  # model_folder, batch_size, etc.
) -> List[str]:
    """
    [new] usevLLMservicebatchhandleimage captiontask
    - viaAPIrequestbatchedhandle
    - compatible with local query_vlm_batch compatible
    """
    if not image_requests:
        return []

    print(f"  -> ⚡️ Starting VLM batch processing via SERVICE: {len(image_requests)} images concurrently.")
    
    # run async main
    descriptions = asyncio.run(_query_vlm_batch_async(
        endpoint, model_alias, image_requests, temperature, max_token, top_p, repetition_penalty
    ))
    
    print("  -> ✅ VLM batch processing via service finished.")
    return descriptions

def estimate_tokens(text: str) -> int:
    """estimate mixed Chinese/English token count"""
    if not isinstance(text, str): return 0
    cjk_chars = sum(1 for char in text if '\u4E00' <= char <= '\u9FFF')
    non_cjk_text = re.sub(r'[\u4E00-\u9FFF]', ' ', text).replace("\n", " ")
    non_cjk_words_count = len(non_cjk_text.split())
    return cjk_chars + int(non_cjk_words_count * 1.3)

def extract_vision_features(image_bytes: bytes) -> Dict[str, float]:
    """extract per imageVLMprediction featurescount"""
    img_array = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    if img is None: return {}
    
    h, w, _ = img.shape
    gray_img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    
    features = {
        "image_height": h, "image_width": w, "image_area": h * w,
        "image_aspect_ratio": h / w if w > 0 else 1,
    }
    
    #

    hist = cv2.calcHist([gray_img], [0], None, [256], [0, 256])
    prob_dist = hist / (hist.sum() + 1e-6)
    features["image_entropy"] = entropy(prob_dist.flatten())
    
    #

    edges = cv2.Canny(gray_img, 100, 200)
    features["edge_density"] = np.sum(edges > 0) / (h * w) if (h * w) > 0 else 0
    features["avg_brightness"] = np.mean(gray_img)
    features["brightness_variance"] = np.var(gray_img)

    # === new ===
    # +
    _, thresh = cv2.threshold(gray_img, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kernel = np.ones((3, 3), np.uint8)
    opening = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=1)
    contours, _ = cv2.findContours(opening, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    text_area = 0
    valid_contours = [cnt for cnt in contours if cv2.contourArea(cnt) > 5]
    for cnt in valid_contours:
        text_area += cv2.contourArea(cnt)
    features["text_area_ratio"] = text_area / (h * w) if h * w > 0 else 0
    features["text_block_count"] = len(valid_contours)
    # === new ===

    return features

def query_vlm_batch(
    model_folder: str,
    model_name: str,
    image_requests: List[Dict[str, Any]],
    temperature: float = 0.6,
    max_token: int = 1024,
    top_p: float = 0.9,
    repetition_penalty: float = 1.1,
    batch_size: int = 8
) -> List[str]:
    """uselocalVLMmodelbatchhandletask"""
    from rich.console import Console

    console = Console()
    model_path = os.path.join(model_folder, model_name)
    processor, model, tokenizer = None, None, None

    try:
        console.print(f"🔯 [bold green]loadingVLMmodel (usepadding)... ({model_path})[/bold green]")
        tokenizer = AutoTokenizer.from_pretrained(model_path, padding_side='left', trust_remote_code=True)
        processor = AutoProcessor.from_pretrained(model_path, tokenizer=tokenizer, trust_remote_code=True)
        model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            model_path, torch_dtype=torch.float16, low_cpu_mem_usage=True, device_map="cuda", offload_state_dict= False, trust_remote_code=True
        )
        console.print("✅ [bold green]VLMmodelloaded[/bold green]")
        
        all_final_responses = []
        num_requests = len(image_requests)
        num_batches = math.ceil(num_requests / batch_size)

        console.print(f"⚙️ [bold]total {num_requests} images, split into {num_batches}  batcheshandle (per batch {batch_size} image(s))[/bold]")

        for i in range(0, num_requests, batch_size):
            batch_requests = image_requests[i:i + batch_size]
            console.print(f"--- currentlyhandlebatch {i // batch_size + 1} / {num_batches} ---")
            
            texts_for_processing, images_for_processing = [], []
            for request in batch_requests:
                messages = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": request['prompt']}]}]
                text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
                texts_for_processing.append(text)
                image = Image.open(BytesIO(request['content'])).convert("RGB")
                images_for_processing.append(image)

            inputs = processor(text=texts_for_processing, images=images_for_processing, padding=True, return_tensors="pt").to(model.device)
            output_ids = model.generate(**inputs, max_new_tokens=max_token, temperature=temperature, top_p=top_p, repetition_penalty=repetition_penalty)
            
            responses = processor.batch_decode(output_ids, skip_special_tokens=True)
            cleaned_responses = [resp.split("assistant\n")[-1].lstrip() for resp in responses]
            all_final_responses.extend(cleaned_responses)
        
        console.print("✅ [bold green]all batcheshandledone[/bold green]")
        return all_final_responses
    except Exception as e:
        console.print(f"[bold red]VLMbatchhandlefailed: {e}")
        import traceback; traceback.print_exc()
        return [f"Error during VLM batch processing: {e}" for _ in image_requests]
    finally:
        if model: del model
        if processor: del processor
        if tokenizer: del tokenizer
        gc.collect()
        torch.cuda.empty_cache()
        console.print("🗑️ [yellow]modelGPUresources released[/yellow]")

# ==============================================================================
#  2. Workflow Task 
# ==============================================================================

@io(mem= 1024)
def task1_start_receive_task(context):
    """Task 1: [aligned] receivetaskconfirm core parameters"""
    try:
        print("✅ Task 1: Starting... Verifying initial context.")
        start_time = time.time()
        # Loader
        dag_id = ray.get(context.get.remote("dag_id"))
        question = ray.get(context.get.remote("question"))
        supplementary_files = ray.get(context.get.remote("supplementary_files"))
        
        print(f"  -> Received dag_id: {dag_id}")
        print(f"  -> Received question: '{question[:100]}...'")
        print(f"  -> Received {len(supplementary_files)} supplementary files.")

        return json.dumps({
            "dag_id": dag_id, "status": "success", "message": "Initial context verified.",
            "start_time": start_time, "end_time": time.time()
        })
    except Exception as e:
        dag_id = "unknown"; start_time = time.time()
        try: dag_id = ray.get(context.get.remote("dag_id"))
        except: pass
        print(f"❌ task1_start_receive_task failed: {str(e)}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 1 Error: {e}", "start_time": start_time, "end_time": time.time()})

@cpu(cpu_num= 1, mem= 1024)
def task2_read_file(context):
    """Task 2: [aligned] file"""
    try:
        print("✅ Task 2: Reading image files...")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))
        supplementary_files = ray.get(context.get.remote("supplementary_files"))
        
        # file
        image_files = {k: v for k, v in supplementary_files.items() if k.lower().endswith(('.png', '.jpg', '.jpeg'))}
        for name, content in image_files.items():
            print(f"[DEBUG] image file: {name}, content length: {len(content)}")
        all_images = [{"file_name": name, "content": content} for name, content in image_files.items()]

        context.put.remote("all_images", all_images)
        print(f"✅ Task 2: Finished. Read {len(all_images)} images.")
        
        total_size = sum(len(img['content']) for img in all_images)
        features_for_next = {"num_images": len(all_images), "total_size_bytes": total_size}
        
        return json.dumps({
            "dag_id": dag_id, "status": "success",
            "curr_task_feat": features_for_next,
            "succ_task_feat": {"task3_file_process": features_for_next},
            "start_time": start_time, "end_time": time.time()
        })
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id"))
        print(f"❌ task2_read_file failed: {str(e)}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 2 Error: {e}", "start_time": time.time(), "end_time": time.time()})

@cpu(cpu_num= 1, mem= 1024)
def task3_file_process(context):
    """Task 3: [modify] VLMtask"""
    try:
        print("✅ Task 3: Enhancing, standardizing images, and preparing prediction features...")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))
        all_images = ray.get(context.get.remote("all_images"))
        
        processed_images = []
        all_vision_features = []
        
        # definemodelhandle
        MAX_DIMENSION = 1024

        for image_info in all_images:
            img_array = np.frombuffer(image_info['content'], np.uint8)
            img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
            if img is None:
                print(f"❌ failed: {image_info['file_name']}")
                continue

            # --- [] new ---
            h, w, _ = img.shape
            if h > MAX_DIMENSION or w > MAX_DIMENSION:
                #

                scale = MAX_DIMENSION / max(h, w)
                new_w, new_h = int(w * scale), int(h * scale)
                img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
                print(f"  ->  '{image_info['file_name']}'  {w}x{h}  {new_w}x{new_h}")
            # ---  ---

            #

            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            blur_score = cv2.Laplacian(gray, cv2.CV_64F).var()
            if blur_score < 100:
                kernel = np.array([[-1, -1, -1], [-1, 9, -1], [-1, -1, -1]])
                img = cv2.filter2D(img, -1, kernel)
            
            #

            enhanced_img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(enhanced_img_rgb)
            buffer = BytesIO()
            pil_img.save(buffer, format='JPEG')
            enhanced_content = buffer.getvalue()
            
            processed_images.append({"file_name": image_info['file_name'], "content": enhanced_content})
            
            #

            vision_features = extract_vision_features(enhanced_content)
            all_vision_features.append(vision_features)

        # --- new ---
        batch_sizes = [2, 2, 2, 4]
        batch_features = []
        start_idx = 0
        question = ray.get(context.get.remote("question"))
        prompt_template = "Carefully observe this image and answer the following question: {}"
        prompt = prompt_template.format(question)
        prompt_length = len(prompt)
        prompt_token_count = estimate_tokens(prompt)

        for size in batch_sizes:
            end_idx = start_idx + size
            batch = all_vision_features[start_idx:end_idx]
            #

            feature_keys = [
                "image_height", "image_width", "image_area", "image_aspect_ratio",
                "image_entropy", "edge_density", "avg_brightness", "brightness_variance",
                "text_area_ratio", "text_block_count"
            ]
            if batch:
                df = pd.DataFrame(batch)
                batch_mean = df.mean().to_dict()
                batch_mean = {str(k): v for k, v in batch_mean.items()}
                #

                for k in feature_keys:
                    if k not in batch_mean:
                        batch_mean[k] = None
                batch_mean["batch_size"] = len(batch)
                batch_mean["text_length"] = prompt_length
                batch_mean["token_count"] = prompt_token_count
                batch_mean["prompt_length"] = prompt_length
                batch_mean["prompt_token_count"] = prompt_token_count
                batch_mean["reason"] = 0
                batch_features.append(batch_mean)
            else:
                #

                empty_feat = {k: None for k in feature_keys}
                empty_feat.update({
                    "batch_size": 0,
                    "text_length": prompt_length,
                    "token_count": prompt_token_count,
                    "prompt_length": prompt_length,
                    "prompt_token_count": prompt_token_count,
                    "reason": 0
                })
                batch_features.append(empty_feat)
            start_idx = end_idx
        #  context
        context.put.remote("vision_features_a", batch_features[0])
        context.put.remote("vision_features_b", batch_features[1])
        context.put.remote("vision_features_c", batch_features[2])
        context.put.remote("vision_features_d", batch_features[3])
        # ---  ---
        if not all_vision_features:
            aggregated_features = {}
        else:
            df = pd.DataFrame(all_vision_features)
            aggregated_features = df.mean().to_dict()
            #  key 
            aggregated_features = {str(k): v for k, v in aggregated_features.items()}
        
        context.put.remote("aggregated_vision_features", aggregated_features)
        context.put.remote("processed_images", processed_images)
        print(f"✅ Task 3: Standardization and enhancement complete. Processed {len(processed_images)} images.")
        
        return json.dumps({
            "succ_task_feat": {
                "task4a_vlm_process": batch_features[0],
                "task4b_vlm_process": batch_features[1],
                "task4c_vlm_process": batch_features[2],
                "task4d_vlm_process": batch_features[3]},
            "dag_id": dag_id, "status": "success",
            "start_time": start_time, "end_time": time.time()
        })
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id"))
        print(f"❌ task3_file_process failed: {str(e)}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 3 Error: {e}", "start_time": start_time, "end_time": time.time()})

# --- Task4 ---

@gpu(gpu_mem=80000, model_name= "qwen2.5-vl-32b", backend="huggingface")
def task4a_vlm_process(context):
    """[Parallel Worker A] useVLMfor first20%image QA answer"""
    task_id = "4a"
    try:
        backend= task4a_vlm_process._task_decorator['backend']
        print(f"✅ Task {task_id}: Starting VLM processing for first 20%...")
        start_time = time.time()
        
        dag_id = ray.get(context.get.remote("dag_id"))
        question = ray.get(context.get.remote("question"))
        model_folder = ray.get(context.get.remote("model_folder"))
        vlm_batch_size = ray.get(context.get.remote("vlm_batch_size"))
        temperature = ray.get(context.get.remote("temperature"))
        max_tokens = ray.get(context.get.remote("max_tokens"))
        top_p = ray.get(context.get.remote("top_p"))
        repetition_penalty = ray.get(context.get.remote("repetition_penalty"))
        
        processed_images = ray.get(context.get.remote("processed_images"))
        total = len(processed_images)
        idx1 = int(total * 0.2)
        batch_to_process = processed_images[:idx1]
        print(f"Task {task_id}: Processing {len(batch_to_process)} items.")

        image_requests = []
        for image_info in batch_to_process:
            image_requests.append({
                "prompt": f"Carefully observe this image and answer the following question: {question}",
                "content": image_info["content"],
                "file_name": image_info["file_name"]
            })
        if backend == "vllm":
            # usevLLMservice
            results = query_vlm_batch_via_service(
                RemoteLlmRoute.for_vllm_worker_base(
                    ray.get(context.get.remote("task4a_vlm_process_request_api_url"))
                ),
                model_alias="qwen2.5-vl-32b",
                image_requests=image_requests,
                temperature=temperature,
                max_token=max_tokens,
                top_p=top_p,
                repetition_penalty=repetition_penalty,
                batch_size=vlm_batch_size
            )
        else:
            results = query_vlm_batch(
                model_folder=model_folder,
                model_name="Qwen/Qwen2.5-VL-32B-Instruct",
                image_requests=image_requests,
                temperature=temperature,
                max_token=max_tokens,
                top_p=top_p,
                repetition_penalty=repetition_penalty,
                batch_size= vlm_batch_size
            )
        final_answers_a = []
        for i, answer in enumerate(results):
            final_answers_a.append({"file_name": batch_to_process[i]["file_name"], "answer": answer})
        
        context.put.remote("final_answers_a", final_answers_a)
        feature_4a = ray.get(context.get.remote("vision_features_a"))
        if feature_4a is None or not isinstance(feature_4a, dict):
            feature_4a = {}
        feature_4a = dict(feature_4a)
        feature_4a["batch_size"] = len(batch_to_process)
        #

        if "text_length" in feature_4a:
            feature_4a["prompt_length"] = feature_4a.pop("text_length")
        if "token_count" in feature_4a:
            feature_4a["prompt_token_count"] = feature_4a.pop("token_count")
        print(feature_4a)
        print(f"✅ Task {task_id}: Processing complete.")
        return json.dumps({
            "curr_task_feat": feature_4a,
            "dag_id": dag_id, "status": "success", "task": task_id,
            "start_time": start_time, "end_time": time.time()
            })

    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id"))
        print(f"❌ {task_id}_vlm_process failed: {str(e)}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task {task_id} Error: {e}"
                           , "start_time": start_time, "end_time": time.time()})

@gpu(gpu_mem=80000, model_name= "qwen2.5-vl-32b", backend="huggingface")
def task4b_vlm_process(context):
    """[Parallel Worker B] useVLMfor chunk20%image QA answer"""
    task_id = "4b"
    try:
        backend= task4b_vlm_process._task_decorator['backend']
        print(f"✅ Task {task_id}: Starting VLM processing for second 20%...")
        start_time = time.time()
        
        dag_id = ray.get(context.get.remote("dag_id"))
        question = ray.get(context.get.remote("question"))
        model_folder = ray.get(context.get.remote("model_folder"))
        vlm_batch_size = ray.get(context.get.remote("vlm_batch_size"))
        temperature = ray.get(context.get.remote("temperature"))
        max_tokens = ray.get(context.get.remote("max_tokens"))
        top_p = ray.get(context.get.remote("top_p"))
        repetition_penalty = ray.get(context.get.remote("repetition_penalty"))
        
        processed_images = ray.get(context.get.remote("processed_images"))
        total = len(processed_images)
        idx1 = int(total * 0.2)
        idx2 = int(total * 0.4)
        batch_to_process = processed_images[idx1:idx2]
        print(f"Task {task_id}: Processing {len(batch_to_process)} items.")

        image_requests = []
        for image_info in batch_to_process:
            image_requests.append({
                "prompt": f"Carefully observe this image and answer the following question: {question}",
                "content": image_info["content"],
                "file_name": image_info["file_name"]
            })
        if backend== "vllm":
            # usevLLMservice
            results = query_vlm_batch_via_service(
                RemoteLlmRoute.for_vllm_worker_base(
                    ray.get(context.get.remote("task4b_vlm_process_request_api_url"))
                ),
                model_alias="qwen2.5-vl-32b",
                image_requests=image_requests,
                temperature=temperature,
                max_token=max_tokens,
                top_p=top_p,
                repetition_penalty=repetition_penalty,
                batch_size=vlm_batch_size
            )
        else:
            results = query_vlm_batch(
                model_folder=model_folder,
                model_name="Qwen/Qwen2.5-VL-32B-Instruct",
                image_requests=image_requests,
                temperature=temperature,
                max_token=max_tokens,
                top_p=top_p,
                repetition_penalty=repetition_penalty,
                batch_size= vlm_batch_size
            )
        final_answers_b = []
        for i, answer in enumerate(results):
            final_answers_b.append({"file_name": batch_to_process[i]["file_name"], "answer": answer})
        
        context.put.remote("final_answers_b", final_answers_b)
        feature_4b = ray.get(context.get.remote("vision_features_b"))
        if feature_4b is None or not isinstance(feature_4b, dict):
            feature_4b = {}
        feature_4b = dict(feature_4b)
        feature_4b["batch_size"] = len(batch_to_process)
        if "text_length" in feature_4b:
            feature_4b["prompt_length"] = feature_4b.pop("text_length")
        if "token_count" in feature_4b:
            feature_4b["prompt_token_count"] = feature_4b.pop("token_count")
        print(f"✅ Task {task_id}: Processing complete.")
        return json.dumps({
            "curr_task_feat": feature_4b,
            "dag_id": dag_id, "status": "success", "task": task_id,
            "start_time": start_time, "end_time": time.time()
            })

    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id"))
        print(f"❌ {task_id}_vlm_process failed: {str(e)}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task {task_id} Error: {e}"
                            , "start_time": start_time, "end_time": time.time()
                           })

@gpu(gpu_mem=80000, model_name= "qwen2.5-vl-32b", backend="huggingface")
def task4c_vlm_process(context):
    """[Parallel Worker C] useVLMfor chunk20%image QA answer"""
    task_id = "4c"
    try:
        backend= task4c_vlm_process._task_decorator['backend']
        print(f"✅ Task {task_id}: Starting VLM processing for third 20%...")
        start_time = time.time()
        
        dag_id = ray.get(context.get.remote("dag_id"))
        question = ray.get(context.get.remote("question"))
        model_folder = ray.get(context.get.remote("model_folder"))
        vlm_batch_size = ray.get(context.get.remote("vlm_batch_size"))
        temperature = ray.get(context.get.remote("temperature"))
        max_tokens = ray.get(context.get.remote("max_tokens"))
        top_p = ray.get(context.get.remote("top_p"))
        repetition_penalty = ray.get(context.get.remote("repetition_penalty"))
        
        processed_images = ray.get(context.get.remote("processed_images"))
        total = len(processed_images)
        idx2 = int(total * 0.4)
        idx3 = int(total * 0.6)
        batch_to_process = processed_images[idx2:idx3]
        print(f"Task {task_id}: Processing {len(batch_to_process)} items.")

        image_requests = []
        for image_info in batch_to_process:
            image_requests.append({
                "prompt": f"Carefully observe this image and answer the following question: {question}",
                "content": image_info["content"],
                "file_name": image_info["file_name"]
            })
        if backend== "vllm":
            # usevLLMservice
            results = query_vlm_batch_via_service(
                RemoteLlmRoute.for_vllm_worker_base(
                    ray.get(context.get.remote("task4c_vlm_process_request_api_url"))
                ),
                model_alias="qwen2.5-vl-32b",
                image_requests=image_requests,
                temperature=temperature,
                max_token=max_tokens,
                top_p=top_p,
                repetition_penalty=repetition_penalty,
                batch_size=vlm_batch_size
            )
        else:
            results = query_vlm_batch(
                model_folder=model_folder,
                model_name="Qwen/Qwen2.5-VL-32B-Instruct",
                image_requests=image_requests,
                temperature=temperature,
                max_token=max_tokens,
                top_p=top_p,
                repetition_penalty=repetition_penalty,
                batch_size= vlm_batch_size
            )
        final_answers_c = []
        for i, answer in enumerate(results):
            final_answers_c.append({"file_name": batch_to_process[i]["file_name"], "answer": answer})
        
        context.put.remote("final_answers_c", final_answers_c)
        feature_4c = ray.get(context.get.remote("vision_features_c"))
        if feature_4c is None or not isinstance(feature_4c, dict):
            feature_4c = {}
        feature_4c = dict(feature_4c)
        feature_4c["batch_size"] = len(batch_to_process)
        if "text_length" in feature_4c:
            feature_4c["prompt_length"] = feature_4c.pop("text_length")
        if "token_count" in feature_4c:
            feature_4c["prompt_token_count"] = feature_4c.pop("token_count")
        print(f"✅ Task {task_id}: Processing complete.")
        return json.dumps({
            "curr_task_feat": feature_4c,
            "dag_id": dag_id, "status": "success", "task": task_id,
            "start_time": start_time, "end_time": time.time()
            })

    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id"))
        print(f"❌ {task_id}_vlm_process failed: {str(e)}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task {task_id} Error: {e}"
                            , "start_time": start_time, "end_time": time.time()
                           })
    
@gpu(gpu_mem=80000, model_name= "qwen2.5-vl-32b", backend="huggingface")
def task4d_vlm_process(context):
    """[Parallel Worker D] useVLMfor last40%image QA answer"""
    task_id = "4d"
    try:
        backend= task4d_vlm_process._task_decorator['backend']
        print(f"✅ Task {task_id}: Starting VLM processing for last 40%...")
        start_time = time.time()
        
        dag_id = ray.get(context.get.remote("dag_id"))
        question = ray.get(context.get.remote("question"))
        model_folder = ray.get(context.get.remote("model_folder"))
        vlm_batch_size = ray.get(context.get.remote("vlm_batch_size"))
        temperature = ray.get(context.get.remote("temperature"))
        max_tokens = ray.get(context.get.remote("max_tokens"))
        top_p = ray.get(context.get.remote("top_p"))
        repetition_penalty = ray.get(context.get.remote("repetition_penalty"))
        
        processed_images = ray.get(context.get.remote("processed_images"))
        total = len(processed_images)
        idx3 = int(total * 0.6)
        batch_to_process = processed_images[idx3:]
        print(f"Task {task_id}: Processing {len(batch_to_process)} items.")

        image_requests = []
        for image_info in batch_to_process:
            image_requests.append({
                "prompt": f"Carefully observe this image and answer the following question: {question}",
                "content": image_info["content"],
                "file_name": image_info["file_name"]
            })
        if backend== "vllm":
            # usevLLMservice
            results = query_vlm_batch_via_service(
                RemoteLlmRoute.for_vllm_worker_base(
                    ray.get(context.get.remote("task4d_vlm_process_request_api_url"))
                ),
                model_alias="qwen2.5-vl-32b",
                image_requests=image_requests,
                temperature=temperature,
                max_token=max_tokens,
                top_p=top_p,
                repetition_penalty=repetition_penalty,
                batch_size=vlm_batch_size
            )
        else:
            results = query_vlm_batch(
                model_folder=model_folder,
                model_name="Qwen/Qwen2.5-VL-32B-Instruct",
                image_requests=image_requests,
                temperature=temperature,
                max_token=max_tokens,
                top_p=top_p,
                repetition_penalty=repetition_penalty,
                batch_size= vlm_batch_size
            )
        final_answers_d = []
        for i, answer in enumerate(results):
            final_answers_d.append({"file_name": batch_to_process[i]["file_name"], "answer": answer})
        
        context.put.remote("final_answers_d", final_answers_d)
        feature_4d = ray.get(context.get.remote("vision_features_d"))
        if feature_4d is None or not isinstance(feature_4d, dict):
            feature_4d = {}
        feature_4d = dict(feature_4d)
        feature_4d["batch_size"] = len(batch_to_process)
        if "text_length" in feature_4d:
            feature_4d["prompt_length"] = feature_4d.pop("text_length")
        if "token_count" in feature_4d:
            feature_4d["prompt_token_count"] = feature_4d.pop("token_count")
        print(f"✅ Task {task_id}: Processing complete.")
        return json.dumps({
            "curr_task_feat": feature_4d,
            "dag_id": dag_id, "status": "success", "task": task_id
            , "start_time": start_time, "end_time": time.time()
            })

    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id"))
        print(f"❌ {task_id}_vlm_process failed: {str(e)}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task {task_id} Error: {e}"
                            , "start_time": start_time, "end_time": time.time()
                           })
    
@io(mem= 1024)
def task4_merge_results(context):
    """[Merge] task4a, 4b, 4c, 4dparallelhandle"""
    task_id = "4_merge"
    try:
        print(f"✅ Task {task_id}: Merging parallel VLM results...")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))

        results_a = ray.get(context.get.remote("final_answers_a"))
        results_b = ray.get(context.get.remote("final_answers_b"))
        results_c = ray.get(context.get.remote("final_answers_c"))
        results_d = ray.get(context.get.remote("final_answers_d"))

        all_final_answers = results_a + results_b + results_c + results_d
        
        # task4emitaligned
        total_answer_length = sum(len(str(ans.get('answer', ''))) for ans in all_final_answers)
        features_for_next = {"num_answers": len(all_final_answers), "total_answer_length": total_answer_length}
        aggregated_image_features = ray.get(context.get.remote("aggregated_vision_features"))

        context.put.remote("final_answers", all_final_answers)
        
        print(f"✅ Task {task_id}: Merging complete. Total items: {len(all_final_answers)}.")
        return json.dumps({
            "dag_id": dag_id, "status": "success",
            "curr_task_feat": aggregated_image_features, # task4aligned
            "succ_task_feat": {"task5_output_final_answer": features_for_next},
            "start_time": start_time, "end_time": time.time()
        })
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id"))
        print(f"❌ {task_id} failed: {str(e)}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task {task_id} Error: {e}"
                            , "start_time": time.time(), "end_time": time.time()
                           })

@io(mem= 1024)
def task5_output_final_answer(context):
    """Task 5: [aligned] emitfinal answer"""
    try:
        print("✅ Task 5: Formatting final output.")
        start_time = time.time()
        dag_id = ray.get(context.get.remote("dag_id"))
        final_answers = ray.get(context.get.remote("final_answers"))
        
        #

        final_answer_text = "\n\n".join([f"Answer for {d['file_name']}:\n{d['answer']}" for d in final_answers])
        
        print(f"🏁 Final Answer for DAG {dag_id}:\n{final_answer_text[:500]}...")
        return json.dumps({
            "dag_id": dag_id, "status": "success", "final_answer": final_answer_text,
            "curr_task_feat": {"final_answer_length": len(final_answer_text), "num_answers": len(final_answers)},
            "start_time": start_time, "end_time": time.time()
        })
    except Exception as e:
        dag_id = ray.get(context.get.remote("dag_id"))
        print(f"❌ task5_output_final_answer failed: {str(e)}")
        return json.dumps({"dag_id": dag_id, "status": "failed", "result": f"Task 5 Error: {e}", "start_time": time.time(), "end_time": time.time()})
