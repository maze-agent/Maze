"""Shared helpers for Ascend-Maze-native GAIA workflow ports."""

from __future__ import annotations

from base64 import b64encode
from collections.abc import Mapping
from csv import reader as csv_reader
from hashlib import sha256
from io import BytesIO, StringIO
import json
from pathlib import Path
import re
import wave
import xml.etree.ElementTree as ET
from xml.dom import minidom
from zipfile import ZipFile

from ascend_maze.contracts.data import SharedFileRef
from ascend_maze.inference.contracts import ChatResponse


GAIA_FINAL_ANSWER_RULES = (
    "You are a general AI assistant. I will ask you a question. Report your "
    "thoughts, and finish your answer with the following template: FINAL "
    "ANSWER: [YOUR FINAL ANSWER].\n"
    "YOUR FINAL ANSWER should be a number OR as few words as possible OR a "
    "comma separated list of numbers and/or strings.\n"
    "If you are asked for a number, don’t use comma to write your number neither "
    "use units such as $ or percent sign unless specified otherwise.\n"
    "If you are asked for a string, don’t use articles, neither abbreviations "
    "(e.g. for cities), and write the digits in plain text unless specified "
    "otherwise.\n"
    "If you are asked for a comma separated list, apply the above rules depending "
    "of whether the element to be put in the list is a number or a string."
)


def estimate_tokens(text: str) -> int:
    """Estimate mixed Chinese/English prompt tokens without importing tokenizers."""

    cjk_chars = sum(1 for char in text if "\u4E00" <= char <= "\u9FFF")
    non_cjk_text = re.sub(r"[\u4E00-\u9FFF]", " ", text).replace("\n", " ")
    non_cjk_words_count = len(non_cjk_text.split())
    return cjk_chars + int(non_cjk_words_count * 1.3)


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


def empty_time_record() -> dict[str, object]:
    return {
        "get_time": 0.0,
        "put_size_bytes": 0,
        "get_size_bytes": 0,
    }


def model_runtime_inputs(api_parameter: str) -> dict[str, object]:
    return {
        "use_online_model": False,
        "model_folder": "",
        "temperature": 0.0,
        "max_tokens": 4096,
        "top_p": 0.9,
        "repetition_penalty": 1.1,
        api_parameter: "",
    }


def inference_features(
    prompt: str,
    response: ChatResponse,
    *,
    reason: int | None = None,
) -> dict[str, object]:
    features = text_features(prompt, reason=reason)
    features["input_tokens"] = response.input_tokens
    features["output_tokens"] = response.output_tokens
    return features


def first_supplementary_file(supplementary_files: object) -> dict[str, object]:
    """Return the first explicit GAIA file payload.

    The contract is intentionally explicit: callers may pass bytes-like objects,
    literal text content, ``SharedFileRef`` instances, or a mapping whose value is
    one of those forms. Ordinary strings are treated as literal file content, not
    as paths.
    """

    if supplementary_files is None:
        return {
            "file_name": "",
            "content_bytes": b"",
            "content_sha256": "",
            "size_bytes": 0,
            "source_kind": "empty",
        }
    if isinstance(supplementary_files, SharedFileRef):
        content = Path(supplementary_files.canonical_path).read_bytes()
        return {
            "file_name": Path(supplementary_files.canonical_path).name,
            "content_bytes": content,
            "content_sha256": supplementary_files.content_sha256,
            "size_bytes": supplementary_files.size_bytes,
            "source_kind": "shared_file",
        }
    if isinstance(supplementary_files, Mapping):
        if not supplementary_files:
            return first_supplementary_file(None)
        first_key = next(iter(supplementary_files))
        first_name = str(first_key)
        payload = supplementary_files[first_key]
        if isinstance(payload, SharedFileRef) or (
            isinstance(payload, Mapping)
            and isinstance(payload.get("shared_file"), SharedFileRef)
        ):
            raise ValueError(
                "nested SharedFileRef values are not supported; pass the "
                "single GAIA supplementary file as the top-level "
                "supplementary_files value"
            )
        content = _payload_to_bytes(payload)
        return {
            "file_name": first_name,
            "content_bytes": content,
            "content_sha256": sha256(content).hexdigest(),
            "size_bytes": len(content),
            "source_kind": _source_kind(payload),
        }
    content = _payload_to_bytes(supplementary_files)
    return {
        "file_name": "supplementary_file",
        "content_bytes": content,
        "content_sha256": sha256(content).hexdigest(),
        "size_bytes": len(content),
        "source_kind": _source_kind(supplementary_files),
    }


