"""ModelSelector：严格对齐 ragent infra-ai.infra.model.ModelSelector。

核心算法（与 ragent 一致）：
  - chat 组走档位机制：thinking → deep_thinking_tier；否则 default_tier / override
  - embedding/rerank/vlm 组走 defaultModel + priority 排序
  - preferred 置顶（仅 chat）
  - 过滤：未启用、不支持 thinking（requireThinking）
"""
from __future__ import annotations

import logging
from typing import Any

from core.llm.enums import Tier
from core.llm.config import AIModelProperties, ModelCandidate, ModelGroup, ProviderConfig
from core.llm.model_target import ModelTarget

log = logging.getLogger(__name__)


class ModelSelector:
    """严格对应 ragent infra-ai.infra.model.ModelSelector。"""

    def __init__(self, properties: AIModelProperties, health_store: Any) -> None:
        self._props = properties
        self._health_store = health_store

    def select_chat_candidates(
        self,
        thinking: bool = False,
        override: Tier | None = None,
        preferred_model_id: str | None = None,
    ) -> list[ModelTarget]:
        """严格对应 ragent selectChatCandidates(thinking, override, preferredModelId)。"""
        group = self._props.chat
        if group is None:
            return []
        tier_name = self._resolve_tier_name(group, thinking, override)
        return self._build_tier_targets(group, tier_name, preferred_model_id, thinking)

    def select_embedding_candidates(self) -> list[ModelTarget]:
        """严格对应 ragent selectEmbeddingCandidates。"""
        return self._select_default_priority(self._props.embedding)

    def select_rerank_candidates(self) -> list[ModelTarget]:
        """严格对应 ragent selectRerankCandidates。"""
        return self._select_default_priority(self._props.rerank)

    def select_vlm_candidates(self) -> list[ModelTarget]:
        """严格对应 ragent selectVlmCandidates。"""
        return self._select_default_priority(self._props.vlm)

    # ==================== chat：档位机制 ====================

    def _resolve_tier_name(self, group: ModelGroup, thinking: bool, override: Tier | None) -> str:
        if thinking and group.deep_thinking_tier:
            return group.deep_thinking_tier
        if override is not None:
            return override.value
        return group.default_tier or "standard"

    def _build_tier_targets(
        self,
        group: ModelGroup,
        tier_name: str,
        preferred_model_id: str | None,
        require_thinking: bool,
    ) -> list[ModelTarget]:
        registry = {c.id: c for c in (group.candidates or [])}

        ordered_ids: list[str] = []
        if preferred_model_id:
            preferred = registry.get(preferred_model_id)
            if preferred is None:
                log.warning("Chat preferred 模型未在注册表登记，忽略并回退档位候选: %s", preferred_model_id)
            elif require_thinking and not preferred.supports_thinking:
                log.warning("Chat preferred 模型不支持思考，思考请求下忽略: %s", preferred_model_id)
            else:
                ordered_ids.append(preferred_model_id)

        tier = (group.tiers or {}).get(tier_name)
        timeout_ms = tier.timeout_ms if tier else None
        if tier is None:
            log.warning("Chat 档位配置缺失: tier=%s", tier_name)
        else:
            for cid in tier.candidates:
                if cid not in ordered_ids:
                    ordered_ids.append(cid)

        providers = self._props.providers
        targets: list[ModelTarget] = []
        for cid in ordered_ids:
            candidate = registry.get(cid)
            if candidate is None:
                log.warning("Chat 档位候选 id 未在注册表登记: id=%s, tier=%s", cid, tier_name)
                continue
            if not candidate.enabled:
                continue
            if require_thinking and not candidate.supports_thinking:
                continue
            target = self._build_model_target(candidate, providers, timeout_ms)
            if target is not None:
                targets.append(target)
        return targets

    def _build_model_target(
        self,
        candidate: ModelCandidate,
        providers: dict[str, ProviderConfig],
        timeout_ms: int | None,
    ) -> ModelTarget | None:
        provider = providers.get(candidate.provider.value)
        if provider is None:
            log.warning("Model candidate 缺少 provider 配置: %s", candidate.id)
            return None
        # candidate 覆盖 provider 字段
        effective_provider = ProviderConfig(
            base_url=candidate.base_url or provider.base_url,
            api_key=candidate.api_key or provider.api_key,
            extra=provider.extra,
        )
        return ModelTarget(
            id=candidate.id,
            candidate=candidate,
            provider=effective_provider,
            timeout_ms=timeout_ms,
        )

    # ==================== embedding/rerank/vlm：defaultModel + priority ====================

    def _select_default_priority(self, group: ModelGroup | None) -> list[ModelTarget]:
        if group is None or not group.candidates:
            return []
        ordered = self._filter_and_sort(group.candidates, group.default_model)
        targets: list[ModelTarget] = []
        for c in ordered:
            if not c.enabled:
                continue
            target = self._build_model_target(c, self._props.providers, None)
            if target is not None:
                targets.append(target)
        return targets

    def _filter_and_sort(
        self, candidates: list[ModelCandidate], default_model: str,
    ) -> list[ModelCandidate]:
        result = [c for c in candidates if c.enabled]
        result.sort(key=lambda c: (0 if c.id == default_model else 1, c.priority, c.id))
        return result
