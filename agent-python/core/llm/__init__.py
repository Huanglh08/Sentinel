"""LLM 抽象层：严格对齐 ragent-main infra-ai.infra.chat.* 与 infra-ai.infra.model.*。

包结构：
  - errors.py：LLMError / LLMErrorType（7 枚举 + from_http_status）
  - types.py：ChatRequest / ChatResponse
  - service.py：LLMService 接口（4 方法） + OpenAIStyleLLMService 抽象实现
  - enums.py：ModelCapability / Tier / ModelProvider（3 枚举）
  - config.py：AIModelProperties + ModelGroup + ModelCandidate + ProviderConfig + TierConfig
  - model_target.py：ModelTarget record（4 字段）
  - selector.py：ModelSelector（chat 走档位 + embedding 走 priority）
  - routing_executor.py：ModelRoutingExecutor（execute_with_fallback 降级）
  - routing_service.py：RoutingLLMService（多 provider 路由入口）
  - providers/：4 provider 子类（SiliconFlow / Ollama / AIHubMix / BaiLian）
  - streaming/：SSE 解析 + 首包探测 + 异步执行 + 取消句柄 + 回调追踪

向后兼容：LLMClient 旧名 = OpenAIStyleLLMService。
"""
from core.llm.errors import LLMError, LLMErrorType
from core.llm.service import AsyncRoutingLLMService, LLMClient, LLMService, OpenAIStyleLLMService
from core.llm.streaming import OpenAIStyleSseParser, ProbeStreamBridge
from core.llm.providers import (
    SiliconFlowLLMService, OllamaLLMService,
    AIHubMixLLMService, BaiLianLLMService,
)
# 路由层
from core.llm.enums import ModelCapability, Tier, ModelProvider
from core.llm.config import (
    AIModelProperties, ModelGroup, ModelCandidate, ProviderConfig, TierConfig,
)
from core.llm.model_target import ModelTarget
from core.llm.selector import ModelSelector
from core.llm.routing_executor import ModelRoutingExecutor, ModelCaller
from core.llm.routing_service import RoutingLLMService

# 向后兼容：旧 LLMClient 名字 = 新 OpenAIStyleLLMService
__all__ = [
    # 基础
    "LLMError", "LLMErrorType",
    "LLMClient", "LLMService", "OpenAIStyleLLMService", "AsyncRoutingLLMService",
    "OpenAIStyleSseParser", "ProbeStreamBridge",
    # 4 provider
    "SiliconFlowLLMService", "OllamaLLMService",
    "AIHubMixLLMService", "BaiLianLLMService",
    # 路由层
    "ModelCapability", "Tier", "ModelProvider",
    "AIModelProperties", "ModelGroup", "ModelCandidate", "ProviderConfig", "TierConfig",
    "ModelTarget", "ModelSelector", "ModelRoutingExecutor", "ModelCaller",
    "RoutingLLMService",
]