def process_document_file(supplementary_files: object) -> dict[str, object]:
    file_payload = first_supplementary_file(supplementary_files)
    file_name = str(file_payload["file_name"])
    content = bytes(file_payload["content_bytes"])
    extension = Path(file_name).suffix.lower()
    processed_content = _extract_document_text(file_name, content)
    return {
        "file_name": file_name,
        "file_extension": extension,
        "source_kind": file_payload["source_kind"],
        "content_sha256": file_payload["content_sha256"],
        "size_bytes": file_payload["size_bytes"],
        "processed_content": processed_content,
        "processed_chars": len(processed_content),
    }


def summarize_audio_file(supplementary_files: object) -> dict[str, object]:
    file_payload = first_supplementary_file(supplementary_files)
    file_name = str(file_payload["file_name"])
    content = bytes(file_payload["content_bytes"])
    summary: dict[str, object] = {
        "file_name": file_name,
        "file_extension": Path(file_name).suffix.lower(),
        "source_kind": file_payload["source_kind"],
        "content_sha256": file_payload["content_sha256"],
        "size_bytes": file_payload["size_bytes"],
        "duration": 0.0,
        "sample_rate": 0,
        "channels": 0,
        "audio_energy": float(sum(byte * byte for byte in content[:4096])),
    }
    if content:
        try:
            with wave.open(BytesIO(content), "rb") as wav:
                frames = wav.getnframes()
                rate = wav.getframerate()
                channels = wav.getnchannels()
                summary["sample_rate"] = rate
                summary["channels"] = channels
                summary["duration"] = float(frames / rate) if rate else 0.0
        except (EOFError, wave.Error):
            summary["duration"] = 0.0
    return {
        "file_name": file_name,
        "audio_bytes": content,
        "audio_features": summary,
    }


def summarize_image_file(supplementary_files: object) -> dict[str, object]:
    file_payload = first_supplementary_file(supplementary_files)
    file_name = str(file_payload["file_name"])
    content = bytes(file_payload["content_bytes"])
    features: dict[str, object] = {
        "file_name": file_name,
        "file_extension": Path(file_name).suffix.lower(),
        "source_kind": file_payload["source_kind"],
        "content_sha256": file_payload["content_sha256"],
        "size_bytes": file_payload["size_bytes"],
        "image_width": 0,
        "image_height": 0,
        "image_area": 0,
        "image_aspect_ratio": 0.0,
        "avg_brightness": 0.0,
    }
    if content:
        try:
            from PIL import Image

            image = Image.open(BytesIO(content)).convert("RGB")
            width, height = image.size
            pixels = list(image.resize((1, 1)).getdata())
            red, green, blue = pixels[0]
            features["image_width"] = width
            features["image_height"] = height
            features["image_area"] = width * height
            features["image_aspect_ratio"] = width / height if height else 0.0
            features["avg_brightness"] = (red + green + blue) / 3.0
        except Exception:
            features["image_width"] = 0
    return {
        "file_name": file_name,
        "image_bytes": content,
        "image_features": features,
    }


def gaia_initial_prompt(question: str) -> str:
    return f"{GAIA_FINAL_ANSWER_RULES}\nQuestion: {question}"


