"""Prompt 构建场景枚举：严格对应 ragent rag.core.prompt.PromptScene。

四个场景：
- KB_ONLY：仅命中知识库检索，使用 KB 专用提示词
- MCP_ONLY：仅命中 MCP 工具调用，使用 MCP 专用提示词
- MIXED：同时命中 KB + MCP，使用混合提示词
- EMPTY：无任何检索命中
"""
from __future__ import annotations

from enum import Enum


class PromptScene(str, Enum):
    """严格对应 ragent rag.core.prompt.PromptScene。"""

    KB_ONLY = "KB_ONLY"
    MCP_ONLY = "MCP_ONLY"
    MIXED = "MIXED"
    EMPTY = "EMPTY"
