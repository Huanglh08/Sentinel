"""具体嵌入 provider 客户端——与 ragent infra-ai.embedding 子包对应。"""
from core.embedding.clients.aihubmix import AIHubMixEmbeddingClient
from core.embedding.clients.ollama import OllamaEmbeddingClient
from core.embedding.clients.siliconflow import SiliconFlowEmbeddingClient

__all__ = ["AIHubMixEmbeddingClient", "OllamaEmbeddingClient", "SiliconFlowEmbeddingClient"]
