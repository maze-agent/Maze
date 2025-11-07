"""
多模态创意内容生成器
===================

功能：从一个文本描述，并行生成文本故事、图像和音频三种模态的内容

工作流结构：
    输入描述 (Task A)
         ↓
    [Task B: 文本生成 (CPU, 4GB)]
    [Task C: 图像生成 (GPU, 8GB)]  ← 并行执行
    [Task D: 音频生成 (CPU, 4GB)]
         ↓
    Task E: 汇总展示 (CPU, 512MB)

使用模型：
- 文本生成: gpt2-large (774M)
- 图像生成: stable-diffusion-v1-5 (4GB VRAM)
- 音频生成: bark (text-to-speech)

运行前准备：
1. 确保 Maze 服务器已启动
2. 安装依赖: pip install transformers diffusers torch accelerate scipy bark
3. 首次运行会下载模型，需要一些时间

运行方式：
python example/multimodal_content_generator.py
"""

from maze import MaClient, task
import os

# 输出目录配置（使用绝对路径）
OUTPUT_DIR = r"E:\PythonProject\maze\Maze\examples\multimodel_create_workflow\outputs"
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ============================================================================
# Task A: 文本预处理和提示词增强
# ============================================================================
@task(
    inputs=["user_description"],
    outputs=["enhanced_text_prompt", "enhanced_image_prompt", "audio_text"],
    resources={"cpu": 1, "cpu_mem": 512, "gpu": 0, "gpu_mem": 0}
)
def preprocess_and_enhance(params):
    """
    预处理用户输入，为不同模态生成优化的提示词
    
    输入: 用户的简短描述
    输出: 
        - enhanced_text_prompt: 用于文本生成的详细提示
        - enhanced_image_prompt: 用于图像生成的优化提示
        - audio_text: 用于语音合成的文本
    """
    description = params.get("user_description")
    
    print(f"[Task A] Processing user input: {description}")
    
    # Enhance prompts for text generation
    text_prompt = f"Write a creative short story (3-4 paragraphs) about: {description}. Make it engaging and vivid."
    
    # Optimize prompts for image generation (Stable Diffusion friendly)
    image_prompt = f"{description}, highly detailed, digital art, trending on artstation, vibrant colors, 8k uhd"
    
    # Prepare concise text for audio synthesis
    audio_text = f"This is a story about {description}."
    
    print(f"[Task A] ✓ Prompt enhancement completed")
    print(f"  - Text prompt: {text_prompt[:50]}...")
    print(f"  - Image prompt: {image_prompt[:50]}...")
    print(f"  - Audio text: {audio_text}")
    
    return {
        "enhanced_text_prompt": text_prompt,
        "enhanced_image_prompt": image_prompt,
        "audio_text": audio_text
    }


# ============================================================================
# Task B: 文本故事生成 (CPU)
# ============================================================================
@task(
    inputs=["text_prompt"],
    outputs=["generated_story", "story_file_path"],
    resources={"cpu": 4, "cpu_mem": 4096, "gpu": 0, "gpu_mem": 0}
)
def generate_story(params):
    """
    使用 GPT-2 Large 生成创意故事
    
    模型: gpt2-large (774M parameters)
    资源: CPU only, 4 cores, 4GB RAM
    """
    prompt = params.get("text_prompt")
    
    print(f"[Task B] Starting text story generation...")
    print(f"[Task B] Loading GPT-2 Large model...")
    
    from transformers import GPT2LMHeadModel, GPT2Tokenizer
    import torch
    
    # Load model
    model_name = "gpt2-large"
    tokenizer = GPT2Tokenizer.from_pretrained(model_name)
    model = GPT2LMHeadModel.from_pretrained(model_name)
    model.eval()
    
    print(f"[Task B] Model loaded, starting generation...")
    
    # 编码输入
    inputs = tokenizer.encode(prompt, return_tensors="pt")
    
    # 生成文本
    with torch.no_grad():
        outputs = model.generate(
            inputs,
            max_length=300,
            num_return_sequences=1,
            temperature=0.8,
            top_p=0.9,
            do_sample=True,
            pad_token_id=tokenizer.eos_token_id
        )
    
    # 解码输出
    story = tokenizer.decode(outputs[0], skip_special_tokens=True)
    
    # 保存到文件
    story_file = os.path.join(OUTPUT_DIR, "generated_story.txt")
    with open(story_file, "w", encoding="utf-8") as f:
        f.write(story)
    
    print(f"[Task B] ✓ Story generation completed!")
    print(f"[Task B]   Story length: {len(story)} characters")
    print(f"[Task B]   Saved to: {story_file}")
    print(f"[Task B]   Preview: {story[:150]}...")
    
    return {
        "generated_story": story,
        "story_file_path": story_file
    }


