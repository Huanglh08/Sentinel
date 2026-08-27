"""硅基流动嵌入客户端：严格对应 ragent SiliconFlowEmbeddingClient。

照搬字段：
  - provider() = "siliconflow"
  - max_batch_size() = 32
  - 其它走 OpenAIStyleEmbeddingClient 默认实现（Bearer auth + encoding_format=float）
"""
from __future__ import annotations

from core.embedding.base import ModelTarget
from core.embedding.openai_style import OpenAIStyleEmbeddingClient


class SiliconFlowEmbeddingClient(OpenAIStyleEmbeddingClient):
    """硅基流动（https://api.siliconflow.cn）——对应 ragent SiliconFlowEmbeddingClient。"""

    def provider(self) -> str:
        return "siliconflow"

    def max_batch_size(self) -> int:
        """严格对应 ragent SiliconFlowEmbeddingClient.maxBatchSize() = 32。"""
        return 32

    def embed_batch(self, texts: list[str], target: ModelTarget) -> list[list[float]]:
        return self.do_embed_batch(texts, target)
