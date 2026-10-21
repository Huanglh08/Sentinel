"""ModelRoutingExecutor：严格对齐 ragent infra-ai.infra.model.ModelRoutingExecutor。

executeWithFallback 泛型方法（与 ragent 一致）：
  遍历 targets → 跳过 client==null → health_store.allow_call → caller.call
  → 成功 mark_success + return；失败 mark_failure + 继续
  → 全部失败抛 RemoteException
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, TypeVar

from core.llm.enums import ModelCapability
from core.llm.model_target import ModelTarget
from core.llm.errors import LLMError, LLMErrorType

log = logging.getLogger(__name__)

T = TypeVar("T")
C = TypeVar("C")


class ModelCaller(ABC):
    """严格对应 ragent ModelCaller interface（单方法接口）。"""

    @abstractmethod
    def call(self, client: C, target: ModelTarget) -> T: ...


class ModelRoutingExecutor:
    """严格对应 ragent ModelRoutingExecutor。"""

    def __init__(self, health_store: Any) -> None:
        self._health_store = health_store

    def execute_with_fallback(
        self,
        capability: ModelCapability,
        targets: list[ModelTarget],
        client_resolver: Callable[[ModelTarget], Any],
        caller: Callable[[Any, ModelTarget], T],
        on_success: Callable[[ModelTarget], None] | None = None,
    ) -> T:
        """严格对应 ragent executeWithFallback(capability, targets, clientResolver, caller)。

        on_success: 可选回调——某个 target 成功后被调用（用于粘性首选记忆）。
        """
        if not targets:
            raise LLMError(
                f"No {capability.value} model candidates available",
                error_type=LLMErrorType.CLIENT_ERROR,
            )
        last_error: Exception | None = None
        for target in targets:
            client = client_resolver(target)
            if client is None:
                log.warning(
                    "%s provider client missing: provider=%s, modelId=%s",
                    capability.value, target.candidate.provider.value, target.id,
                )
                continue
            permit = self._health_store.allow_call(target.id)
            if permit.half_open_token == 0 and self._health_store.is_unavailable(target.id):
                continue
            try:
                response = caller(client, target)
                self._health_store.record_success(target.id, permit)
                if on_success is not None:
                    on_success(target)
                return response
            except Exception as e:  # noqa: BLE001
                last_error = e
                self._health_store.record_failure(target.id, permit, e)
                log.warning(
                    "%s model failed, fallback to next. modelId=%s, provider=%s, err=%s",
                    capability.value, target.id, target.candidate.provider.value, e,
                )
        raise LLMError(
            f"All {capability.value} model candidates failed: {last_error or 'unknown'}",
            error_type=LLMErrorType.SERVER_ERROR,
            cause=last_error,
        )
