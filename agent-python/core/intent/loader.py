"""意图树加载器：抽象接口 + InMemory / Redis 两种实现。

严格对齐 ragent IntentTreeServiceImpl.getFullTree 缓存策略：
  1. 先查 Redis 缓存（IntentTreeCacheManager.get）
  2. 缓存未命中 → 走 fallback（DB/InMemory）
  3. fallback 结果写回 Redis（IntentTreeCacheManager.save）
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from core.intent.types import IntentNode
from core.intent.factory import DEFAULT_INTENT_TREE


class IntentTreeLoader(ABC):
    """严格对应 ragent IntentTreeServiceImpl 加载逻辑（抽象）。"""

    @abstractmethod
    def load(self) -> list[IntentNode]:
        """加载全量意图节点列表。"""
        ...


class InMemoryIntentLoader(IntentTreeLoader):
    """从默认 DEFAULT_INTENT_TREE 加载——向后兼容旧 IntentResolver。"""

    def load(self) -> list[IntentNode]:
        return list(DEFAULT_INTENT_TREE)


class RedisBackedIntentLoader(IntentTreeLoader):
    """Redis 缓存优先——严格对齐 ragent IntentTreeServiceImpl.getFullTree。

    加载流程：
      1. cache.get() — Redis 命中直接返回
      2. cache 不存在 → fallback.load()（DB/InMemory）
      3. cache.save(roots) — 写回 Redis
    """

    def __init__(self, cache_manager: Any, fallback: IntentTreeLoader | None = None) -> None:
        self._cache = cache_manager
        self._fallback = fallback or InMemoryIntentLoader()

    def load(self) -> list[IntentNode]:
        # 1. 试 Redis
        cached = self._cache.get()
        if cached is not None:
            return [self._from_dict(d) for d in cached]
        # 2. 走 fallback
        nodes = self._fallback.load()
        # 3. 写回 Redis
        self._cache.save([n.to_dict() for n in nodes])
        return nodes

    def invalidate(self) -> None:
        """清除 Redis 缓存——对应 ragent clearIntentTreeCache。"""
        self._cache.clear()

    def _from_dict(self, d: dict) -> IntentNode:
        from core.intent.types import IntentKind, IntentLevel
        return IntentNode(
            id=d["id"],
            name=d["name"],
            description=d.get("description", ""),
            kind=IntentKind(d.get("kind", "KB")),
            level=IntentLevel(d.get("level", "TOPIC")),
            parent_id=d.get("parent_id"),
            kb_id=d.get("kb_id"),
            tool_id=d.get("tool_id"),
            collection_names=d.get("collection_names", []),
            examples=d.get("examples", []),
            top_k=d.get("top_k"),
            sort_order=d.get("sort_order", 0),
            enabled=d.get("enabled", True),
        )
