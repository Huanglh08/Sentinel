"""AIHubMix 嵌入客户端：严格对应 ragent AIHubMixEmbeddingClient。

照搬字段：
  - provider() = "aihubmix"
  - max_batch_size() = 32
  - 其它走 OpenAIStyleEmbeddingClient 默认实现（Bearer auth + encoding_format=float）
"""
from __future__ import annotations

from core.embedding.base import ModelTarget
from core.embedding.openai_style import OpenAIStyleEmbeddingClient


class AIHubMixEmbeddingClient(OpenAIStyleEmbeddingClient):
    """AIHubMix（https://aihubmix.com/v1）——对应 ragent AIHubMixEmbeddingClient。"""

    def provider(self) -> str:
        return "aihubmix"

    def max_batch_size(self) -> int:
        """严格对应 ragent AIHubMixEmbeddingClient.maxBatchSize() = 32。"""
        return 32

    def embed_batch(self, texts: list[str], target: ModelTarget) -> list[list[float]]:
        return self.do_embed_batch(texts, target)
