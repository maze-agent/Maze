# Multimodal Content Generator Workflow

## 🎯 Overall Business Scenario

This is a **multimodal creative content generation system** that demonstrates the power of parallel processing and heterogeneous resource scheduling in distributed workflows.

**Actual Business Process:**
Imagine you are a content creator who needs to quickly generate multiple types of creative materials. Given a single text description, the system automatically generates:
- 📝 A creative story (Text)
- 🖼️ A matching image (Visual)
- 🔊 Voice narration (Audio)

All three content types are generated in **parallel**, leveraging both CPU and GPU resources efficiently.

## 📊 Workflow Execution Flow

```
1. User Input Description
   ↓
2. [Text Preprocessing] Enhance prompts for different modalities
   ↓
3. Parallel Content Generation (simultaneously to maximize efficiency)
   ├─ [Text Generation] GPT-2 Large generates creative story (CPU)
   ├─ [Image Generation] Stable Diffusion creates artwork (GPU)
   └─ [Audio Generation] Bark synthesizes voice narration (CPU)
   ↓
4. [Result Aggregation] Combine all outputs into HTML showcase
```

## 🔍 Detailed Function Description

### 1. preprocess_and_enhance - Preprocessing Node

**Business Purpose:** Acts as an intelligent prompt engineer, optimizing the user's input for each content generation model.

**Input:**
```
"a magical forest at sunset"
```

**What It Does:**
- Creates a detailed prompt for story generation
- Optimizes description for image generation (adding quality keywords)
- Prepares concise text for audio synthesis

**Output:**
```python
{
    "enhanced_text_prompt": "Write a creative short story about: a magical forest...",
    "enhanced_image_prompt": "a magical forest at sunset, highly detailed, digital art...",
    "audio_text": "This is a story about a magical forest at sunset."
}
```

**Why Preprocessing?**
Different AI models have different "languages". Text models need narrative prompts, while image models work better with quality descriptors like "8k uhd" or "trending on artstation".

---

### 2. generate_story - Text Story Generation (CPU Task)

**Business Purpose:** Creates an engaging narrative story based on the user's theme.

**Model Used:** GPT-2 Large (774M parameters)

**What It Does:**
1. **Load GPT-2 Large Model**
   - 774M parameter language model
   - Optimized for creative text generation

2. **Generate Creative Content**
   - Temperature: 0.8 (creative randomness)
   - Max length: 300 tokens
   - Outputs coherent narrative text

3. **Save Results**
   - Saves story to `generated_story.txt`
   - Returns story content and file path

**Resource Requirements:**
- CPU: 4 cores
- Memory: 4GB RAM
- No GPU required

**Actual Business Meaning:**
```
Output Example:
"Once upon a time in a magical forest, where ancient trees 
whispered secrets to the wind, a young wanderer discovered 
a hidden path illuminated by golden sunset rays..."
```

This provides the textual foundation for the creative content, which can be used for articles, scripts, or storytelling.

---

### 3. generate_image - Image Generation (GPU Task)

**Business Purpose:** Creates high-quality artwork matching the theme.

**Model Used:** Stable Diffusion v1.5

**What It Does:**
1. **Load Stable Diffusion Pipeline**
   - State-of-the-art text-to-image model
   - FP16 precision for memory efficiency
   - Optimized for NVIDIA GPUs

2. **Generate High-Quality Image**
   - 50 inference steps for quality
   - 512x512 resolution
   - Guidance scale: 7.5 (prompt adherence)

3. **Save and Clean Up**
   - Saves as `generated_image.png`
   - Clears GPU memory after generation

**Resource Requirements:**
- GPU: 1x (RTX 4090 or similar)
- VRAM: 8GB
- CPU: 2 cores
- Memory: 2GB RAM

**Actual Business Meaning:**
```
Output: High-quality digital artwork
- Resolution: 512x512
- Style: Professional digital art
- Suitable for: Social media, presentations, marketing
```

This demonstrates **heterogeneous resource scheduling**: while the CPU handles text generation, the GPU dedicates full resources to image generation.

---

### 4. generate_audio - Audio Synthesis (CPU Task)

**Business Purpose:** Converts text into natural-sounding voice narration.

**Model Used:** Bark TTS (Text-to-Speech)

**What It Does:**
1. **Load Bark Model**
   - Neural text-to-speech synthesis
   - Natural, human-like voice quality
   - Supports multiple speaking styles

2. **Synthesize Audio**
   - Converts text to speech waveform
   - Sample rate: 24kHz
   - Natural prosody and intonation

3. **Export Audio File**
   - Saves as `generated_audio.wav`
   - Calculates and reports duration

**Resource Requirements:**
- CPU: 4 cores
- Memory: 4GB RAM
- No GPU required

**Actual Business Meaning:**
```
Output: Natural voice narration
- Duration: ~5-10 seconds
- Quality: Natural human-like voice
- Use cases: Podcasts, audiobooks, accessibility
```

This completes the multimodal generation by adding an auditory dimension to the content.

