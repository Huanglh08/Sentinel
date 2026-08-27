"""记忆配置：1:1 翻译 ragent rag.config.MemoryProperties。

字段严格对齐 Java @Value 注入：
  - historyKeepTurns：保留的历史轮数（loadHistory 取 maxTurns*2 条消息）
  - summaryEnabled：是否启用对话摘要压缩
  - summaryStartTurns：触发摘要的最小用户消息数
  - summaryMaxChars：摘要最大字符数
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class MemoryProperties:
    """1:1 翻译 ragent MemoryProperties。"""

    history_keep_turns: int = 20
    summary_enabled: bool = True
    summary_start_turns: int = 10
    summary_max_chars: int = 2000

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "MemoryProperties":
        """1:1 翻译 ragent @Value("${rag.memory.*:default}") 注入机制（环境变量优先）。"""
        env = env if env is not None else dict(os.environ)

        def _int(name: str, default: int) -> int:
            raw = env.get(name, "").strip()
            return int(raw) if raw else default

        def _bool(name: str, default: bool) -> bool:
            raw = env.get(name, "").strip().lower()
            return raw in ("1", "true", "yes", "on") if raw else default

        return cls(
            history_keep_turns=_int("RAG_MEMORY_HISTORY_KEEP_TURNS", 20),
            summary_enabled=_bool("RAG_MEMORY_SUMMARY_ENABLED", True),
            summary_start_turns=_int("RAG_MEMORY_SUMMARY_START_TURNS", 10),
            summary_max_chars=_int("RAG_MEMORY_SUMMARY_MAX_CHARS", 2000),
        )
