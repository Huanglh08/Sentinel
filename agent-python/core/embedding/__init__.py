"""嵌入抽象层：严格对齐 ragent-main infra-ai/embedding 设计。

完整吸收 ragent 的关键设计：
  - BaseEmbeddingClient 接口（对应 ragent EmbeddingClient）
  - OpenAIStyleEmbeddingClient 抽象基类（对应 ragent AbstractOpenAIStyleEmbeddingClient）
  - SiliconFlow / Ollama / AIHubMix 三个具体实现（逐字段照搬 max_batch=32、no api_key、no encoding_format）
  - ModelHealthStore（CLOSED/OPEN/HALF_OPEN 三态熔断器，对应 ragent ModelHealthStore）
  - ModelRoutingExecutor（按 priority 降级链 + 熔断准入，对应 ragent ModelRoutingExecutor）
  - RoutingEmbeddingService（统一入口，对应 ragent RoutingEmbeddingService）

默认模型与候选链与 ragent application.yaml L253-270 完全一致：
  qwen-emb-8b (siliconflow, Qwen/Qwen3-Embedding-8B) -> qwen-emb-local (ollama) -> text-embedding-3-large (aihubmix)
维度与 ragent rag.default.dimension: 1536 完全一致。
"""
from core.embedding.base import BaseEmbeddingClient, EmbeddingResult, ModelTarget
from core.embedding.config import EmbeddingConfig, ModelCandidate, ProviderConfig
from core.embedding.errors import ModelClientError, ModelClientErrorType
from core.embedding.health import ModelHealthStore, CircuitState
from core.embedding.routing import (
    ModelRoutingExecutor,
    NoAvailableModelException,
    RoutingEmbeddingService,
    SkippedModelException,
)

__all__ = [
    "BaseEmbeddingClient",
    "EmbeddingResult",
    "ModelTarget",
    "ModelClientError",
    "ModelClientErrorType",
    "EmbeddingConfig",
    "ModelCandidate",
    "ProviderConfig",
    "ModelHealthStore",
    "CircuitState",
    "ModelRoutingExecutor",
    "NoAvailableModelException",
    "RoutingEmbeddingService",
    "SkippedModelException",
]