---

### 5. summarize_results - Result Aggregation Node

**Business Purpose:** Combines all generated content into a unified, beautiful presentation.

**What It Does:**
- Collects all generated files (story, image, audio)
- Creates an interactive HTML showcase
- Generates metadata and statistics
- Provides easy-to-share results page

**Output:**
```
HTML Report including:
- Complete story text with formatting
- Embedded high-quality image
- Playable audio controls
- Model information and generation stats
- Performance metrics (parallel execution time)
```

**Why This Matters:**
In real business scenarios, this aggregated output can be:
- Shared with clients immediately
- Used in presentations or demos
- Published directly to web platforms
- Archived for portfolio review

---

## 🎓 Workflow Summary

This workflow demonstrates a **complete multimodal content creation pipeline**:

1. **Intelligent Preprocessing** → Optimize prompts for each model
2. **Parallel Generation** (3 tasks) → Maximize efficiency through concurrency
3. **Heterogeneous Resources** → CPU and GPU tasks scheduled independently
4. **Unified Output** → Professional presentation ready for use

In real business applications, such a system can:
- **Save Time**: Parallel execution reduces total time from 5+ minutes to ~2 minutes
- **Optimize Resources**: CPU and GPU work simultaneously, not sequentially
- **Scale Easily**: Add more workers to handle multiple requests concurrently
- **Professional Output**: Generate production-ready content automatically

## ⚡ Key Features

### 1. Parallel Execution
- Tasks B, C, D run simultaneously
- Total time = max(B_time, C_time, D_time), not B + C + D
- **Real Performance**: ~2x faster than sequential execution

### 2. Heterogeneous Resource Scheduling
- **CPU Tasks**: Text generation, audio synthesis
- **GPU Task**: Image generation (requires CUDA)
- Automatic resource allocation and management

### 3. Automatic Dependency Management
- Task A completes before B/C/D start
- Tasks B/C/D outputs feed into Task E
- No manual coordination needed

## 📋 Execution Process

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Start Maze

Start Maze Head (if you have multiple machines, you can start Maze workers):

```bash
maze start --head
```

### 3. Run the Workflow

```bash
python main.py
```

### 4. Input Your Theme

```
Enter your creative theme: a futuristic city at night
```

### 5. View Results

The workflow will generate:
```
outputs/
├── generated_story.txt          # Creative story
├── generated_image.png          # AI-generated artwork
├── generated_audio.wav          # Voice narration
└── result.html                  # ⭐ Complete showcase (open this!)
```

Open `result.html` in your browser to see all generated content in a beautiful, interactive format!

## 🎨 Example Output

For input: **"a magical forest at sunset"**

You will get:
- **Story**: 300-word creative narrative about the magical forest
- **Image**: High-quality 512x512 digital artwork
- **Audio**: ~10-second voice narration
- **HTML**: Professional showcase page

## 💡 Technical Highlights

### Models Used
| Task | Model | Size | Device | Purpose |
|------|-------|------|--------|---------|
| Text | GPT-2 Large | 774M | CPU | Story generation |
| Image | Stable Diffusion v1.5 | ~4GB | GPU | Artwork creation |
| Audio | Bark TTS | ~1GB | CPU | Voice synthesis |

### Resource Allocation
```python
Task A: CPU=1, MEM=512MB   (Lightweight preprocessing)
Task B: CPU=4, MEM=4GB     (Text generation)
Task C: GPU=1, VRAM=8GB    (Image generation)
Task D: CPU=4, MEM=4GB     (Audio synthesis)
Task E: CPU=1, MEM=512MB   (Result aggregation)
```

## 🚀 Performance Benefits

### Sequential vs Parallel Execution

**Sequential (without Maze):**
```
Task A: 5s → Task B: 60s → Task C: 90s → Task D: 45s → Task E: 2s
Total: 202 seconds (3.4 minutes)
```

**Parallel (with Maze):**
```
Task A: 5s → [Task B: 60s | Task C: 90s | Task D: 45s] → Task E: 2s
                    (parallel execution)
Total: 97 seconds (1.6 minutes)
```

**Speed Improvement: 2.1x faster!** ⚡

## 🔧 Customization

You can easily customize the workflow:

1. **Change Models**: Replace with your preferred models in the code
2. **Adjust Quality**: Modify generation parameters (steps, temperature, etc.)
3. **Add More Tasks**: Extend with video generation, translation, etc.
4. **Scale Out**: Add more workers to handle multiple concurrent requests

## 📝 Notes

- First run will download models (~8GB total)
- Requires NVIDIA GPU for image generation
- CPU-only mode available (slower image generation)
- Output directory: `example/outputs/`

## 🎯 Use Cases

This workflow is perfect for:
- **Content Creators**: Quick creative material generation
- **Marketing Teams**: Multi-format campaign assets
- **Education**: Demonstrate AI capabilities
- **Research**: Test distributed AI workflows
- **Prototyping**: Rapid content mockups

