"""RoutingLLMService：严格对齐 ragent infra-ai.infra.chat.RoutingLLMService。

核心设计（与 ragent 一致）：
  - 5 字段：selector / healthStore / executor / firstPacketProbe / clientsByProvider
  - chat 3 overload（默认 / tier / preferredModelId）
  - streamChat(request, callback)：用 ProbeStreamBridge + 降级执行
  - 6 个 @RagTraceNode 注解方法（python 端用 AOP 装饰器实现，本版先做基础结构）

注：chat() 与 stream_chat() 默认同步接口（与 ragent OkHttp 同步调用一致）。
    OpenAIStyleLLMService 的 async chat/stream 是上层 LLMService 接口，路由内部用 sync 委派。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import httpx

from core.llm.enums import ModelCapability, Tier
from core.llm.model_target import ModelTarget
from core.llm.selector import ModelSelector
from core.llm.routing_executor import ModelRoutingExecutor
from core.llm.streaming import StreamCallback
from core.llm.errors import LLMError, LLMErrorType

log = logging.getLogger(__name__)


class RoutingLLMService:
    """严格对应 ragent infra-ai.infra.chat.RoutingLLMService。

    字段映射（ragent → python）：
      selector              → self._selector
      healthStore           → self._executor._health_store
      executor              → self._executor
      firstPacketProbe      → self._first_packet_probe（可选，简化版可空）
      clientsByProvider     → self._clients
    """

    def __init__(
        self,
        selector: ModelSelector,
        executor: ModelRoutingExecutor,
        chat_clients_by_provider: dict[str, Any],
        first_packet_probe: Any | None = None,
    ) -> None:
        self._selector = selector
        self._executor = executor
        self._clients = chat_clients_by_provider
        self._first_packet_probe = first_packet_probe

    # ==================== chat ====================

    def chat(self, request: dict) -> str:
        """默认档位 chat。"""
        return self.chat_with_tier(request, override=None, preferred_model_id=None)

    def chat_with_tier(
        self,
        request: dict,
        override: Tier | None = None,
        preferred_model_id: str | None = None,
    ) -> str:
        """严格对应 ragent chat(request, tier, preferredModelId)。"""
        thinking = bool(request.get("thinking", False))
        targets = self._selector.select_chat_candidates(thinking, override, preferred_model_id)
        return self._executor.execute_with_fallback(
            ModelCapability.CHAT,
            targets,
            client_resolver=self._resolve_client,
            caller=lambda client, target: client.do_chat(target, request),
        )

    # ==================== stream ====================

    def stream_chat(
        self,
        request: dict,
        callback: StreamCallback,
        override: Tier | None = None,
        preferred_model_id: str | None = None,
    ) -> Any:
        """严格对应 ragent streamChat(request, callback, tier, preferredModelId)。

        简化版实现（完整版需要 ProbeStreamBridge.await_first_packet + LlmFirstPacketProbe AOP）：
          - 遍历 targets → 跳过 client==null
          - health_store.allow_call → permit
          - 调 client.stream_chat(target, request, callback) → handle
          - 成功 → record_success + return handle
          - 失败 → record_failure + 继续
        """
        thinking = bool(request.get("thinking", False))
        targets = self._selector.select_chat_candidates(thinking, override, preferred_model_id)
        if not targets:
            raise LLMError("No CHAT model candidates available", LLMErrorType.CLIENT_ERROR)
        last_error: Exception | None = None
        health = self._executor._health_store
        for target in targets:
            client = self._resolve_client(target)
            if client is None:
                log.warning("Provider client missing: %s", target.id)
                continue
            permit = health.allow_call(target.id)
            if permit.half_open_token == 0 and health.is_unavailable(target.id):
                continue
            try:
                handle = client.stream_chat(target, request, callback)
                health.record_success(target.id, permit)
                return handle
            except Exception as e:  # noqa: BLE001
                last_error = e
                health.record_failure(target.id, permit, e)
                log.warning(
                    "Stream failed, fallback to next. modelId=%s, err=%s",
                    target.id, e,
                )
        raise LLMError(
            f"All CHAT model candidates failed: {last_error or 'unknown'}",
            error_type=LLMErrorType.SERVER_ERROR,
            cause=last_error,
        )

    # ==================== helpers ====================

    def _resolve_client(self, target: ModelTarget) -> Any | None:
        """严格对应 ragent resolveClient(target)：按 provider 选 client。"""
        return self._clients.get(target.candidate.provider.value)
