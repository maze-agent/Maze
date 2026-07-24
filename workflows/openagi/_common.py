"""Shared helpers for Ascend-Maze-native OpenAGI workflow ports."""

from __future__ import annotations

from base64 import b64encode
from collections.abc import Mapping, Sequence
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import json
import math
import re

from ascend_maze.contracts.data import SharedFileRef
from ascend_maze.inference.contracts import ChatResponse


FINAL_ANSWER_RULES = (
    "You are a general AI assistant. I will ask you a question. Report your "
    "thoughts, and finish your answer with the following template: FINAL "
    "ANSWER: [YOUR FINAL ANSWER].\n"
    "YOUR FINAL ANSWER should be a number OR as few words as possible OR a "
    "comma separated list of numbers and/or strings.\n"
    "If you are asked for a number, do not use comma to write your number and "
    "do not use units such as $ or percent sign unless specified otherwise.\n"
    "If you are asked for a string, do not use articles or abbreviations, and "
    "write digits in plain text unless specified otherwise.\n"
)


def estimate_tokens(text: str) -> int:
    if not isinstance(text, str):
        return 0
    cjk_chars = sum(1 for char in text if "\u4E00" <= char <= "\u9FFF")
    non_cjk_text = re.sub(r"[\u4E00-\u9FFF]", " ", text).replace("\n", " ")
    return cjk_chars + int(len(non_cjk_text.split()) * 1.3)


def metadata_dict(metadata: object) -> dict[str, object]:
    if isinstance(metadata, Mapping):
        return {str(key): value for key, value in metadata.items()}
    return {}


def text_features(text: str, *, reason: int | None = None) -> dict[str, object]:
    features: dict[str, object] = {
        "text_length": len(text),
        "token_count": estimate_tokens(text),
    }
    if reason is not None:
        features["reason"] = reason
    return features


def inference_features(
    prompt: str,
    response: ChatResponse,
    *,
    batch_size: int = 1,
    reason: int = 0,
) -> dict[str, object]:
    return {
        "text_length": len(prompt),
        "token_count": estimate_tokens(prompt),
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "batch_size": batch_size,
        "reason": reason,
    }


def response_or_override(
    metadata: dict[str, object],
    key: str,
    response: ChatResponse,
) -> str:
    override = metadata.get(key)
    if isinstance(override, str) and override.strip():
        return override
    return response.text


