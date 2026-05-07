"""Unit tests for LiteLLM model implementation."""

import sys
import types
import pytest
from unittest.mock import AsyncMock, MagicMock

from maze.agent.model.base_model import BaseModel
from maze.agent.model.litellm_model import LiteLLMModel


class TestLiteLLMModelAttributes:
    def test_extends_base_model(self):
        assert issubclass(LiteLLMModel, BaseModel)

    def test_default_model_name(self):
        model = LiteLLMModel()
        assert model.model_name == "openai/gpt-4o"

    def test_custom_model_name(self):
        model = LiteLLMModel(model_name="anthropic/claude-sonnet-4-20250514")
        assert model.model_name == "anthropic/claude-sonnet-4-20250514"

    def test_api_key_stored(self):
        model = LiteLLMModel(api_key="sk-test")
        assert model.api_key == "sk-test"


class TestLiteLLMModelChatCompletion:
    @pytest.mark.asyncio
    async def test_dispatches_to_litellm_acompletion(self):
        fake_litellm = types.ModuleType("litellm")
        mock_message = MagicMock()
        mock_message.role = "assistant"
        mock_message.content = "Hello!"
        mock_message.tool_calls = None
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_choice.finish_reason = "stop"
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        fake_litellm.acompletion = AsyncMock(return_value=mock_response)
        sys.modules["litellm"] = fake_litellm

        try:
            model = LiteLLMModel(api_key="sk-test", model_name="openai/gpt-4o")
            result = await model.chat_completion(
                messages=[{"role": "user", "content": "hi"}],
            )

            assert result["choices"][0]["message"]["content"] == "Hello!"
            assert result["choices"][0]["finish_reason"] == "stop"

            kwargs = fake_litellm.acompletion.call_args.kwargs
            assert kwargs["model"] == "openai/gpt-4o"
            assert kwargs["drop_params"] is True
            assert kwargs["api_key"] == "sk-test"
        finally:
            del sys.modules["litellm"]

    @pytest.mark.asyncio
    async def test_includes_drop_params_true(self):
        fake_litellm = types.ModuleType("litellm")
        mock_msg = MagicMock(role="assistant", content="ok", tool_calls=None)
        mock_choice = MagicMock(message=mock_msg, finish_reason="stop")
        mock_resp = MagicMock(choices=[mock_choice])
        fake_litellm.acompletion = AsyncMock(return_value=mock_resp)
        sys.modules["litellm"] = fake_litellm

        try:
            model = LiteLLMModel(model_name="anthropic/claude-haiku-4-5")
            await model.chat_completion(messages=[{"role": "user", "content": "hi"}])
            assert fake_litellm.acompletion.call_args.kwargs["drop_params"] is True
        finally:
            del sys.modules["litellm"]

    @pytest.mark.asyncio
    async def test_omits_api_key_when_empty(self):
        fake_litellm = types.ModuleType("litellm")
        mock_msg = MagicMock(role="assistant", content="ok", tool_calls=None)
        mock_choice = MagicMock(message=mock_msg, finish_reason="stop")
        mock_resp = MagicMock(choices=[mock_choice])
        fake_litellm.acompletion = AsyncMock(return_value=mock_resp)
        sys.modules["litellm"] = fake_litellm

        try:
            model = LiteLLMModel(model_name="openai/gpt-4o")
            await model.chat_completion(messages=[{"role": "user", "content": "hi"}])
            assert "api_key" not in fake_litellm.acompletion.call_args.kwargs
        finally:
            del sys.modules["litellm"]

    @pytest.mark.asyncio
    async def test_passes_tools(self):
        fake_litellm = types.ModuleType("litellm")
        mock_msg = MagicMock(role="assistant", content="ok", tool_calls=None)
        mock_choice = MagicMock(message=mock_msg, finish_reason="stop")
        mock_resp = MagicMock(choices=[mock_choice])
        fake_litellm.acompletion = AsyncMock(return_value=mock_resp)
        sys.modules["litellm"] = fake_litellm

        try:
            model = LiteLLMModel(model_name="openai/gpt-4o")
            tools = [{"type": "function", "function": {"name": "test", "parameters": {}}}]
            await model.chat_completion(
                messages=[{"role": "user", "content": "hi"}], tools=tools
            )
            kwargs = fake_litellm.acompletion.call_args.kwargs
            assert kwargs["tools"] == tools
            assert kwargs["tool_choice"] == "auto"
        finally:
            del sys.modules["litellm"]

    @pytest.mark.asyncio
    async def test_handles_tool_call_response(self):
        fake_litellm = types.ModuleType("litellm")
        mock_tc = MagicMock()
        mock_tc.id = "call_123"
        mock_tc.function.name = "get_weather"
        mock_tc.function.arguments = '{"city": "London"}'
        mock_msg = MagicMock(role="assistant", content=None, tool_calls=[mock_tc])
        mock_choice = MagicMock(message=mock_msg, finish_reason="tool_calls")
        mock_resp = MagicMock(choices=[mock_choice])
        fake_litellm.acompletion = AsyncMock(return_value=mock_resp)
        sys.modules["litellm"] = fake_litellm

        try:
            model = LiteLLMModel(model_name="openai/gpt-4o")
            result = await model.chat_completion(
                messages=[{"role": "user", "content": "weather?"}]
            )
            tc = result["choices"][0]["message"]["tool_calls"][0]
            assert tc["function"]["name"] == "get_weather"
            assert tc["id"] == "call_123"
        finally:
            del sys.modules["litellm"]

    @pytest.mark.asyncio
    async def test_raises_import_error_when_litellm_missing(self):
        sys.modules["litellm"] = None  # type: ignore
        try:
            model = LiteLLMModel(model_name="openai/gpt-4o")
            with pytest.raises(ImportError, match="litellm is required"):
                await model.chat_completion(messages=[{"role": "user", "content": "hi"}])
        finally:
            del sys.modules["litellm"]

    @pytest.mark.asyncio
    async def test_returns_same_format_as_openai_model(self):
        fake_litellm = types.ModuleType("litellm")
        mock_msg = MagicMock(role="assistant", content="test", tool_calls=None)
        mock_choice = MagicMock(message=mock_msg, finish_reason="stop")
        mock_resp = MagicMock(choices=[mock_choice])
        fake_litellm.acompletion = AsyncMock(return_value=mock_resp)
        sys.modules["litellm"] = fake_litellm

        try:
            model = LiteLLMModel(model_name="openai/gpt-4o")
            result = await model.chat_completion(
                messages=[{"role": "user", "content": "hi"}]
            )
            assert "choices" in result
            assert result["choices"][0]["index"] == 0
            assert "message" in result["choices"][0]
            assert "role" in result["choices"][0]["message"]
            assert "content" in result["choices"][0]["message"]
            assert "tool_calls" in result["choices"][0]["message"]
            assert "finish_reason" in result["choices"][0]
        finally:
            del sys.modules["litellm"]
