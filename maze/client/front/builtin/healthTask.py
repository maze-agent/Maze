"""
Medical health related tasks

Medical diagnosis assistance features implemented using Alibaba Cloud DashScope
"""

from maze.client.front.decorator import task
import os


@task(
    inputs=["tongue_image_path"],
    outputs=["tongue_features"],
    data_types={
        "tongue_image_path": "file:image",
        "tongue_features": "dict"
    },
    resources={"cpu": 2, "cpu_mem": 512, "gpu": 0, "gpu_mem": 0}
)
def analyze_tongue_image(params):
    """
    Analyze tongue coating image features using VLM
    
    Input:
        tongue_image_path: Tongue coating image path (automatically uploaded to server)
        
    Output:
        tongue_features: Extracted tongue coating features (color, coating quality, shape, etc.)
    """
    import dashscope
    from dashscope import MultiModalConversation
    import base64
    import os
    tongue_image_path = params.get("tongue_image_path")
    api_key = os.getenv("DASHSCOPE_API_KEY")
    
    if not api_key:
        raise ValueError("DASHSCOPE_API_KEY environment variable not found")
    
    dashscope.api_key = api_key
    
    # Read image and convert to base64
    with open(tongue_image_path, 'rb') as f:
        image_data = base64.b64encode(f.read()).decode('utf-8')
    
    # Analyze tongue coating image using qwen-vl-max model
    messages = [{
        'role': 'user',
        'content': [
            {'image': f'data:image/jpeg;base64,{image_data}'},
            {'text': '''请作为一名专业的中医师，仔细观察这张舌苔图片，并详细分析以下特征：

1. 舌色（舌体颜色）：淡白、淡红、红、绛红等
2. 舌苔颜色：白、黄、灰、黑等
3. 舌苔厚薄：薄苔、厚苔、无苔等
4. 舌苔润燥：润苔、燥苔
5. 舌形：胖大、瘦薄、正常
6. 舌质：有无裂纹、齿痕、瘀斑等
7. 整体中医辨证分析

请用结构化的JSON格式输出，包含以上各项特征的描述。'''}
        ]
    }]
    
    response = MultiModalConversation.call(
        model='qwen-vl-max',
        messages=messages
    )
    print(response) 
    if response.status_code == 200:
        analysis_text = response.output.choices[0].message.content[0]['text']
        
        tongue_features = {
            "raw_analysis": analysis_text,
            "image_path": tongue_image_path,
            "model": "qwen-vl-max"
        }
        
        return {"tongue_features": tongue_features}
    else:
            raise Exception(f"VLM analysis failed: {response.message}")


@task(
    inputs=["symptom_description"],
    outputs=["structured_symptoms"],
    data_types={
        "symptom_description": "str",
        "structured_symptoms": "dict"
    },
    resources={"cpu": 1, "cpu_mem": 256, "gpu": 0, "gpu_mem": 0}
)
def extract_symptoms(params):
    """
    Extract structured symptom information from patient descriptions using LLM
    
    Input:
        symptom_description: Patient's symptom description text
        
    Output:
        structured_symptoms: Structured symptom information
    """
    import dashscope
    from dashscope import Generation
    import json
    import os
    symptom_description = params.get("symptom_description")
    api_key = os.getenv("DASHSCOPE_API_KEY")
    
    if not api_key:
        raise ValueError("DASHSCOPE_API_KEY environment variable not found")
    
    dashscope.api_key = api_key
    
    prompt = f"""作为一名专业的医疗信息提取专家，请从以下患者症状描述中提取关键信息，并以JSON格式输出：

患者描述：
{symptom_description}

请提取以下信息（若未提及则标注为"未提及"）：
1. 主要症状列表
2. 症状持续时间
3. 症状严重程度
4. 伴随症状
5. 可能的诱因
6. 既往病史（如有提及）
7. 生活习惯相关信息

输出格式示例：
{{
    "main_symptoms": ["症状1", "症状2"],
    "duration": "持续时间",
    "severity": "轻度/中度/重度",
    "accompanying_symptoms": ["伴随症状1", "伴随症状2"],
    "possible_triggers": ["诱因1"],
    "medical_history": "既往病史或未提及",
    "lifestyle_factors": ["相关生活习惯"],
    "summary": "症状总结"
}}

只输出JSON，不要其他内容。"""
    
    response = Generation.call(
        model='qwen-max',
        prompt=prompt,
        result_format='message'
    )
    print(response)
    if response.status_code == 200:
        extracted_text = response.output.choices[0].message.content
        
        # Try to parse JSON
        try:
            # Clean possible markdown code block markers
            if '```json' in extracted_text:
                extracted_text = extracted_text.split('```json')[1].split('```')[0]
            elif '```' in extracted_text:
                extracted_text = extracted_text.split('```')[1].split('```')[0]
            
            structured_data = json.loads(extracted_text.strip())
        except json.JSONDecodeError:
            # If parsing fails, return raw text
            structured_data = {
                "raw_extraction": extracted_text,
                "parse_error": "JSON parsing failed, returning raw text"
            }
        
        structured_symptoms = {
            "original_description": symptom_description,
            "extracted_data": structured_data,
            "model": "qwen-max"
        }
        
        return {"structured_symptoms": structured_symptoms}
    else:
        raise Exception(f"Symptom extraction failed: {response.message}")


