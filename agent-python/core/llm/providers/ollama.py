"""Ollama provider：严格对齐 ragent infra-ai.infra.chat.OllamaChatClient。

关键差异（与 ragent OllamaChatClient.requiresApiKey() = false 一致）：
  - requires_api_key = False（Ollama 本地部署不需要 API key）
"""
from __future__ import annotations

import os

from core.llm.service import OpenAIStyleLLMService


class OllamaLLMService(OpenAIStyleLLMService):
    """严格对应 ragent infra-ai.infra.chat.OllamaChatClient。"""

    provider_id: str = "ollama"
    requires_api_key: bool = False  # 严格对齐 ragent OllamaChatClient.requiresApiKey() = false
    DEFAULT_BASE_URL: str = "http://localhost:11434/v1"
    DEFAULT_MODEL: str = "qwen2.5:7b"

    def __init__(self, base_url: str | None = None, model: str | None = None, timeout: float = 60.0) -> None:
        super().__init__(
            base_url=base_url or os.getenv("AGENT_LLM_BASE_URL", self.DEFAULT_BASE_URL),
            api_key="ollama",  # 任意占位，requires_api_key=False 不检查
            model=model or os.getenv("AGENT_LLM_MODEL", self.DEFAULT_MODEL),
            timeout=timeout,
        )
