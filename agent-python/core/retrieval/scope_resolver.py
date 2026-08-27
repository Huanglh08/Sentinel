"""检索作用域：1:1 翻译 ragent rag.core.retrieval.channel.RetrievalScope + RetrievalScopeResolver。

核心语义（与 ragent RetrievalScopeResolver.resolve 严格一致）：
  - 提取 KB 意图（按 sub_intents 拍平，按 score >= min_intent_score 过滤，按 kind=KB 过滤，
    按 node.collection_names 非空过滤，按 node.id 去重保留最高分）
  - 计算 topScore
  - 若无 KB 意图 / topScore < confidenceThreshold / 意图绑定库与 active_collections 求交为空
    → 走 GLOBAL 模式
  - 否则走 INTENT 模式：targets = 意图绑定库 ∩ active_collections
  - supplement_collections = active_collections - targets
  - 永远填充 supplement_collections 供补路使用

意义：把"拍平所有意图 collection 当作全局检索域"换成"按子意图分批检索"，
     避免跨库混淆（每个 <content data-ragent-doc-id="..."> 只属于该子意图绑定的库）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from core.intent import NodeScore


# 1:1 翻译 ragent SearchChannelProperties.defaults
_DEFAULT_CONFIDENCE_THRESHOLD = 0.5
_DEFAULT_MIN_INTENT_SCORE = 0.3


@dataclass
class RetrievalScope:
    """1:1 翻译 ragent rag.core.retrieval.channel.RetrievalScope。

    字段：
      mode:                   GLOBAL | INTENT
      collections:            本次检索的定向库（INTENT 模式 = 意图绑定 ∩ active；GLOBAL 模式 = active）
      supplement_collections: 补路库（active - collections），供向量通道并行补路
      top_score:              触发本次决议的 KB 意图最高分（可观测）
      node_ids:               参与本次决议的意图 ID（去重）
    """
    mode: str  # "GLOBAL" | "INTENT"
    collections: list[str] = field(default_factory=list)
    supplement_collections: list[str] = field(default_factory=list)
    top_score: float = 0.0
    node_ids: list[str] = field(default_factory=list)

    @property
    def is_global(self) -> bool:
        return self.mode == "GLOBAL"

    @property
    def is_intent(self) -> bool:
        return self.mode == "INTENT"


class RetrievalScopeResolver:
    """1:1 翻译 ragent rag.core.retrieval.channel.RetrievalScopeResolver。

    参数：
      active_collections:     系统当前启用的 collection 列表（替代 ragent KbCollectionProvider.listActiveCollections()）
      confidence_threshold:    KB 意图最高分低于该值 → 走全局（默认 0.5）
      min_intent_score:        低于该分的 KB 意图不参与决议（默认 0.3，与 ragent INTENT_MIN_SCORE 一致）
    """

    def __init__(
        self,
        active_collections: list[str] | None = None,
        confidence_threshold: float = _DEFAULT_CONFIDENCE_THRESHOLD,
        min_intent_score: float = _DEFAULT_MIN_INTENT_SCORE,
    ) -> None:
        self.active_collections: list[str] = list(active_collections or [])
        self.confidence_threshold = confidence_threshold
        self.min_intent_score = min_intent_score

    # ---- 顶层入口：严格对应 ragent RetrievalScopeResolver.resolve ----
    def resolve(self, sub_intents: list[tuple[str, list[NodeScore]]]) -> RetrievalScope:
        """sub_intents: [(sub_question, [NodeScore, ...]), ...]
        严格对应 ragent RetrievalScopeResolver.resolve(List<SubQuestionIntent>)。
        """
        kb_intents = self._extract_kb_intents(sub_intents)
        top_score = max((s.score for s in kb_intents), default=0.0)
        active = self.active_collections

        if not kb_intents:
            return RetrievalScope(
                mode="GLOBAL", collections=list(active), supplement_collections=[],
                top_score=top_score, node_ids=[],
            )
        if top_score < self.confidence_threshold:
            return RetrievalScope(
                mode="GLOBAL", collections=list(active), supplement_collections=[],
                top_score=top_score,
                node_ids=[s.node.id for s in kb_intents if s.node and s.node.id],
            )

        # 意图绑定库去重保序
        bound: list[str] = []
        for s in kb_intents:
            if not s.node:
                continue
            for c in (s.node.collection_names or []):
                if c and c not in bound:
                    bound.append(c)

        # 与 active 求交（保交集合的顺序，按 active 的顺序）
        active_set = set(active)
        targets: list[str] = [c for c in active if c in set(bound)]
        if not targets:
            return RetrievalScope(
                mode="GLOBAL", collections=list(active), supplement_collections=[],
                top_score=top_score,
                node_ids=[s.node.id for s in kb_intents if s.node and s.node.id],
            )

        supplement = [c for c in active if c not in set(targets)]
        return RetrievalScope(
            mode="INTENT",
            collections=targets,
            supplement_collections=supplement,
            top_score=top_score,
            node_ids=[s.node.id for s in kb_intents if s.node and s.node.id],
        )

    # ---- 提取 KB 意图：1:1 翻译 ragent extractKbIntents ----
    def _extract_kb_intents(
        self,
        sub_intents: Iterable[tuple[str, list[NodeScore]]],
    ) -> list[NodeScore]:
        # flat + min_score + KB 过滤
        all_scores: list[NodeScore] = []
        for _, scores in sub_intents:
            all_scores.extend(scores or [])
        filtered = [
            s for s in all_scores
            if (s.score or 0) >= self.min_intent_score
            and s.node
            and (s.node.kind or "KB") == "KB"  # 与 ragent isKB() 一致
            and (s.node.collection_names or [])
        ]
        # 按 node.id 去重，保留最高分
        best_by_id: dict[str, NodeScore] = {}
        for s in filtered:
            sid = s.node.id if s.node else ""
            if not sid:
                continue
            if sid not in best_by_id or (s.score or 0) > (best_by_id[sid].score or 0):
                best_by_id[sid] = s
        return list(best_by_id.values())

    # ---- 顶层入口2：按子查询分别解析（修跨库混淆的入口） ----
    def per_sub_query(
        self,
        sub_query_to_scores: list[tuple[str, list[NodeScore]]],
    ) -> list[tuple[str, RetrievalScope]]:
        """严格对应 ragent RetrievalService 中的 sq-by-sq 解析：
        对每个子查询单独调用 resolve。
        注：ragent 走的是"先 resolve 一次，再 fan-out"；Python 端为简化做 per_sub_query，
        与 ragent 顶层效果一致。
        """
        return [(sq, self.resolve([(sq, scores)])) for sq, scores in sub_query_to_scores]
