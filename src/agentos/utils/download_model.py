import os
import easyocr
import time
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"  #

os.environ["HF_HUB_OFFLINE"] = "0"  #

from pathlib import Path
from typing import List
import requests #

from PIL import Image #

from io import BytesIO #

import torch
from transformers import pipeline
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
from datasets import load_dataset
import transformers.utils.hub as hub
hub.HF_ENDPOINT = "https://hf-mirror.com"
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    AutoProcessor,
    AutoModelForImageTextToText,
    WhisperProcessor,
    WhisperForConditionalGeneration,
    BlipProcessor,
    BlipForConditionalGeneration
)

# gpu
# os.environ["CUDA_VISIBLE_DEVICES"] = "1,2,3"
def test_sentiment_analysis_inference(model_path: Path) -> bool:
    """model"""
    try:
        #

        sentiment_analyzer = pipeline(
            "sentiment-analysis", 
            model=model_path, 
            device=0 if torch.cuda.is_available() else -1
        )
        
        #  ()
        test_texts = [
            "This is absolutely wonderful!",  #

            "Je suis très content avec ce produit.",  #

            "Ich bin sehr enttäuscht von dieser Erfahrung.",  #

            "Estoy bastante satisfecho con el servicio.",  #

            "",  #

        ]
        
        #

        results = sentiment_analyzer(test_texts)
        
        #

        for text, result in zip(test_texts, results):
            print(f"📝 : {text}")
            print(f"  : {result['label']}, : {result['score']:.4f}")
            print("-" * 50)
        
        #

        del sentiment_analyzer
        torch.cuda.empty_cache()
        return True
    except Exception as e:
        print(f"❌ model: {e}")
        return False

def test_blip_inference(model_path: Path) -> bool:
    """ BLIP model"""
    try:
        #

        processor = BlipProcessor.from_pretrained(model_path)
        #  device_map="auto" 
        model = BlipForConditionalGeneration.from_pretrained(
            model_path,
            torch_dtype=torch.float16,
            device_map="auto"
        )
        
        #  ()
        url = "http://images.cocodataset.org/val2017/000000039769.jpg"
        response = requests.get(url)
        raw_image = Image.open(BytesIO(response.content)).convert('RGB')

        #

        #

        inputs = processor(raw_image, return_tensors="pt").to(model.device, torch.float16)

        #

        out = model.generate(**inputs, max_new_tokens=75)
        caption = processor.decode(out[0], skip_special_tokens=True)
        
        print(f"🖼️ BLIP : {caption}")
        #

        del model, processor, inputs, out
        torch.cuda.empty_cache()
        return True
    except Exception as e:
        print(f"❌ BLIP : {e}")
        return False

def test_deepseek_inference(model_path: Path) -> bool:
    """DeepSeekmodel"""
    try:
        #  AutoTokenizer  AutoModelForCausalLM 
        tokenizer = AutoTokenizer.from_pretrained(model_path, device_map= "auto")
        model = AutoModelForCausalLM.from_pretrained(
            model_path,
            torch_dtype=torch.bfloat16,  # bfloat16 GPU
            device_map="auto",
        )
        #

        messages = [
            {"role": "user", "content": ""}
        ]
        #  apply_chat_template 
        text = tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True
        )
        inputs = tokenizer(text, return_tensors="pt").to(model.device)
        #

        outputs = model.generate(
            **inputs,
            max_new_tokens= 16384,
            do_sample= True,
            top_p= 0.9,
            temperature= 0.6,
            repetition_penalty=1.1
        )
        #

        response = tokenizer.decode(outputs[0][inputs.input_ids.shape[1]:], skip_special_tokens=True)
        print(f"🤖 DeepSeek : {response}")
        return True
    except Exception as e:
        print(f"❌ DeepSeek : {e}")
        return False

def test_whisper_inference(model_path):
    try:
        processor = WhisperProcessor.from_pretrained(model_path, device_map= "auto")
        model = WhisperForConditionalGeneration.from_pretrained(model_path, device_map= "auto")
        model.generation_config.forced_decoder_ids = None

        ds = load_dataset("hf-internal-testing/librispeech_asr_dummy", "clean", split="validation")
        sample = ds[0]["audio"]

        input_features = processor(
            sample["array"],
            sampling_rate=sample["sampling_rate"],
            return_tensors="pt"
        ).input_features
        input_features = input_features.to("cuda")
        predicted_ids = model.generate(input_features)
        transcription = processor.batch_decode(predicted_ids, skip_special_tokens=True)[0]
        print("🔊 Whisper ", transcription)
        return True
    except Exception as e:
        print("❌ Whisper ", e)
        return False