def gaia_question_prompt(question: str, extracted_label: str, extracted_text: str) -> str:
    prompt = (
        "#Background#\n"
        f"{GAIA_FINAL_ANSWER_RULES}\n"
        f"#Question#\n{question}\n"
    )
    if extracted_label:
        prompt += f"#{extracted_label}#\n{extracted_text[:8000]}\n"
    return prompt


def gaia_deepseek_prompt(
    question: str,
    extracted_label: str,
    extracted_text: str,
) -> str:
    prompt = (
        "#Background#\n"
        "You are a general AI assistant. I will ask you a question. Report your "
        "concise thinking thoughts and don't think too complicated, and finish "
        "your answer with the following template: FINAL ANSWER: [YOUR FINAL "
        "ANSWER].\n"
        "YOUR FINAL ANSWER should be a number OR as few words as possible OR a "
        "comma separated list of numbers and/or strings.\n"
        "If you are asked for a number, don’t use comma to write your number "
        "neither use units such as $ or percent sign unless specified otherwise.\n"
        "If you are asked for a string, don’t use articles, neither abbreviations "
        "(e.g. for cities), and write the digits in plain text unless specified "
        "otherwise.\n"
        "If you are asked for a comma separated list, apply the above rules "
        "depending of whether the element to be put in the list is a number or a "
        "string.\n"
        f"#Question#\n{question}\n"
    )
    if extracted_label:
        prompt += f"#{extracted_label}#\n{extracted_text[:8000]}\n"
    return prompt


def gaia_fusion_prompt(question: str, qwen_answer: str, deepseek_answer: str) -> str:
    return (
        "You are a senior editor and a world-class reasoning expert. Your job "
        "is to synthesize the answers from two different AI assistants to "
        "produce one final, superior answer for the given question.\n\n"
        f"--- Original Question ---\n{question}\n\n"
        f"--- Answer from Assistant 1 (Qwen3) ---\n{qwen_answer}\n\n"
        f"--- Answer from Assistant 2 (DeepSeek) ---\n{deepseek_answer}\n\n"
        "--- Your Task ---\n"
        "Analyze both answers. Identify the strengths and weaknesses of each. "
        "Then, combine their best elements, correct any errors, and provide a "
        "single, comprehensive, and accurate final answer. Adhere to the final "
        "answer format requested in the original prompt.\n\n"
        "Report your thoughts, and finish your answer with the following "
        "template: FINAL ANSWER: [YOUR FINAL ANSWER]."
    )


def speech_transcription_prompt(
    question: str,
    audio_features: dict[str, object],
) -> str:
    return (
        "Transcribe the GAIA supplementary audio as accurately as possible. "
        "Use the audio metadata only as diagnostic context.\n\n"
        f"Question that will use the transcript:\n{question}\n\n"
        f"Audio metadata:\n{json.dumps(audio_features, sort_keys=True)}"
    )


def vision_prompt(question: str, image_features: dict[str, object]) -> str:
    del image_features
    return gaia_question_prompt(question, "", "")


def vision_content_parts(
    question: str,
    image_bytes: bytes,
    image_features: dict[str, object],
) -> list[dict[str, object]] | str:
    prompt = vision_prompt(question, image_features)
    if not image_bytes:
        return prompt
    return [
        {"type": "text", "text": prompt},
        {
            "type": "image_url",
            "image_url": {
                "url": _image_data_url(
                    image_bytes,
                    str(image_features.get("file_extension", "")),
                )
            },
        },
    ]


def _image_data_url(image_bytes: bytes, extension: str) -> str:
    mime_type = _image_mime_type(extension)
    return f"data:{mime_type};base64,{b64encode(image_bytes).decode('ascii')}"


def _image_mime_type(extension: str) -> str:
    normalized = extension.lower()
    if normalized in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if normalized == ".webp":
        return "image/webp"
    if normalized == ".gif":
        return "image/gif"
    if normalized == ".bmp":
        return "image/bmp"
    return "image/png"


