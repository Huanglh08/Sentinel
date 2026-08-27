"""1:1 翻译 ragent rag.core.intent.SubQuestionIntent。"""
from __future__ import annotations

from dataclasses import dataclass

from core.intent import NodeScore


@dataclass
class SubQuestionIntent:
    """1:1 翻译 ragent rag.core.intent.SubQuestionIntent。

    字段：
      question:    子问题原文
      node_scores: 该子问题分类出的意图打分（已过滤 < INTENT_MIN_SCORE，已截断到 MAX_INTENT_COUNT）
    """
    question: str
    node_scores: list[NodeScore]