def test_qwen3_inference(model_path, cache_dir= "/mnt/7T/xz/"):
    try:
        tokenizer = AutoTokenizer.from_pretrained(model_path, device_map= "auto")
        model = AutoModelForCausalLM.from_pretrained(
            model_path,
            torch_dtype=torch.float16,
            low_cpu_mem_usage=True,
            cache_dir= cache_dir,
            device_map= "auto"
        )

        messages = [
            {"role": "user", "content": ""},
        ]
        conversation = ""
        for m in messages:
            conversation += f"{m['role']}: {m['content']}\n"

        input_ids = tokenizer.encode(conversation + tokenizer.eos_token, return_tensors="pt").to("cuda")
        output = model.generate(input_ids, pad_token_id= tokenizer.eos_token_id)
        response = tokenizer.decode(output[:, input_ids.shape[-1]:][0], skip_special_tokens=True)
        print("🧠 Qwen3 ", response)

        del model
        del tokenizer
        return True
    except Exception as e:
        print("❌ Qwen3 ", e)
        return False

def test_qwen_vl_inference(model_path):
    try:
        from qwen_vl_utils import process_vision_info
        from transformers import Qwen2_5_VLForConditionalGeneration  #


        processor = AutoProcessor.from_pretrained(model_path, device_map= "auto")
        model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            model_path,
            torch_dtype=torch.float16,
            low_cpu_mem_usage=True,
            device_map="auto"
        )

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "image": "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen-VL/assets/demo.jpeg",
                    },
                    {"type": "text", "text": ""},
                ],
            }
        ]

        text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        image_inputs, video_inputs = process_vision_info(messages)
        inputs = processor(
            text=[text],
            images=image_inputs,
            videos=video_inputs,
            padding=True,
            return_tensors="pt"
        ).to("cuda")

        output_ids = model.generate(**inputs, max_new_tokens=64)
        response = processor.batch_decode(output_ids, skip_special_tokens=True)[0]
        print("🖼️ Qwen-VL ", response)
        return True
    except Exception as e:
        print("❌ Qwen-VL ", e)
        return False

def test_t5_inference(model_path: Path) -> bool:
    """ T5 model"""
    try:
        #

        tokenizer = AutoTokenizer.from_pretrained(model_path)
        model = AutoModelForSeq2SeqLM.from_pretrained(
            model_path,
            torch_dtype=torch.float16,
            device_map="auto"
        )
        
        #

        text = """The Tower of London is a historic castle on the north bank of 
                the River Thames in central London. It was founded towards the 
                end of 1066 as part of the Norman Conquest. The Tower has served 
                variously as an armory, a treasury, a menagerie, the home of the 
                Royal Mint, a public records office, and the home of the Crown 
                Jewels of England."""
        
        #

        inputs = tokenizer(
            "summarize: " + text,  # T5 
            return_tensors="pt",
            max_length=512,
            truncation=True
        ).to(model.device)
        
        #

        outputs = model.generate(
            **inputs,
            max_length=150,
            min_length=40,
            num_beams=4,
            early_stopping=True
        )
        
        #

        summary = tokenizer.decode(outputs[0], skip_special_tokens=True)
        print(f"📝 T5 : {summary}")
        
        #

        del model, tokenizer, inputs, outputs
        torch.cuda.empty_cache()
        return True
    except Exception as e:
        print(f"❌ T5 : {e}")
        return False

