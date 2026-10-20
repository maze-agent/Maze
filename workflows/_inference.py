"""Small OpenAI-compatible inference client used by example workflows."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import requests
from maze.core.scheduler.error import ModelInferenceError, memory_failure_from_message


def _raise_model_error(message: str, base_url: str) -> None:
    failure = memory_failure_from_message(message, scope="external_service")
    if not failure:
        return
    failure["phase"] = "inference"
    route = json.loads(os.environ.get("MAZE_MODEL_ROUTE") or "{}")
    if route and route.get("endpoint", "").rstrip("/") == base_url.rstrip("/"):
        failure.update(scope="model_instance", instance_id=route["instance_id"])
    raise ModelInferenceError(message, failure)


def chat(
    messages: list[dict[str, object]],
    *,
    base_url: str,
    model: str,
    api_key: str,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    timeout: float = 120.0,
    chat_template_kwargs: dict[str, object] | None = None,
) -> str:
    """Call an OpenAI-compatible ``chat/completions`` endpoint."""

    if not base_url.strip():
        raise ValueError("base_url is required")
    if not model.strip():
        raise ValueError("model is required")

    request_body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if chat_template_kwargs is not None:
        request_body["chat_template_kwargs"] = chat_template_kwargs
    body = json.dumps(request_body).encode("utf-8")
    resolved_api_key = _resolve_api_key(api_key)
    headers = {"Content-Type": "application/json"}
    if resolved_api_key:
        headers["Authorization"] = f"Bearer {resolved_api_key}"

    request = Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        message = exc.read(65536).decode("utf-8", errors="replace")
        _raise_model_error(message, base_url)
        raise
    if isinstance(payload, dict) and ("error" in payload or "detail" in payload):
        _raise_model_error(json.dumps(payload, ensure_ascii=False), base_url)

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("chat response is missing choices[0].message.content") from exc
    if not isinstance(content, str):
        raise ValueError("chat response content must be a string")
    if not content.strip():
        raise ValueError(
            "chat response content is empty; check the generation token budget"
        )
    return content


def transcribe(
    content: bytes,
    *,
    file_name: str,
    base_url: str,
    model: str,
    api_key: str,
    timeout: float = 120.0,
) -> str:
    """Call an OpenAI-compatible ``audio/transcriptions`` endpoint."""

    if not content:
        raise ValueError("audio file is empty")
    if not base_url.strip():
        raise ValueError("base_url is required")
    if not model.strip():
        raise ValueError("model is required")

    config_path = Path(model) / "config.json"
    if config_path.is_file():
        config = json.loads(config_path.read_text())
        if (
            any(
                name.endswith("ForCausalLM") for name in config.get("architectures", [])
            )
            and not (config_path.parent / "preprocessor_config.json").is_file()
        ):
            raise ValueError(
                f"Transcription requires an audio model, but {model!r} is a text model "
                f"({config.get('model_type', 'unknown')}). Bind the transcription node "
                "to an audio model such as Whisper-large-v3."
            )

    resolved_api_key = _resolve_api_key(api_key)
    headers = {}
    if resolved_api_key:
        headers["Authorization"] = f"Bearer {resolved_api_key}"
    response = requests.post(
        f"{base_url.rstrip('/')}/audio/transcriptions",
        headers=headers,
        data={"model": model},
        files={"file": (file_name, content)},
        timeout=timeout,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = response.text[:65536]
        _raise_model_error(detail, base_url)
        raise requests.HTTPError(
            f"Audio transcription failed for model {model!r}: "
            f"HTTP {response.status_code}: {detail[:1024]}",
            response=response,
        ) from exc
    payload = response.json()
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise ValueError("transcription response is missing text")
    return text


def _resolve_api_key(api_key: str) -> str:
    if not api_key.startswith("env:"):
        return api_key
    variable = api_key.removeprefix("env:")
    if not variable:
        raise ValueError("api_key env reference must name an environment variable")
    try:
        return os.environ[variable]
    except KeyError as exc:
        raise ValueError(
            f"api_key environment variable is not set: {variable}"
        ) from exc
