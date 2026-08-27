"""Redis 缓存层：严格对应 ragent rag.core.intent.IntentTreeCacheManager。

关键设计（与 ragent 一致）：
  - CACHE_KEY = "ragent:intent:tree"（固定 key）
  - CACHE_EXPIRE_DAYS = 7（7 天 TTL）
  - get() / save() / clear() / exists() 4 个方法
  - 所有方法吞掉异常（避免缓存失败影响主流程）
"""
from __future__ import annotations

import json
from typing import Any


class IntentTreeCacheManager:
    """严格对应 ragent IntentTreeCacheManager（Redis 7 天 TTL）。"""

    CACHE_KEY = "ragent:intent:tree"
    CACHE_EXPIRE_DAYS = 7
    CACHE_EXPIRE_SECONDS = 7 * 86400

    def __init__(self, redis_client: Any | None = None) -> None:
        # 默认为 None（无 Redis），由调用方注入（与 ragent @RequiredArgsConstructor 风格一致）
        self._redis = redis_client

    def bind(self, redis_client: Any) -> None:
        """注入 Redis 客户端（运行时绑定，对应 ragent @Autowired 注入）。"""
        self._redis = redis_client

    def get(self) -> list[dict[str, Any]] | None:
        """严格对应 ragent getIntentTreeFromCache()：缓存不存在返 None。"""
        if self._redis is None:
            return None
        try:
            raw = self._redis.get(self.CACHE_KEY)
            if raw is None:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return json.loads(raw)
        except Exception:
            return None

    def save(self, roots: list[dict[str, Any]]) -> None:
        """严格对应 ragent saveIntentTreeToCache(roots)。"""
        if self._redis is None:
            return
        try:
            payload = json.dumps(roots, ensure_ascii=False)
            self._redis.setex(self.CACHE_KEY, self.CACHE_EXPIRE_SECONDS, payload)
        except Exception:
            pass

    def clear(self) -> None:
        """严格对应 ragent clearIntentTreeCache()：增删改时调用。"""
        if self._redis is None:
            return
        try:
            self._redis.delete(self.CACHE_KEY)
        except Exception:
            pass

    def exists(self) -> bool:
        """严格对应 ragent isCacheExists()。"""
        if self._redis is None:
            return False
        try:
            return bool(self._redis.exists(self.CACHE_KEY))
        except Exception:
            return False