def download_model(model_name: str, local_model_folder: str= "./"):
    if model_name == "Qwen/Qwen3-32B":
        local_path = os.path.join(local_model_folder, "Qwen/Qwen3-32B")
        if Path(local_path).exists() and test_qwen3_inference(local_path):
            print(f"✅  Qwen3 model{local_path}")
            return

        tokenizer = AutoTokenizer.from_pretrained(model_name, device_map= "auto")
        model = AutoModelForCausalLM.from_pretrained(
            model_name, torch_dtype=torch.float16, low_cpu_mem_usage=True, device_map= "auto"
        )
        tokenizer.save_pretrained(local_path)
        model.save_pretrained(local_path)
        print(f"✅ Qwen3 model{local_path}")
        test_qwen3_inference(local_path)
    
    elif model_name == "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B":
        local_path = os.path.join(local_model_folder, "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B")
        if Path(local_path).exists() and test_deepseek_inference(local_path):
            print(f"✅  DeepSeek r1 model{local_path}")
            return

        tokenizer = AutoTokenizer.from_pretrained(model_name, device_map= "auto")
        model = AutoModelForCausalLM.from_pretrained(
            model_name, torch_dtype=torch.float16, low_cpu_mem_usage=True, device_map= "auto"
        )
        tokenizer.save_pretrained(local_path)
        model.save_pretrained(local_path)
        print(f"✅ DeepSeek r1 model{local_path}")
        test_deepseek_inference(local_path)

    elif model_name == "Qwen/Qwen2.5-VL-32B-Instruct":
        local_path = os.path.join(local_model_folder, "Qwen/Qwen2.5-VL-32B-Instruct")
        if Path(local_path).exists() and test_qwen_vl_inference(local_path):
            print(f"✅  Qwen-VL model{local_path}")
            return
        processor = AutoProcessor.from_pretrained(model_name, device_map= "auto")
        model = AutoModelForImageTextToText.from_pretrained(model_name, device_map= "auto")
        processor.save_pretrained(local_path)
        model.save_pretrained(local_path)
        processor.save_pretrained(local_path)
        print(f"✅ Qwen-VL model{local_path}")
        test_qwen_vl_inference(local_path)

    elif model_name == "openai/whisper-medium":
        local_path = os.path.join(local_model_folder, "whisper_models/whisper-medium")        
        if Path(local_path).exists() and test_whisper_inference(local_path):
            print(f"✅  Whisper model{local_path}")
            return

        processor = WhisperProcessor.from_pretrained(model_name, device_map= "auto")
        model = WhisperForConditionalGeneration.from_pretrained(model_name, device_map= "auto")
        processor.save_pretrained(local_path)
        model.save_pretrained(local_path)
        print(f"✅ Whisper model{local_path}")
        test_whisper_inference(local_path)

    elif model_name == "Salesforce/blip-image-captioning-large":
        #

        local_path = os.path.join(local_model_folder, "blip-image-captioning-large")
        if Path(local_path).exists() and test_blip_inference(local_path):
            print(f"✅  BLIP model{local_path}")
            return

        print(f"📥  BLIP model: {model_name}...")
        processor = BlipProcessor.from_pretrained(
            model_name,
            force_download=True,  #

            resume_download=False,  #

            local_files_only=False,  #

            use_fast=False,  #  fast tokenizer
        )
        model = BlipForConditionalGeneration.from_pretrained(
            model_name,
            torch_dtype=torch.float16,
            device_map="auto" #

        )
        
        #

        Path(local_path).mkdir(parents=True, exist_ok=True)
        processor.save_pretrained(local_path)
        model.save_pretrained(local_path)
        
        print(f"✅ BLIP model{local_path}")
        #

        test_blip_inference(local_path)

    elif model_name== "t5-base":
        model_name = "t5-base"
        languages= ["en"]
        local_path = os.path.join(local_model_folder, "t5-base")
        
        #

        if Path(local_path).exists() and test_t5_inference(local_path):
            print(f"✅  T5-base model: {local_path}")
            return
        
        print(f"📥  T5-base model...")
        
        #

        tokenizer = AutoTokenizer.from_pretrained(model_name)
        model = AutoModelForSeq2SeqLM.from_pretrained(
            model_name,
            torch_dtype=torch.float16,
            device_map="auto"
        )
        
        #

        Path(local_path).mkdir(parents=True, exist_ok=True)
        tokenizer.save_pretrained(local_path)
        model.save_pretrained(local_path)
        
        print(f"✅ T5-base model: {local_path}")
        #

        test_t5_inference(local_path)
    elif model_name== "easyocr":
        model_name = "easyocr"
        languages= ["en"]
        local_path = os.path.join(local_model_folder, "easyocr")
        #

        if Path(local_path).exists() and test_easyocr_inference(local_path):
            print(f"✅  easyocr model: {local_path}")
            return
        
        print(f"📥  easyocr model...")

        #

        if Path(local_path).exists():
            print("🔄 modelfile...")
            if test_easyocr_inference(local_path):
                print(f"✅ modelvia")
                return 

        #

        print("⏳ modelfile...")
        start_time = time.time()
        
        #

        reader = easyocr.Reader(
            lang_list= languages,
            gpu=True,
            download_enabled=True,
            model_storage_directory=local_path,
            detector=True,
            recognizer=True
        )
        
        download_time = time.time() - start_time
        print(f"✅ model {download_time:.2f}s")
        
        #

        print("\n🧪 model...")
        test_result = test_easyocr_inference(local_path, languages)
        #

        del reader
        torch.cuda.empty_cache()        
        if not test_result:
            raise RuntimeError("model")
    elif model_name== "nlptown/bert-base-multilingual-uncased-sentiment":
        local_path = os.path.join(local_model_folder, model_name)
        #

        if Path(local_path).exists() and test_sentiment_analysis_inference(local_path):
            print(f"✅  {model_name} model: {local_path}")
            return
        
        print(f"📥  easyocr model...")
        sentiment_analyzer = pipeline(
            "sentiment-analysis", 
            model=model_name,
            device="cuda"
        )
        
        #

        Path(local_path).mkdir(parents=True, exist_ok=True)
        sentiment_analyzer.model.save_pretrained(local_path)
        sentiment_analyzer.tokenizer.save_pretrained(local_path)
        
        print(f"✅ model: {local_path}")

        #

        if Path(local_path).exists():
            print("🔄 modelfile...")
            if test_sentiment_analysis_inference(local_path):
                print(f"✅ modelvia")
                return 
    else:
        print(f"❌ model{model_name}")


if __name__ == "__main__":
    #

    # download_model("Qwen/Qwen3-32B")
    # download_model("Qwen/Qwen2.5-VL-32B-Instruct")
    # download_model("deepseek-ai/DeepSeek-R1-Distill-Qwen-32B")
    # download_model("openai/whisper-medium")
    # download_model("Salesforce/blip-image-captioning-large")
    # download_model("t5-base")
    # download_model("easyocr")
    download_model("nlptown/bert-base-multilingual-uncased-sentiment")