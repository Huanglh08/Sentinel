"""检索层数据模型：对应 ragent-main rag/core/retrieval + framework.convention.RetrievedChunk。

核心语义（严格对齐 ragent java 版）：
  - RetrievedChunk：检索召回的片段（严格 7 字段对齐 java record：id/text/score/collectionName/docId/chunkIndex/docName）
  - BY_SCORE_DESC：静态倒序比较器（NaN/Inf 兜底 -inf，对应 java Float.NEGATIVE_INFINITY）
  - RetrievalBudget 三段式：recallBudget(每通道扇出) → candidateLimit(送 Rerank 上限) → contextTopK(进 LLM 条数)
  - SearchChannelResult：单通道结果（带通道类型/名称/耗时，供 RRF 按名次累计）

注：channel/source_type/file_type/url 是 python 端为了多通道归因和 SSE 来源面板补齐的承载字段，
Java 端对应信息由 SourcesAssembler 回表补齐。
"""
from __future__ import annotations


import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, ClassVar


class ChannelType(str, Enum):
    VECTOR = "VECTOR"
    KEYWORD = "KEYWORD"
    GRAPH = "GRAPH"
    WEB_SEARCH = "WEB_SEARCH"
    HYBRID = "HYBRID"


@dataclass
class RetrievedChunk:
    """严格对应 ragent framework.convention.RetrievedChunk record（7 字段）。

    字段（与 java record 一一对应）：
      id:            片段唯一标识（java: id，对应文档内的 chunk 唯一 key）
      text:          片段正文（java: text，对应 Chunk.content）
      score:         相关性分数（java: score，Float）
      collection_name: 集合名（java: collectionName）
      doc_id:        文档 id（java: docId）
      chunk_index:   文档内分块索引（java: chunkIndex，Integer）
      doc_name:      文档名（java: docName）

    python 扩展（java 端无）：
      channel:       多通道分类（用于 RRF 融合归因）
      source_type/file_type/url: 来源面板元数据（java 端由 SourcesAssembler 回表得到）
    """
    id: str
    text: str
    score: float = 0.0
    collection_name: str | None = None
    doc_id: str | None = None
    chunk_index: int | None = None
    doc_name: str | None = None
    channel: ChannelType | None = None
    source_type: str | None = None
    file_type: str | None = None
    url: str | None = None

    @staticmethod
    def _sort_score(score: float) -> float:
        """对应 ragent RetrievedChunk.sortScore：NaN/Inf 兜底 -inf（java: Float.NEGATIVE_INFINITY）。"""
        if not math.isfinite(score):
            return float("-inf")
        return score

    # 静态倒序比较器——对应 ragent RetrievedChunk.BY_SCORE_DESC
    # 算法：Float.compare(sortScore(b), sortScore(a))，把 NaN/Inf 视为最小
    BY_SCORE_DESC: ClassVar[Callable[["RetrievedChunk", "RetrievedChunk"], int]] = (
        lambda a, b: (
            -1 if RetrievedChunk._sort_score(b.score) < RetrievedChunk._sort_score(a.score) else
            1 if RetrievedChunk._sort_score(b.score) > RetrievedChunk._sort_score(a.score) else
            0
        )
    )

    def key(self) -> str:
        """1:1 对应 ragent framework.convention.RetrievedChunkKey.of：
        id 非空用 id，否则回退 sha256(text)（与 java DigestUtil.sha256Hex 一致）。"""
        if self.id and self.id.strip():
            return self.id
        import hashlib

        return hashlib.sha256((self.text or "").encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "score": round(self.score, 4),
            "collection_name": self.collection_name,
            "doc_id": self.doc_id,
            "chunk_index": self.chunk_index,
            "doc_name": self.doc_name,
            "channel": self.channel.value if self.channel else None,
            "source_type": self.source_type,
            "file_type": self.file_type,
            "url": self.url,
        }


@dataclass
class SearchChannelResult:
    channel_type: ChannelType
    channel_name: str
    chunks: list[RetrievedChunk]
    latency_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievalBudget:
    """三段式预算：recallBudget ≥ candidateLimit ≥ contextTopK（漏斗单调收窄）。"""
    recall_budget: int = 10
    candidate_limit: int = 20
    context_top_k: int = 6

    @classmethod
    def uniform(cls, k: int) -> "RetrievalBudget":
        return cls(k, k, k)


# 兼容旧调用方从模块级导入排序比较器。
BY_SCORE_DESC = RetrievedChunk.BY_SCORE_DESC


@dataclass
class SearchContext:
    """1:1 翻译 ragent rag.core.retrieval.channel.SearchContext。

    携带检索所需全部信息，在多个通道之间传递：
      - original_question / rewritten_question：原始问题与重写问题（main_question 优先取重写）
      - sub_questions：子问题列表
      - intents：意图识别结果（NodeScore 列表，供 derive_attribution 推导归属）
      - budget：检索预算（recallBudget / candidateLimit / contextTopK 三段）
      - retrieval_scope：检索作用域（RetrievalScope，定向/全局）
      - metadata：扩展元数据
    """

    original_question: str = ""
    rewritten_question: str = ""
    sub_questions: list[str] = field(default_factory=list)
    intents: list = field(default_factory=list)
    budget: "RetrievalBudget | None" = None
    retrieval_scope: Any | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def main_question(self) -> str:
        """1:1 对应 ragent SearchContext.getMainQuestion()：优先重写问题。"""
        return self.rewritten_question or self.original_question


@dataclass
class KnowledgeRetrievalResult:
    """1:1 翻译 ragent rag.core.retrieval.KnowledgeRetrievalResult。

    检索最终产物：
      - chunks：后处理后的 Chunk 列表
      - intent_ids_by_chunk_key：chunk key -> 归属意图 id 集合（按库推导）
      - directed_intent_ids：定向作用域下的命中意图 id 集合
    """

    chunks: list[RetrievedChunk] = field(default_factory=list)
    intent_ids_by_chunk_key: dict[str, set[str]] = field(default_factory=dict)
    directed_intent_ids: set[str] = field(default_factory=set)

    @classmethod
    def empty(cls) -> "KnowledgeRetrievalResult":
        return cls([], {}, set())

    def retrieved_intent_ids(self) -> frozenset[str]:
        """1:1 对应 ragent KnowledgeRetrievalResult.retrievedIntentIds()。"""
        ids: set[str] = set()
        for s in self.intent_ids_by_chunk_key.values():
            if s:
                ids.update(s)
        return frozenset(ids)

    def eligible_intent_ids(self, candidate_intents: list) -> list[str]:
        """1:1 对应 ragent KnowledgeRetrievalResult.eligibleIntentIds()。"""
        retrieved = self.retrieved_intent_ids()
        result: list[str] = []
        for ns in candidate_intents or []:
            node = getattr(ns, "node", None)
            if node is None or not getattr(node, "id", None):
                continue
            intent_id = node.id
            # 定向意图：未命中任何证据则不可选；非定向意图恒可选
            if intent_id not in self.directed_intent_ids or intent_id in retrieved:
                result.append(intent_id)
        return result

    def group_by_intent(self, global_key: str) -> dict[str, list[RetrievedChunk]]:
        """1:1 对应 ragent KnowledgeRetrievalResult.groupByIntent()。"""
        grouped: dict[str, list[RetrievedChunk]] = {}
        for chunk in self.chunks:
            ids = self.intent_ids_by_chunk_key.get(chunk.key())
            if not ids:
                grouped.setdefault(global_key, []).append(chunk)
                continue
            for intent_id in ids:
                grouped.setdefault(intent_id, []).append(chunk)
        return grouped
