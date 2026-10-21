"""BaiLian (DashScope) provider：严格对齐 ragent infra-ai.infra.chat.BaiLianChatClient。"""
from __future__ import annotations

import os

from core.llm.service import OpenAIStyleLLMService


class BaiLianLLMService(OpenAIStyleLLMService):
    """严格对应 ragent infra-ai.infra.chat.BaiLianChatClient。"""

    provider_id: str = "bailian"
    DEFAULT_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    DEFAULT_MODEL: str = "qwen-turbo"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(
            base_url=self.DEFAULT_BASE_URL,
            api_key=api_key or os.getenv("BAILIAN_API_KEY") or os.getenv("DASHSCOPE_API_KEY"),
            model=model or os.getenv("BAILIAN_MODEL", self.DEFAULT_MODEL),
            timeout=timeout,
        )
