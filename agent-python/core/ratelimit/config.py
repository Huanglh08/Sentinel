"""限流配置：1:1 翻译 ragent rag.config.RAGRateLimitProperties。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class RAGRateLimitProperties:
    """1:1 翻译 ragent RAGRateLimitProperties。"""

    global_enabled: bool = True
    global_max_concurrent: int = 16
    global_lease_seconds: int = 120
    global_max_wait_seconds: int = 30
    global_poll_interval_ms: int = 200

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "RAGRateLimitProperties":
        env = env if env is not None else dict(os.environ)

        def _int(name: str, default: int) -> int:
            raw = env.get(name, "").strip()
            return int(raw) if raw else default

        def _bool(name: str, default: bool) -> bool:
            raw = env.get(name, "").strip().lower()
            return raw in ("1", "true", "yes", "on") if raw else default

        return cls(
            global_enabled=_bool("RAG_RATELIMIT_GLOBAL_ENABLED", True),
            global_max_concurrent=_int("RAG_RATELIMIT_GLOBAL_MAX_CONCURRENT", 16),
            global_lease_seconds=_int("RAG_RATELIMIT_GLOBAL_LEASE_SECONDS", 120),
            global_max_wait_seconds=_int("RAG_RATELIMIT_GLOBAL_MAX_WAIT_SECONDS", 30),
            global_poll_interval_ms=_int("RAG_RATELIMIT_GLOBAL_POLL_INTERVAL_MS", 200),
        )
