"""LLM 层枚举：严格对齐 ragent infra-ai.infra.enums.*。

3 个枚举（严格对齐 ragent）：
  - ModelCapability：CHAT / EMBEDDING / RERANK（displayName）
  - Tier：FAST("fast") / STANDARD("standard") / DEEP("deep")（档位 key）
  - ModelProvider：OLLAMA / BAI_LIAN / SILICON_FLOW / AI_HUB_MIX（provider key）
"""
from __future__ import annotations

from enum import Enum


class ModelCapability(str, Enum):
    """严格对应 ragent ModelCapability（displayName）。"""

    CHAT = "Chat"
    EMBEDDING = "Embedding"
    RERANK = "Rerank"


class Tier(str, Enum):
    """严格对应 ragent Tier（key）。

    档位表达"质量/成本/时延预算"，非业务任务本身。
    - FAST: 快速档，低延迟（标题/改写/摘要）
    - STANDARD: 标准档（默认）
    - DEEP: 深度档（thinking=true 触发）
    """

    FAST = "fast"
    STANDARD = "standard"
    DEEP = "deep"


class ModelProvider(str, Enum):
    """严格对应 ragent ModelProvider（key）。"""

    OLLAMA = "ollama"
    BAI_LIAN = "bailian"
    SILICON_FLOW = "siliconflow"
    AI_HUB_MIX = "aihubmix"