# ============================================================================
# Task C: 图像生成 (GPU)
# ============================================================================
@task(
    inputs=["image_prompt"],
    outputs=["image_file_path", "image_info"],
    resources={"cpu": 2, "cpu_mem": 2048, "gpu": 1, "gpu_mem": 8192}
)
def generate_image(params):
    """
    使用 Stable Diffusion 生成图像
    
    模型: stable-diffusion-v1-5
    资源: 1 GPU (4090), 8GB VRAM
    """
    prompt = params.get("image_prompt")
    
    print(f"[Task C] Starting image generation...")
    print(f"[Task C] Prompt: {prompt}")
    print(f"[Task C] Loading Stable Diffusion model...")
    
    from diffusers import StableDiffusionPipeline
    import torch
    
    # 加载模型到 GPU
    model_id = "runwayml/stable-diffusion-v1-5"
    pipe = StableDiffusionPipeline.from_pretrained(
        model_id,
        torch_dtype=torch.float16,
        safety_checker=None  # 禁用安全检查器以节省内存
    )
    pipe = pipe.to("cuda")
    
    print(f"[Task C] Model loaded, starting image generation...")
    
    # 生成图像
    with torch.no_grad():
        image = pipe(
            prompt,
            num_inference_steps=50,
            guidance_scale=7.5,
            height=512,
            width=512
        ).images[0]
    
    # 保存图像
    image_file = os.path.join(OUTPUT_DIR, "generated_image.png")
    image.save(image_file)
    
    # 获取图像信息
    image_info = f"512x512, Stable Diffusion v1.5, 50 steps"
    
    print(f"[Task C] ✓ Image generation completed!")
    print(f"[Task C]   Size: 512x512")
    print(f"[Task C]   Saved to: {image_file}")
    
    # 清理 GPU 内存
    del pipe
    torch.cuda.empty_cache()
    
    return {
        "image_file_path": image_file,
        "image_info": image_info
    }


# ============================================================================
# Task D: 音频生成 (CPU)
# ============================================================================
@task(
    inputs=["audio_text"],
    outputs=["audio_file_path", "audio_duration"],
    resources={"cpu": 4, "cpu_mem": 4096, "gpu": 0, "gpu_mem": 0}
)
def generate_audio(params):
    """
    使用 Bark 将文本转换为语音
    
    模型: suno/bark-small
    资源: CPU only, 4 cores, 4GB RAM
    """
    text = params.get("audio_text")
    
    print(f"[Task D] Starting audio generation...")
    print(f"[Task D] Text: {text}")
    print(f"[Task D] Loading Bark TTS model...")
    
    from transformers import AutoProcessor, BarkModel
    import scipy.io.wavfile as wavfile
    import torch
    
    # 加载模型
    processor = AutoProcessor.from_pretrained("suno/bark-small")
    model = BarkModel.from_pretrained("suno/bark-small")
    model.eval()
    
    print(f"[Task D] Model loaded, starting speech synthesis...")
    
    # 处理输入
    inputs = processor(text, voice_preset="v2/en_speaker_6")
    
    # 生成音频
    with torch.no_grad():
        audio_array = model.generate(**inputs)
    
    # 转换为 numpy 数组
    audio_array = audio_array.cpu().numpy().squeeze()
    
    # 保存音频文件
    sample_rate = model.generation_config.sample_rate
    audio_file = os.path.join(OUTPUT_DIR, "generated_audio.wav")
    wavfile.write(audio_file, rate=sample_rate, data=audio_array)
    
    # 计算时长
    duration = len(audio_array) / sample_rate
    
    print(f"[Task D] ✓ Audio generation completed!")
    print(f"[Task D]   Duration: {duration:.2f} seconds")
    print(f"[Task D]   Sample rate: {sample_rate} Hz")
    print(f"[Task D]   Saved to: {audio_file}")
    
    return {
        "audio_file_path": audio_file,
        "audio_duration": f"{duration:.2f}"
    }


