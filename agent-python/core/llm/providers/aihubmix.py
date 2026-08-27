"""AIHubMix provider：严格对齐 ragent infra-ai.infra.chat.AIHubMixChatClient。"""
from __future__ import annotations

import os

from core.llm.service import OpenAIStyleLLMService


class AIHubMixLLMService(OpenAIStyleLLMService):
    """严格对应 ragent infra-ai.infra.chat.AIHubMixChatClient。"""

    provider_id: str = "aihubmix"
    DEFAULT_BASE_URL: str = "https://aihubmix.com/v1"
    DEFAULT_MODEL: str = "gpt-4o-mini"

    def __init__(self, api_key: str | None = None, model: str | None = None, timeout: float = 60.0) -> None:
        super().__init__(
            base_url=self.DEFAULT_BASE_URL,
            api_key=api_key or os.getenv("AIHUBMIX_API_KEY"),
            model=model or os.getenv("AIHUBMIX_MODEL", self.DEFAULT_MODEL),
            timeout=timeout,
        )
