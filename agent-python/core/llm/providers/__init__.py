"""LLM Provider 拆分：严格对齐 ragent infra-ai.infra.chat.*ChatClient（4 个）。

ragent 4 provider 全部继承 AbstractOpenAIStyleChatClient，仅重写 provider()：
  - SiliconFlowChatClient → provider_id = "siliconflow"
  - OllamaChatClient     → provider_id = "ollama", requiresApiKey = false
  - AIHubMixChatClient   → provider_id = "aihubmix"
  - BaiLianChatClient    → provider_id = "bailian"

Python 端：用 OpenAIStyleLLMService 作为抽象基类，4 个子类继承 + 自定义 provider_id/base_url/model。
"""
from core.llm.providers.siliconflow import SiliconFlowLLMService
from core.llm.providers.ollama import OllamaLLMService
from core.llm.providers.aihubmix import AIHubMixLLMService
from core.llm.providers.bailian import BaiLianLLMService

__all__ = [
    "SiliconFlowLLMService",
    "OllamaLLMService",
    "AIHubMixLLMService",
    "BaiLianLLMService",
]
