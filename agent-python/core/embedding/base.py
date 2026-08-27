"""嵌入层接口 + ModelTarget + EmbeddingResult：严格对应 ragent infra-ai.embedding。

包含：
  - ModelTarget record：对应 ragent infra-ai.model.ModelTarget
  - EmbeddingResult：嵌入调用的返回结构（带 model/provider/dimension 标记，便于熔断统计）
  - BaseEmbeddingClient 接口：对应 ragent EmbeddingClient

ragent EmbeddingClient 的方法签名：
  List<Float> embed(String text, ModelTarget target)
  List<List<Float>> embedBatch(List<String> texts, ModelTarget target)
Python 这边用 snake_case：embed_one / embed_batch。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from core.embedding.config import ModelCandidate, ProviderConfig


@dataclass(frozen=True)
class ModelTarget:
    """模型目标配置——严格对应 ragent ModelTarget record。

    字段：
      id: 模型候选 id（如 "qwen-emb-8b"）
      candidate: 候选模型配置（model 名 + dimension + priority）
      provider: 提供商配置（url + api_key + endpoints）
      timeout_ms: 本次调用的超时预算（毫秒），None 表示走 HTTP 客户端默认
    """

    id: str
    candidate: ModelCandidate
    provider: ProviderConfig
    timeout_ms: int | None = None


@dataclass(frozen=True)
class EmbeddingResult:
    """嵌入调用的结果包装：含来源 model_id/provider，便于上层审计/降级回溯。"""

    vectors: list[list[float]]
    model_id: str
    provider: str
    dimension: int


class BaseEmbeddingClient(ABC):
    """嵌入客户端接口——严格对应 ragent infra-ai.embedding.EmbeddingClient。

    子类必须实现 provider() 与 embed_batch()；embed() 有默认实现（走 batch）。
    钩子方法与 ragent AbstractOpenAIStyleEmbeddingClient 完全对齐：
      - requires_api_key(): Ollama 覆写为 False
      - customize_request_body(): Ollama 覆写为空（不发送 encoding_format）
      - max_batch_size(): 0=不限制；SiliconFlow/AIHubMix=32
    """

    @abstractmethod
    def provider(self) -> str:
        """提供商 id（与 ModelProvider 枚举的 id 完全一致，如 'siliconflow'）。"""
        ...

    def embed(self, text: str, target: ModelTarget) -> list[float]:
        """单文本嵌入——默认实现走 batch；子类一般不需要覆写。"""
        return self.embed_batch([text], target)[0]

    @abstractmethod
    def embed_batch(self, texts: list[str], target: ModelTarget) -> list[list[float]]:
        """批量嵌入；空输入返回空列表（与 ragent 行为一致）。"""
        ...

    # ---------------- 钩子方法（子类可选覆写） ----------------

    def requires_api_key(self) -> bool:
        """是否要求 API Key——对应 ragent AbstractOpenAIStyleEmbeddingClient.requiresApiKey()。"""
        return True

    def customize_request_body(self, body: dict[str, Any], target: ModelTarget) -> None:
        """子类可覆写以添加 provider 特有请求字段。

        默认实现：添加 encoding_format=float（与 ragent customizeRequestBody 默认实现一致）。
        Ollama 覆写为空（不发送）。
        """
        body["encoding_format"] = "float"

    def max_batch_size(self) -> int:
        """单次请求最大批量大小，0 表示不限制——对应 ragent maxBatchSize()。"""
        return 0
