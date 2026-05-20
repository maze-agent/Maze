from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Dict

from maze.client.maze.decorator import task


def create_openai_react_llm_task(
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    config_path: str | None = None,
    task_name: str = "openai_react_decide",
    system_prompt: str | None = None,
    temperature: float = 0,
    max_tokens: int = 512,
    timeout: int = 60,
    resources: Dict[str, Any] | None = None,
) -> Callable[..., Dict[str, Any]]:
    """Create an OpenAI-compatible ReAct decision task.

    The returned function is a Maze @task. It calls `/chat/completions` and
    returns `{"action": ...}` where action is a ReAct tool/final action.
    """

    if config_path is None and (base_url is None or model is None):
        raise ValueError("base_url and model are required unless config_path is provided")
    if api_key is None and api_key_env is None and config_path is None:
        raise ValueError("api_key, api_key_env, or config_path is required")

    normalized_base_url = base_url.rstrip("/") if base_url else None
    default_system_prompt = system_prompt or (
        "You are a ReAct controller. Return strict JSON only. "
        "Use either {\"tool\": \"tool_name\", \"args\": {...}} or "
        "{\"final\": \"answer\"}."
    )
    task_resources = resources or {"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0}

    def _openai_react_decide(
        prompt: str,
        history: list,
        tools: dict,
        step: int,
        system_prompt: str | None = None,
    ):
        import json
        import os
        import re
        import requests

        config_data = {}
        if config_path:
            with open(config_path, "r", encoding="utf-8") as handle:
                config_data = json.load(handle)

        resolved_base_url = config_data.get("url") or config_data.get("base_url") or normalized_base_url
        resolved_model = config_data.get("model") or model
        resolved_key = config_data.get("key") or config_data.get("api_key") or api_key
        resolved_env = config_data.get("key_env") or config_data.get("api_key_env") or api_key_env
        if not resolved_key and resolved_env:
            resolved_key = os.environ.get(resolved_env)

        if not resolved_base_url:
            raise ValueError("OpenAI-compatible ReAct task is missing base_url")
        if not resolved_model:
            raise ValueError("OpenAI-compatible ReAct task is missing model")
        if not resolved_key:
            raise ValueError("OpenAI-compatible ReAct task is missing api key")

        tool_summary = {
            name: {
                "description": spec.get("description", ""),
                "inputs": spec.get("inputs", []),
                "required_inputs": spec.get("required_inputs", []),
                "outputs": spec.get("outputs", []),
                "data_types": spec.get("data_types", {}),
            }
            for name, spec in (tools or {}).items()
        }
        history_summary = [
            {
                "step": item.get("index"),
                "tool": item.get("tool"),
                "args": item.get("args"),
                "observation": item.get("observation"),
            }
            for item in (history or [])
        ]
        user_payload = {
            "task": prompt,
            "step": step,
            "available_tools": tool_summary,
            "history": history_summary,
            "instructions": (
                "Return JSON only. To call a tool, return "
                "{\"tool\": \"tool_name\", \"args\": {...}}. "
                "To finish, return {\"final\": \"answer\"}. "
                "Use only available tool names. If write_file, read_file, and exec_code "
                "are available, you may create and run a Python helper when no direct "
                "domain tool exists."
            ),
        }
        messages = [
            {"role": "system", "content": system_prompt or default_system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]
        response = requests.post(
            str(resolved_base_url).rstrip("/") + "/chat/completions",
            headers={
                "Authorization": "Bearer " + str(resolved_key),
                "Content-Type": "application/json",
            },
            json={
                "model": str(resolved_model),
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"].get("content", "").strip()

        try:
            payload = json.loads(content)
        except Exception:
            match = re.search(r"\{.*\}", content, re.S)
            if not match:
                payload = {"final": content}
            else:
                payload = json.loads(match.group(0))

        if not isinstance(payload, dict):
            action = {"final": str(payload)}
        else:
            action = payload.get("action", payload)
            if not isinstance(action, dict):
                action = {"final": str(action)}
            elif "final" in action:
                action = {"final": action["final"]}
            else:
                action_type = action.get("type") or action.get("action")
                if action_type == "final":
                    action = {
                        "final": action.get("answer", action.get("result", action.get("content", ""))),
                    }
                else:
                    tool_name = action.get("tool")
                    if not tool_name and isinstance(action_type, str) and action_type not in {"tool", "call_tool"}:
                        tool_name = action_type
                    if tool_name:
                        action = {
                            "tool": tool_name,
                            "args": action.get("args") or {},
                        }
                    else:
                        action = {"final": json.dumps(payload, ensure_ascii=False)}

        return {
            "action": action,
            "raw": content[:1000],
        }

    decorated = task(resources=task_resources)(_openai_react_decide)
    decorated.__name__ = task_name
    decorated.__qualname__ = task_name
    decorated._maze_task_metadata.func_name = task_name
    return decorated


def _load_openai_react_config(
    *,
    base_url: str | None,
    model: str | None,
    api_key: str | None,
    api_key_env: str | None,
    config_path: str | None,
) -> dict[str, str]:
    config: dict[str, Any] = {}
    if config_path:
        with open(config_path, "r", encoding="utf-8") as handle:
            config = json.load(handle)

    resolved_base_url = config.get("url") or config.get("base_url") or base_url
    resolved_model = config.get("model") or model
    resolved_key = config.get("key") or config.get("api_key") or api_key

    env_name = config.get("key_env") or config.get("api_key_env") or api_key_env
    if not resolved_key and env_name:
        resolved_key = os.environ.get(env_name)

    if not resolved_base_url:
        raise ValueError("OpenAI-compatible ReAct task is missing base_url")
    if not resolved_model:
        raise ValueError("OpenAI-compatible ReAct task is missing model")
    if not resolved_key:
        raise ValueError("OpenAI-compatible ReAct task is missing api key")

    return {
        "base_url": str(resolved_base_url).rstrip("/"),
        "model": str(resolved_model),
        "api_key": str(resolved_key),
    }


def _build_react_messages(
    *,
    prompt: str,
    history: list,
    tools: dict,
    step: int,
    system_prompt: str,
) -> list[dict[str, str]]:
    tool_summary = {
        name: {
            "description": spec.get("description", ""),
            "inputs": spec.get("inputs", []),
            "required_inputs": spec.get("required_inputs", []),
            "outputs": spec.get("outputs", []),
            "data_types": spec.get("data_types", {}),
        }
        for name, spec in (tools or {}).items()
    }
    history_summary = [
        {
            "step": item.get("index"),
            "tool": item.get("tool"),
            "args": item.get("args"),
            "observation": item.get("observation"),
        }
        for item in (history or [])
    ]
    user_payload = {
        "task": prompt,
        "step": step,
        "available_tools": tool_summary,
        "history": history_summary,
        "instructions": (
            "Return JSON only. To call a tool, return "
            "{\"tool\": \"tool_name\", \"args\": {...}}. "
            "To finish, return {\"final\": \"answer\"}. "
            "Use only available tool names. If write_file, read_file, and exec_code "
            "are available, you may create and run a Python helper when no direct "
            "domain tool exists."
        ),
    }
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


def _parse_react_action(content: str) -> dict[str, Any]:
    try:
        payload = json.loads(content)
    except Exception:
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            return {"final": content}
        payload = json.loads(match.group(0))

    if not isinstance(payload, dict):
        return {"final": str(payload)}

    action = payload.get("action", payload)
    if not isinstance(action, dict):
        return {"final": str(action)}

    if "final" in action:
        return {"final": action["final"]}

    action_type = action.get("type") or action.get("action")
    if action_type == "final":
        return {
            "final": action.get("answer", action.get("result", action.get("content", ""))),
        }

    tool_name = action.get("tool")
    if not tool_name and isinstance(action_type, str) and action_type not in {"tool", "call_tool"}:
        tool_name = action_type

    if tool_name:
        return {
            "tool": tool_name,
            "args": action.get("args") or {},
        }

    return {"final": json.dumps(payload, ensure_ascii=False)}
