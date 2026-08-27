"""智能体提示词缓存管理器：1:1 翻译 ragent AgentPromptCacheManager。

缓存「激活智能体叠加自定义提示词」后的结果，命中即可直接取用；任何写操作后必须 clearCache。
优先写 Redis（与 Java 一致，key=ragent:agent:resolved-prompts，TTL=1h），Redis 不可用时回落进程内缓存。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

_logger = logging.getLogger("sentinel.agent_profile.cache")

_CACHE_KEY = "ragent:agent:resolved-prompts"
_CACHE_EXPIRE_SECONDS = 3600


class AgentPromptCacheManager:
    """1:1 翻译 ragent AgentPromptCacheManager。"""

    def __init__(self) -> None:
        self._local: dict[str, tuple[float, dict]] = {}
        self._local_lock = threading.Lock()
        self._redis = None

    @property
    def redis(self):
        if self._redis is None:
            try:
                import redis  # type: ignore

                self._redis = redis.from_url(
                    os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
                    decode_responses=True,
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning("Redis 不可用，提示词缓存回落进程内：%s", exc)
                self._redis = False
        return self._redis or None

    def get_from_cache(self) -> dict | None:
        try:
            r = self.redis
            if r is not None:
                raw = r.get(_CACHE_KEY)
                return json.loads(raw) if raw else None
        except Exception as exc:  # noqa: BLE001
            _logger.debug("从 Redis 读取提示词缓存失败：%s", exc)
        with self._local_lock:
            item = self._local.get(_CACHE_KEY)
            if item and item[0] > time.time():
                return dict(item[1])
            self._local.pop(_CACHE_KEY, None)
        return None

    def save_to_cache(self, prompts: dict) -> None:
        try:
            r = self.redis
            if r is not None:
                r.set(_CACHE_KEY, json.dumps(prompts), ex=_CACHE_EXPIRE_SECONDS)
        except Exception as exc:  # noqa: BLE001
            _logger.debug("保存提示词到 Redis 缓存失败：%s", exc)
        with self._local_lock:
            self._local[_CACHE_KEY] = (time.time() + _CACHE_EXPIRE_SECONDS, dict(prompts))

    def clear_cache(self) -> None:
        try:
            r = self.redis
            if r is not None:
                r.delete(_CACHE_KEY)
        except Exception as exc:  # noqa: BLE001
            _logger.debug("清除 Redis 提示词缓存失败：%s", exc)
        with self._local_lock:
            self._local.pop(_CACHE_KEY, None)
        _logger.info("智能体提示词缓存已清除")