def response_or_override(
    metadata: dict[str, object],
    key: str,
    response: ChatResponse,
) -> str:
    override = metadata.get(key)
    if isinstance(override, str) and override.strip():
        return override
    return response.text


def text_feature_for_answer(prefix: str, answer: str) -> dict[str, object]:
    return {
        f"{prefix}_length": len(answer),
        f"{prefix}_token_count": estimate_tokens(answer),
    }


def _payload_to_bytes(payload: object) -> bytes:
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
        "supplementary file payload must be bytes, text, SharedFileRef, "
        "or a mapping with a 'content' field"
    )


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


def _decode_text(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("latin-1", errors="replace")


def _extract_document_text(file_name: str, content: bytes) -> str:
    if not content:
        return ""
    lowered = file_name.lower()
    if lowered.endswith((".txt", ".md")):
        return _decode_text(content)
    if lowered.endswith(".csv"):
        rows = csv_reader(StringIO(_decode_text(content)))
        return "\n".join(" | ".join(row) for row in rows)
    if lowered.endswith((".json", ".jsonl")):
        return _pretty_json_or_text(content)
    if lowered.endswith(".xml"):
        return _extract_xml_text(content)
    if lowered.endswith(".docx"):
        return _extract_docx_text(content)
    if lowered.endswith(".pptx"):
        return _extract_pptx_text(content)
    if lowered.endswith((".xlsx", ".xls")):
        return _extract_spreadsheet_text(content)
    if lowered.endswith(".pdf"):
        return _extract_pdf_text(content)
    return _decode_text(content)


def _pretty_json_or_text(content: bytes) -> str:
    text = _decode_text(content)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    return json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True)


def _extract_xml_text(content: bytes) -> str:
    text = _decode_text(content)
    try:
        pretty = minidom.parseString(text).toprettyxml()
    except Exception:
        pretty = text
    try:
        root = ET.fromstring(text)
        extracted = "".join(root.itertext())
    except ET.ParseError:
        extracted = ""
    if extracted:
        return f"=== XML Structure ===\n{pretty}\n\n=== Extracted Text Content ===\n{extracted}"
    return pretty


def _extract_docx_text(content: bytes) -> str:
    try:
        with ZipFile(BytesIO(content)) as archive:
            xml = archive.read("word/document.xml")
        root = ET.fromstring(xml)
        return "\n".join(text for text in root.itertext() if text.strip())
    except Exception:
        return _decode_text(content)


def _extract_pptx_text(content: bytes) -> str:
    try:
        slides: list[str] = []
        with ZipFile(BytesIO(content)) as archive:
            names = sorted(
                name
                for name in archive.namelist()
                if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            )
            for index, name in enumerate(names, start=1):
                root = ET.fromstring(archive.read(name))
                slide_text = "\n".join(text for text in root.itertext() if text.strip())
                slides.append(f"--- Slide {index} ---\n{slide_text}")
        return "\n\n".join(slides)
    except Exception:
        return _decode_text(content)


def _extract_spreadsheet_text(content: bytes) -> str:
    try:
        import pandas as pd

        with pd.ExcelFile(BytesIO(content)) as workbook:
            sheets = []
            for sheet_name in workbook.sheet_names:
                frame = pd.read_excel(workbook, sheet_name)
                sheets.append(f"=== Sheet: {sheet_name} ===\n{frame.to_string(index=True)}")
        return "\n\n".join(sheets)
    except Exception:
        return _decode_text(content)


def _extract_pdf_text(content: bytes) -> str:
    try:
        import pdfplumber

        pages = []
        with pdfplumber.open(BytesIO(content)) as pdf:
            for index, page in enumerate(pdf.pages, start=1):
                page_text = page.extract_text() or ""
                if page_text.strip():
                    pages.append(f"--- Page {index} ---\n{page_text}")
        return "\n\n".join(pages)
    except Exception:
        return _decode_text(content)
