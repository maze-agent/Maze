"""LiteLLM model implementation.

Routes to 100+ LLM providers (OpenAI, Anthropic, Google, Azure, Bedrock,
Ollama, etc.) via the litellm SDK. No proxy server needed.

Model strings use the ``provider/model`` format, e.g.
``anthropic/claude-sonnet-4-20250514``, ``azure/gpt-4o``,
``bedrock/anthropic.claude-3-haiku``, ``openai/gpt-4o``.

See https://docs.litellm.ai/docs/providers for all supported models.
"""

from typing import Any, Dict, List, Optional

from .base_model import BaseModel


class LiteLLMModel(BaseModel):
    """LiteLLM model implementation."""

    def __init__(self, api_key: str = "", model_name: str = "openai/gpt-4o"):
        super().__init__(api_key, model_name)

    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Perform chat completion via LiteLLM.

        Args:
            messages: List of messages in the conversation
            tools: Optional list of tools that can be used
            **kwargs: Additional parameters

        Returns:
            Model response in the same format as OpenAIModel
        """
        try:
            import litellm
        except ImportError:
            raise ImportError(
                "litellm is required for LiteLLMModel. "
                "Install with: pip install litellm"
            )

        params: Dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "drop_params": True,
        }

        if self.api_key:
            params["api_key"] = self.api_key

        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"

        params.update(kwargs)

        response = await litellm.acompletion(**params)

        message = response.choices[0].message
        tool_calls = []
        if hasattr(message, "tool_calls") and message.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in message.tool_calls
            ]

        result = {
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": message.role,
                        "content": message.content,
                        "tool_calls": tool_calls,
                    },
                    "finish_reason": response.choices[0].finish_reason,
                }
            ]
        }

        return result
