"""Prompt and feature helpers shared by the GAIA reasoning tasks."""

from __future__ import annotations

import re


GAIA_FINAL_ANSWER_RULES = (
    "You are a general AI assistant. I will ask you a question. Report your "
    "thoughts, and finish your answer with the following template: FINAL "
    "ANSWER: [YOUR FINAL ANSWER].\n"
    "YOUR FINAL ANSWER should be a number OR as few words as possible OR a "
    "comma separated list of numbers and/or strings.\n"
    "If you are asked for a number, don't use comma to write your number neither "
    "use units such as $ or percent sign unless specified otherwise.\n"
    "If you are asked for a string, don't use articles, neither abbreviations "
    "(e.g. for cities), and write the digits in plain text unless specified "
    "otherwise.\n"
    "If you are asked for a comma separated list, apply the above rules depending "
    "on whether the element to be put in the list is a number or a string."
)


def estimate_tokens(text: str) -> int:
    cjk_chars = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    non_cjk = re.sub(r"[\u4e00-\u9fff]", " ", text).replace("\n", " ")
    return cjk_chars + int(len(non_cjk.split()) * 1.3)


def text_features(text: str, *, reason: int | None = None) -> dict[str, object]:
    features: dict[str, object] = {
        "text_length": len(text),
        "token_count": estimate_tokens(text),
    }
    if reason is not None:
        features["reason"] = reason
    return features


def empty_time_record() -> dict[str, object]:
    return {"get_time": 0.0, "put_size_bytes": 0, "get_size_bytes": 0}


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
        "If you are asked for a number, don't use comma to write your number "
        "neither use units such as $ or percent sign unless specified otherwise.\n"
        "If you are asked for a string, don't use articles, neither abbreviations "
        "(e.g. for cities), and write the digits in plain text unless specified "
        "otherwise.\n"
        "If you are asked for a comma separated list, apply the above rules "
        "depending on whether the element to be put in the list is a number or a "
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


def text_feature_for_answer(prefix: str, answer: str) -> dict[str, object]:
    return {
        f"{prefix}_length": len(answer),
        f"{prefix}_token_count": estimate_tokens(answer),
    }
