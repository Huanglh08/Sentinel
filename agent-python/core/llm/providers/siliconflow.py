"""SiliconFlow provider：严格对齐 ragent infra-ai.infra.chat.SiliconFlowChatClient。"""
from __future__ import annotations

import os

from core.llm.service import OpenAIStyleLLMService


class SiliconFlowLLMService(OpenAIStyleLLMService):
    """严格对应 ragent infra-ai.infra.chat.SiliconFlowChatClient。"""

    provider_id: str = "siliconflow"
    DEFAULT_BASE_URL: str = "https://api.siliconflow.cn/v1"
    DEFAULT_MODEL: str = "Qwen/Qwen2.5-7B-Instruct"

    def __init__(self, api_key: str | None = None, model: str | None = None, timeout: float = 60.0) -> None:
        super().__init__(
            base_url=self.DEFAULT_BASE_URL,
            api_key=api_key or os.getenv("SILICONFLOW_API_KEY"),
            model=model or os.getenv("SILICONFLOW_MODEL", self.DEFAULT_MODEL),
            timeout=timeout,
        )