@task(
    inputs=["tongue_features", "structured_symptoms"],
    outputs=["medical_advice"],
    data_types={
        "tongue_features": "dict",
        "structured_symptoms": "dict",
        "medical_advice": "dict"
    },
    resources={"cpu": 2, "cpu_mem": 512, "gpu": 0, "gpu_mem": 0}
)
def generate_medical_advice(params):
    """
    Generate medical advice by combining tongue diagnosis and symptoms using web search
    
    Input:
        tongue_features: Tongue coating feature analysis results
        structured_symptoms: Structured symptom information
        
    Output:
        medical_advice: Comprehensive medical advice
    """
    import dashscope
    from dashscope import Generation
    import json
    import os
    tongue_features = params.get("tongue_features")
    structured_symptoms = params.get("structured_symptoms")
    api_key = os.getenv("DASHSCOPE_API_KEY")
    
    if not api_key:
        raise ValueError("DASHSCOPE_API_KEY environment variable not found")
    
    dashscope.api_key = api_key
    
    # Build comprehensive analysis prompt
    prompt = f"""As an experienced integrated Chinese and Western medicine physician, please provide professional medical advice based on the following information:

【舌诊分析】
{json.dumps(tongue_features, ensure_ascii=False, indent=2)}

【症状信息】
{json.dumps(structured_symptoms, ensure_ascii=False, indent=2)}

请提供以下内容（使用JSON格式）：
1. 中医辨证分析（根据舌诊和症状）
2. 西医可能的诊断方向
3. 建议的检查项目
4. 生活调理建议（饮食、作息、运动等）
5. 中医调理建议（可能的中药方剂或穴位）
6. 就医建议（是否需要立即就医、看什么科室）
7. 注意事项

输出格式：
{{
    "tcm_diagnosis": "中医辨证分析",
    "western_diagnosis_direction": ["可能的西医诊断1", "可能的西医诊断2"],
    "recommended_tests": ["建议检查1", "建议检查2"],
    "lifestyle_advice": {{
        "diet": ["饮食建议"],
        "rest": ["作息建议"],
        "exercise": ["运动建议"]
    }},
    "tcm_treatment": {{
        "herbal_formula": "推荐方剂",
        "acupoints": ["穴位1", "穴位2"]
    }},
    "medical_visit": {{
        "urgency": "紧急/尽快/可择期",
        "department": "建议科室",
        "reason": "就医原因"
    }},
    "precautions": ["注意事项1", "注意事项2"],
    "disclaimer": "本建议仅供参考，具体诊疗请遵医嘱"
}}

只输出JSON，不要其他内容。"""
    
    # Use qwen-max enhanced with web search capability
    response = Generation.call(
        model='qwen-max',
        prompt=prompt,
        result_format='message',
        enable_search=True  # Enable web search
    )
    print(response)
    if response.status_code == 200:
        advice_text = response.output.choices[0].message.content
        
        # Try to parse JSON
        try:
            # Clean possible markdown code block markers
            if '```json' in advice_text:
                advice_text = advice_text.split('```json')[1].split('```')[0]
            elif '```' in advice_text:
                advice_text = advice_text.split('```')[1].split('```')[0]
            
            advice_data = json.loads(advice_text.strip())
        except json.JSONDecodeError:
            # If parsing fails, return raw text
            advice_data = {
                "raw_advice": advice_text,
                "parse_error": "JSON parsing failed, returning raw text"
            }
        
        medical_advice = {
            "advice_data": advice_data,
            "model": "qwen-max",
            "search_enabled": True,
            "timestamp": __import__('datetime').datetime.now().isoformat()
        }
        
        return {"medical_advice": medical_advice}
    else:
        raise Exception(f"Failed to generate medical advice: {response.message}")