# ============================================================================
# Task E: 汇总和展示
# ============================================================================
@task(
    inputs=["story_file", "image_file", "audio_file", "story_text", "image_info", "audio_duration"],
    outputs=["summary_html_path", "summary_text"],
    resources={"cpu": 1, "cpu_mem": 512, "gpu": 0, "gpu_mem": 0}
)
def summarize_results(params):
    """
    汇总所有生成的内容，创建展示页面
    """
    story_file = params.get("story_file")
    image_file = params.get("image_file")
    audio_file = params.get("audio_file")
    story_text = params.get("story_text")
    image_info = params.get("image_info")
    audio_duration = params.get("audio_duration")
    
    print(f"[Task E] Aggregating all generated content...")
    
    # 创建 HTML 展示页面
    html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>多模态创意内容生成结果</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f5f5;
        }}
        .container {{
            background: white;
            border-radius: 10px;
            padding: 30px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        h1 {{
            color: #333;
            border-bottom: 3px solid #4CAF50;
            padding-bottom: 10px;
        }}
        .section {{
            margin: 30px 0;
            padding: 20px;
            background: #fafafa;
            border-radius: 8px;
        }}
        .section h2 {{
            color: #4CAF50;
            margin-top: 0;
        }}
        .story {{
            line-height: 1.8;
            color: #333;
        }}
        .image-container {{
            text-align: center;
            margin: 20px 0;
        }}
        .image-container img {{
            max-width: 100%;
            border-radius: 8px;
            box-shadow: 0 4px 8px rgba(0,0,0,0.2);
        }}
        .info {{
            color: #666;
            font-size: 0.9em;
            margin-top: 10px;
        }}
        .badge {{
            display: inline-block;
            background: #4CAF50;
            color: white;
            padding: 5px 10px;
            border-radius: 5px;
            font-size: 0.85em;
            margin-right: 10px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🎨 多模态创意内容生成结果</h1>
        <p style="color: #666;">通过 Maze 分布式工作流并行生成</p>
        
        <div class="section">
            <h2>📝 生成的故事</h2>
            <div class="story">
                {story_text[:500]}...
            </div>
            <div class="info">
                <span class="badge">CPU</span>
                模型: GPT-2 Large | 字符数: {len(story_text)}
            </div>
        </div>
        
        <div class="section">
            <h2>🖼️ 生成的图像</h2>
            <div class="image-container">
                <img src="{os.path.basename(image_file)}" alt="Generated Image">
            </div>
            <div class="info">
                <span class="badge">GPU</span>
                模型: Stable Diffusion v1.5 | {image_info}
            </div>
        </div>
        
        <div class="section">
            <h2>🔊 生成的音频</h2>
            <audio controls style="width: 100%;">
                <source src="{os.path.basename(audio_file)}" type="audio/wav">
                您的浏览器不支持音频播放
            </audio>
            <div class="info">
                <span class="badge">CPU</span>
                模型: Bark TTS | 时长: {audio_duration} 秒
            </div>
        </div>
        
        <div style="margin-top: 40px; padding: 20px; background: #e8f5e9; border-radius: 8px;">
            <h3 style="color: #2e7d32; margin-top: 0;">⚡ 性能亮点</h3>
            <ul style="color: #333;">
                <li><strong>并行执行:</strong> 文本、图像、音频生成同时进行</li>
                <li><strong>资源异构:</strong> CPU 任务和 GPU 任务智能调度</li>
                <li><strong>自动管理:</strong> 依赖关系自动处理，结果自动汇总</li>
            </ul>
        </div>
    </div>
</body>
</html>
"""
    
    # 保存 HTML
    html_file = os.path.join(OUTPUT_DIR, "result.html")
    with open(html_file, "w", encoding="utf-8") as f:
        f.write(html_content)
    
    # 创建文本摘要
    summary = f"""
{'='*70}
多模态创意内容生成完成！
{'='*70}

📁 输出文件:
  - 故事文本: {story_file}
  - 生成图像: {image_file}
  - 合成音频: {audio_file}
  - 汇总页面: {html_file}

📊 统计信息:
  - 故事长度: {len(story_text)} 字符
  - 图像信息: {image_info}
  - 音频时长: {audio_duration} 秒

🌐 查看结果:
  在浏览器中打开: file:///{html_file}

✨ 工作流特点:
  - 3 个任务并行执行 (B, C, D)
  - CPU 和 GPU 资源异构调度
  - 自动依赖管理和结果汇总
{'='*70}
"""
    
    print(f"[Task E] ✓ Aggregation completed!")
    print(summary)
    
    return {
        "summary_html_path": html_file,
        "summary_text": summary
    }


# ============================================================================
# 主程序：编排工作流
# ============================================================================
def main():
    print("=" * 70)
    print("🎨 Multimodal Creative Content Generator")
    print("=" * 70)
    print()
    
    # User input
    user_input = input("Enter your creative theme (e.g., a magical forest at sunset): ").strip()
    if not user_input:
        user_input = "a magical forest at sunset with glowing fireflies"
        print(f"Using default theme: {user_input}")
    
    print()
    print("🚀 Starting workflow creation...")
    print()
    
    # 1. 创建客户端
    client = MaClient("http://localhost:8000")
    
    # 2. Create workflow
    workflow = client.create_workflow()
    print(f"✓ Workflow created: {workflow.workflow_id}")
    
    # 3. Add Task A: Preprocessing
    print("✓ Adding Task A: Text preprocessing and prompt enhancement")
    task_a = workflow.add_task(
        preprocess_and_enhance,
        inputs={"user_description": user_input},
        task_name="Preprocessing and Prompt Enhancement"
    )
    
    # 4. Add parallel tasks B, C, D
    print("✓ Adding Task B: Text story generation (CPU)")
    task_b = workflow.add_task(
        generate_story,
        inputs={"text_prompt": task_a.outputs["enhanced_text_prompt"]},
        task_name="Text Story Generation"
    )
    
    print("✓ Adding Task C: Image generation (GPU)")
    task_c = workflow.add_task(
        generate_image,
        inputs={"image_prompt": task_a.outputs["enhanced_image_prompt"]},
        task_name="Image Generation"
    )
    
    print("✓ Adding Task D: Audio generation (CPU)")
    task_d = workflow.add_task(
        generate_audio,
        inputs={"audio_text": task_a.outputs["audio_text"]},
        task_name="Audio Generation"
    )
    
    # 5. Add Task E: Aggregation
    print("✓ Adding Task E: Result aggregation")
    task_e = workflow.add_task(
        summarize_results,
        inputs={
            "story_file": task_b.outputs["story_file_path"],
            "image_file": task_c.outputs["image_file_path"],
            "audio_file": task_d.outputs["audio_file_path"],
            "story_text": task_b.outputs["generated_story"],
            "image_info": task_c.outputs["image_info"],
            "audio_duration": task_d.outputs["audio_duration"]
        },
        task_name="Result Aggregation"
    )
    
    print()
    print("📊 Workflow structure:")
    print("    Task A (Preprocessing)")
    print("       ↓")
    print("    ┌──┴──┬──────┐")
    print("    ↓     ↓      ↓")
    print("  Task B Task C Task D  ← Parallel execution")
    print("  (CPU)  (GPU)  (CPU)")
    print("    └──┬──┴──────┘")
    print("       ↓")
    print("    Task E (Aggregation)")
    print()
    
    # 6. Run workflow
    print("🚀 Starting workflow execution...")
    print("=" * 70)
    print()
    
    workflow.run()
    
    # 7. Get results with automatic progress display
    result = workflow.show_results(output_dir=OUTPUT_DIR)
    
    print()
    print("📁 All files saved to:", OUTPUT_DIR)
    print("🌐 Open result.html to view complete results")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  User interrupted execution")
    except Exception as e:
        print(f"\n\n❌ Execution error: {e}")
        import traceback
        traceback.print_exc()