def chat_prompt(
    prompt: str,
    metadata: dict[str, object],
    override_key: str,
    *,
    max_tokens: int = 4096,
) -> tuple[str, str, dict[str, object]]:
    from ascend_maze.inference import chat

    response = chat(
        [{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.0,
    )
    return (
        response_or_override(metadata, override_key, response),
        response.text,
        inference_features(prompt, response),
    )


def chat_prompt_batch(
    prompts: list[str],
    metadata: dict[str, object],
    override_key: str,
    *,
    max_tokens: int = 4096,
) -> tuple[list[str], dict[str, object]]:
    from ascend_maze.inference import chat

    answers: list[str] = []
    total_input_tokens = 0
    total_output_tokens = 0
    for index, prompt in enumerate(prompts):
        response = chat(
            [{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.0,
        )
        override = list_override(metadata, override_key, index)
        answers.append(response.text if override is None else override)
        total_input_tokens += response.input_tokens
        total_output_tokens += response.output_tokens
    return (
        answers,
        {
            "text_length": sum(len(prompt) for prompt in prompts),
            "token_count": sum(estimate_tokens(prompt) for prompt in prompts),
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "batch_size": len(prompts),
            "reason": 0,
        },
    )


def chat_image_prompt_batch(
    prompts: list[str],
    images: list[dict[str, object]],
    metadata: dict[str, object],
    override_key: str,
    *,
    max_tokens: int = 4096,
) -> tuple[list[str], dict[str, object]]:
    from ascend_maze.inference import chat

    if len(prompts) != len(images):
        raise ValueError("prompts and images must have the same length")
    answers: list[str] = []
    total_input_tokens = 0
    total_output_tokens = 0
    true_multimodal_count = 0
    for index, (prompt, image) in enumerate(zip(prompts, images)):
        content = image_content_parts(prompt, image)
        if isinstance(content, list):
            true_multimodal_count += 1
        response = chat(
            [{"role": "user", "content": content}],
            max_tokens=max_tokens,
            temperature=0.0,
        )
        override = list_override(metadata, override_key, index)
        answers.append(response.text if override is None else override)
        total_input_tokens += response.input_tokens
        total_output_tokens += response.output_tokens
    return (
        answers,
        {
            "text_length": sum(len(prompt) for prompt in prompts),
            "token_count": sum(estimate_tokens(prompt) for prompt in prompts),
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "batch_size": len(prompts),
            "reason": 0,
            "vision_input_mode": (
                "true_multimodal"
                if true_multimodal_count == len(images)
                else "mixed_or_text_only"
            ),
            "true_multimodal_count": true_multimodal_count,
        },
    )


def image_content_parts(
    prompt: str,
    image_info: dict[str, object],
) -> list[dict[str, object]] | str:
    content = image_info.get("content")
    if not isinstance(content, bytes) or not content:
        return prompt
    return [
        {"type": "text", "text": prompt},
        {
            "type": "image_url",
            "image_url": {
                "url": _image_data_url(
                    content,
                    str(image_info.get("file_name", "")),
                )
            },
        },
    ]


def list_override(
    metadata: dict[str, object],
    key: str,
    index: int,
) -> str | None:
    value = metadata.get(key)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if index < len(value) and isinstance(value[index], str):
            item = value[index].strip()
            if item:
                return item
    return None


def target_language_from_question(question: str) -> str:
    lowered = question.lower()
    if "german" in lowered or "deutsch" in lowered:
        return "German"
    if "chinese" in lowered or "中文" in lowered:
        return "Chinese"
    return "English"


def target_language_code_from_question(question: str) -> str:
    target = target_language_from_question(question)
    if target == "Chinese":
        return "zh"
    if target == "German":
        return "de"
    return "en"


def detect_language_code(text: str) -> str:
    if any("\u4E00" <= char <= "\u9FFF" for char in text):
        return "zh"
    if re.search(r"\b(und|der|die|das|nicht|ist|mit|für)\b", text.lower()):
        return "de"
    return "en"


def read_named_text_file(
    supplementary_files: object,
    expected_name: str,
) -> dict[str, object]:
    payload = _named_payload(supplementary_files, expected_name)
    content = _payload_to_bytes(payload)
    return {
        "file_name": expected_name,
        "content": _decode_text(content),
        "content_sha256": sha256(content).hexdigest(),
        "size_bytes": len(content),
        "source_kind": _source_kind(payload),
    }


def list_inline_images(supplementary_files: object) -> list[dict[str, object]]:
    if isinstance(supplementary_files, SharedFileRef):
        name = Path(supplementary_files.canonical_path).name
        if not _is_image_name(name):
            return []
        content = Path(supplementary_files.canonical_path).read_bytes()
        return [_image_record(name, content, "shared_file")]
    if not isinstance(supplementary_files, Mapping):
        return []
    images: list[dict[str, object]] = []
    for key, payload in supplementary_files.items():
        name = str(key)
        if not _is_image_name(name):
            continue
        if isinstance(payload, SharedFileRef) or (
            isinstance(payload, Mapping)
            and isinstance(payload.get("shared_file"), SharedFileRef)
        ):
            raise ValueError(
                "nested SharedFileRef image payloads are not supported; pass a "
                "single SharedFileRef as the top-level supplementary_files value "
                "or use inline bytes"
            )
        content = _payload_to_bytes(payload)
        images.append(_image_record(name, content, _source_kind(payload)))
    return images


def normalize_document_text(document_content: str) -> str:
    lines = [line.strip() for line in document_content.splitlines() if line.strip()]
    return "\n".join(dict.fromkeys(lines))


def document_structure_prompt(document_content: str) -> str:
    return (
        "Analyze the structure of the following document and provide a brief "
        "summary.\n\n"
        f"Document (first 3000 chars):\n{document_content[:3000]}"
    )


def document_qa_prompt(
    rare_content: str,
    document_structure: str,
    document_content: str,
    question: str,
) -> str:
    return (
        "Answer the question."
        f"rare_content: {rare_content}\n\n"
        f"Document Structure:\n{document_structure}\n\n"
        f"Content:\n{document_content[:12000]}\n\n"
        f"Question: {question}\n\nAnswer:"
    )


def split_document_questions(question: str) -> list[list[str]]:
    questions = split_question_lines(question)
    total = len(questions)
    first = int(0.2 * total)
    second = int(0.2 * total)
    return [
        questions[:first],
        questions[first : first + second],
        questions[first + second :],
    ]


def split_question_lines(question: str) -> list[str]:
    return [line.strip() for line in question.splitlines() if line.strip()]


def split_questions_even(question: str, parts: int = 3) -> list[list[str]]:
    questions = split_question_lines(question)
    if not questions:
        return [[] for _ in range(parts)]
    size = math.ceil(len(questions) / parts)
    batches = [questions[index : index + size] for index in range(0, len(questions), size)]
    while len(batches) < parts:
        batches.append([])
    return batches[:parts]


def split_four_20_20_20_40(items: list[dict[str, object]]) -> list[list[dict[str, object]]]:
    total = len(items)
    first = int(total * 0.2)
    second = int(total * 0.4)
    third = int(total * 0.6)
    return [items[:first], items[first:second], items[second:third], items[third:]]


def image_caption_prompt(
    target_language: str,
    caption: str,
    ocr_text: str,
) -> str:
    prompt = (
        "#Background#\n"
        f"{FINAL_ANSWER_RULES}"
        "#Question#\n"
        "You are an expert image analyst. Provide a detailed, fluent "
        f"description of the image in {target_language}.\n"
        f"Base visual analysis suggests: '{caption}'.\n"
    )
    if ocr_text:
        prompt += f"Text found in the image: '{ocr_text}'.\n"
    prompt += (
        "Combine all this information into a comprehensive description. Output "
        f"only the final description in {target_language}."
    )
    return prompt


def multimodal_vqa_prompt(question: str, image_info: dict[str, object]) -> str:
    return (
        "Carefully observe the attached image and answer the "
        f"following question: {question}\n\n"
        "Use the image itself as primary evidence. The metadata below is only "
        "diagnostic context.\n"
        f"Image metadata: {json.dumps(image_info.get('features', {}), sort_keys=True)}"
    )


def blip_prompt(image_info: dict[str, object]) -> str:
    return (
        "Generate a concise visual caption for the attached image. "
        "Use the image itself as primary evidence. "
        f"Image metadata: {json.dumps(image_info.get('features', {}), sort_keys=True)}"
    )


def ocr_prompt(image_info: dict[str, object], target_language: str) -> str:
    return (
        "Extract any readable OCR text from the attached image. "
        f"Preferred language: {target_language}. "
        "Use the image itself as primary evidence. "
        f"Image metadata: {json.dumps(image_info.get('features', {}), sort_keys=True)}"
    )


def text_summary_prompt(translated_text: str) -> str:
    return f"Summarize the following text concisely:\n\n{translated_text[:4000]}"


def text_sentiment_prompt(translated_text: str) -> str:
    return (
        "Classify the overall sentiment of the following text and include a "
        "short confidence explanation:\n\n"
        f"{translated_text[:4000]}"
    )


def text_translate_prompt(
    document_content: str,
    source_language: str,
    target_language: str,
) -> str:
    return (
        "Translate the following document if translation is needed. Return only "
        "the translated text.\n\n"
        f"Source language: {source_language}\n"
        f"Target language: {target_language}\n\n"
        f"Document:\n{document_content[:4000]}"
    )


def final_text_question_prompt(
    translated_text: str,
    summary: str,
    sentiment: str,
    instruction: str,
    question: str,
) -> str:
    return (
        "Please generate the final and complete answer strictly according to "
        "the user's instruction and specific question.\n\n"
        f"--- Original Text (possibly translated) ---\n{translated_text[:4000]}\n\n"
        f"--- Preliminary Analysis ---\nSummary: {summary}\nSentiment: {sentiment}\n\n"
        f"--- User Instruction ---\n{instruction}\n\n"
        f"--- Specific Question ---\n{question}"
    )


def image_records_with_features(images: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "file_name": image["file_name"],
            "content": image["content"],
            "features": image_features(bytes(image["content"])),
        }
        for image in images
    ]


def image_features(content: bytes) -> dict[str, object]:
    features: dict[str, object] = {
        "size_bytes": len(content),
        "content_sha256": sha256(content).hexdigest(),
        "image_width": 0,
        "image_height": 0,
        "image_area": 0,
        "image_aspect_ratio": 0.0,
        "avg_brightness": 0.0,
    }
    if not content:
        return features
    try:
        from PIL import Image

        image = Image.open(BytesIO(content)).convert("RGB")
        width, height = image.size
        red, green, blue = list(image.resize((1, 1)).getdata())[0]
        features["image_width"] = width
        features["image_height"] = height
        features["image_area"] = width * height
        features["image_aspect_ratio"] = width / height if height else 0.0
        features["avg_brightness"] = (red + green + blue) / 3.0
    except Exception:
        return features
    return features


def aggregate_feature_dicts(records: list[dict[str, object]]) -> dict[str, object]:
    values: dict[str, list[float]] = {}
    for record in records:
        raw_features = record.get("features", {})
        if not isinstance(raw_features, Mapping):
            continue
        for key, value in raw_features.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values.setdefault(str(key), []).append(float(value))
    return {
        key: sum(items) / len(items)
        for key, items in sorted(values.items())
        if items
    }


def batch_feature_summary(
    batch: list[dict[str, object]],
    prompt: str,
) -> dict[str, object]:
    summary = aggregate_feature_dicts(batch)
    summary["batch_size"] = len(batch)
    summary["prompt_length"] = len(prompt)
    summary["prompt_token_count"] = estimate_tokens(prompt)
    summary["reason"] = 0
    return summary


def format_named_answers(records: list[dict[str, object]], field: str) -> str:
    sorted_records = sorted(records, key=lambda item: str(item.get("file_name", "")))
    return "\n\n".join(
        f"Image {record.get('file_name', '')}: {record.get(field, '')}"
        for record in sorted_records
    )


def _named_payload(supplementary_files: object, expected_name: str) -> object:
    if isinstance(supplementary_files, SharedFileRef):
        return supplementary_files
    if not isinstance(supplementary_files, Mapping):
        raise ValueError("supplementary_files must be a mapping or SharedFileRef")
    if expected_name not in supplementary_files:
        raise ValueError(f"supplementary_files missing {expected_name!r}")
    payload = supplementary_files[expected_name]
    if isinstance(payload, SharedFileRef) or (
        isinstance(payload, Mapping)
        and isinstance(payload.get("shared_file"), SharedFileRef)
    ):
        raise ValueError(
            "nested SharedFileRef values are not supported; pass the single "
            "document as the top-level supplementary_files value"
        )
    return payload


def _payload_to_bytes(payload: object) -> bytes:
    if isinstance(payload, SharedFileRef):
        return Path(payload.canonical_path).read_bytes()
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, bytearray):
        return bytes(payload)
    if isinstance(payload, memoryview):
        return payload.tobytes()
    if isinstance(payload, str):
        return payload.encode("utf-8")
    if isinstance(payload, Mapping) and "content" in payload:
        return _payload_to_bytes(payload["content"])
    raise ValueError(
        "supplementary payload must be bytes, text, SharedFileRef, or a mapping "
        "with a 'content' field"
    )


def _decode_text(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("latin-1", errors="replace")


def _source_kind(payload: object) -> str:
    if isinstance(payload, SharedFileRef):
        return "shared_file"
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return "inline_bytes"
    if isinstance(payload, str):
        return "inline_text"
    if isinstance(payload, Mapping):
        return "inline_mapping"
    return "unknown"


def _is_image_name(name: str) -> bool:
    return name.lower().endswith((".png", ".jpg", ".jpeg"))


def _image_data_url(content: bytes, file_name: str) -> str:
    mime_type = _image_mime_type(file_name)
    return f"data:{mime_type};base64,{b64encode(content).decode('ascii')}"


def _image_mime_type(file_name: str) -> str:
    suffix = Path(file_name).suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".gif":
        return "image/gif"
    if suffix == ".bmp":
        return "image/bmp"
    return "image/png"


def _image_record(name: str, content: bytes, source_kind: str) -> dict[str, object]:
    return {
        "file_name": name,
        "content": content,
        "source_kind": source_kind,
        "features": image_features(content),
    }
