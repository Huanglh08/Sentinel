"""Ollama 嵌入客户端：严格对应 ragent OllamaEmbeddingClient。

照搬字段：
  - provider() = "ollama"
  - requires_api_key() = False（本地服务不需鉴权）
  - customize_request_body() 留空（不发送 encoding_format 字段）
  - max_batch_size() = 0（不限制；ragent OllamaEmbeddingClient 未覆写，继承 AbstractOpenAIStyleEmbeddingClient 默认 0）
"""
from __future__ import annotations

from typing import Any

from core.embedding.base import ModelTarget
from core.embedding.openai_style import OpenAIStyleEmbeddingClient


class OllamaEmbeddingClient(OpenAIStyleEmbeddingClient):
    """Ollama 本地（http://localhost:11434）——对应 ragent OllamaEmbeddingClient。"""

    def provider(self) -> str:
        return "ollama"

    def requires_api_key(self) -> bool:
        """严格对应 ragent OllamaEmbeddingClient.requiresApiKey() = false。"""
        return False

    def customize_request_body(self, body: dict[str, Any], target: ModelTarget) -> None:
        """严格对应 ragent OllamaEmbeddingClient.customizeRequestBody() 留空。"""
        return

    def max_batch_size(self) -> int:
        """0 = 不限制（ragent OllamaEmbeddingClient 未覆写 maxBatchSize）。"""
        return 0

    def embed_batch(self, texts: list[str], target: ModelTarget) -> list[list[float]]:
        return self.do_embed_batch(texts, target)
