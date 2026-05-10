"""
Medical health related tasks

Medical diagnosis assistance features implemented using Alibaba Cloud DashScope
"""

from maze.client.front.decorator import task
import os


@task(data_types={"tongue_image_path": "file:image", "tongue_features": "dict"}, resources={"cpu": 2, "cpu_mem": 512, "gpu": 0, "gpu_mem": 0})
def analyze_tongue_image(tongue_image_path: str):
    """
    Analyze tongue coating image features using VLM
    """
    import dashscope
    from dashscope import MultiModalConversation
    import base64

    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise ValueError("DASHSCOPE_API_KEY environment variable not found")

    dashscope.api_key = api_key

    with open(tongue_image_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")

    messages = [{
        "role": "user",
        "content": [
            {"image": f"data:image/jpeg;base64,{image_data}"},
            {"text": "请作为一名专业的中医师，仔细观察这张舌苔图片，并详细分析以下特征。"},
        ]
    }]

    response = MultiModalConversation.call(
        model="qwen-vl-max",
        messages=messages,
    )
    if response.status_code == 200:
        analysis_text = response.output.choices[0].message.content[0]["text"]
        tongue_features = {
            "raw_analysis": analysis_text,
            "image_path": tongue_image_path,
            "model": "qwen-vl-max",
        }
        return {"tongue_features": tongue_features}
    raise Exception(f"VLM analysis failed: {response.message}")


@task(data_types={"symptom_description": "str", "structured_symptoms": "dict"}, resources={"cpu": 1, "cpu_mem": 256, "gpu": 0, "gpu_mem": 0})
def extract_symptoms(symptom_description: str):
    """
    Extract structured symptom information from patient descriptions using LLM
    """
    import dashscope
    from dashscope import Generation
    import json

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

只输出JSON，不要其他内容。"""

    response = Generation.call(
        model="qwen-max",
        prompt=prompt,
        result_format="message",
    )
    if response.status_code == 200:
        extracted_text = response.output.choices[0].message.content
        try:
            if "```json" in extracted_text:
                extracted_text = extracted_text.split("```json")[1].split("```")[0]
            elif "```" in extracted_text:
                extracted_text = extracted_text.split("```")[1].split("```")[0]
            structured_data = json.loads(extracted_text.strip())
        except json.JSONDecodeError:
            structured_data = {
                "raw_extraction": extracted_text,
                "parse_error": "JSON parsing failed, returning raw text",
            }

        structured_symptoms = {
            "original_description": symptom_description,
            "extracted_data": structured_data,
            "model": "qwen-max",
        }
        return {"structured_symptoms": structured_symptoms}
    raise Exception(f"Symptom extraction failed: {response.message}")


@task(data_types={"tongue_features": "dict", "structured_symptoms": "dict", "medical_advice": "dict"}, resources={"cpu": 2, "cpu_mem": 512, "gpu": 0, "gpu_mem": 0})
def generate_medical_advice(tongue_features: dict, structured_symptoms: dict):
    """
    Generate medical advice by combining tongue diagnosis and symptoms using web search
    """
    import dashscope
    from dashscope import Generation
    import json

    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise ValueError("DASHSCOPE_API_KEY environment variable not found")

    dashscope.api_key = api_key

    prompt = f"""As an experienced integrated Chinese and Western medicine physician, please provide professional medical advice based on the following information:

【舌诊分析】
{json.dumps(tongue_features, ensure_ascii=False, indent=2)}

【症状信息】
{json.dumps(structured_symptoms, ensure_ascii=False, indent=2)}

只输出JSON，不要其他内容。"""

    response = Generation.call(
        model="qwen-max",
        prompt=prompt,
        result_format="message",
    )
    if response.status_code == 200:
        advice_text = response.output.choices[0].message.content
        try:
            if "```json" in advice_text:
                advice_text = advice_text.split("```json")[1].split("```")[0]
            elif "```" in advice_text:
                advice_text = advice_text.split("```")[1].split("```")[0]
            medical_advice = json.loads(advice_text.strip())
        except json.JSONDecodeError:
            medical_advice = {
                "raw_advice": advice_text,
                "parse_error": "JSON parsing failed, returning raw text",
            }
        return {"medical_advice": medical_advice}
    raise Exception(f"Medical advice generation failed: {response.message}")
