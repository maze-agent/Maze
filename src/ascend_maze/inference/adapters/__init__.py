"""Inference engine adapter implementations."""

from ascend_maze.inference.adapters.fake import (
    FakeAdapterPlan,
    FakeInferenceEngineAdapter,
)
from ascend_maze.inference.adapters.transformers_local import (
    TransformersLocalGenerationConfig,
    TransformersLocalInferenceEngineAdapter,
    generate_chat_once,
)
from ascend_maze.inference.adapters.vllm_ascend import (
    HttpxVllmTransport,
    VllmAscendInferenceEngineAdapter,
    VllmHttpResponse,
    VllmHttpTransport,
)

__all__ = [
    "FakeAdapterPlan",
    "FakeInferenceEngineAdapter",
    "HttpxVllmTransport",
    "TransformersLocalGenerationConfig",
    "TransformersLocalInferenceEngineAdapter",
    "VllmAscendInferenceEngineAdapter",
    "VllmHttpResponse",
    "VllmHttpTransport",
    "generate_chat_once",
]
