"""多通道检索引擎：1:1 翻译 ragent-main MultiChannelRetrievalEngine + 后置处理器链。

核心算法（严格对齐 ragent Java 版）：
  1. 并行执行所有启用的检索通道（asyncio.gather），单通道超时/异常 -> 降级为空结果，不中断融合
     —— 对应 ragent executeSearchChannels + withTimeout（orTimeout + exceptionally 降级 emptyResult）
  2. 后置处理器链（按 order 顺序执行，对应 ragent executePostProcessors）：
       - Deduplication（order=1，常开）：按 key 去重，同 key 保留首次出现（不比较分数）
       - Fusion/RRF（order=5，strategy=rrf 时启用）：score = Σ_channel weight_c / (k + rank_c)，
         按通道原始名次累计；融合后按 candidateLimit 截断候选池
       - Rerank（order=10，rerank_enabled 时启用）：精排输出 contextTopK
       - MetadataEnrichment（order=20，context_enrich_enabled 时启用）：按 chunkId/docId 回表补 docId/chunkIndex/docName
  3. 归因推导（deriveAttribution）：定向作用域下，存活 chunk 的 collection 命中某意图绑定库即归属该意图
     —— 对应 ragent deriveAttribution + KnowledgeRetrievalResult

与 Java 版的对应说明：
  - Java 用 CompletableFuture + 线程池 + orTimeout；Python 用 asyncio.gather + asyncio.wait_for，语义一致。
  - Java 的 Rerank 是同步 RerankService；Python 抽象为异步 Reranker 接口（见 rerank.py）。
  - Java 的 ChunkMetadataResolver 回表补齐；Python 端 pgvector 在召回时即填充 docId/chunkIndex/docName，
    故 MetadataEnrichmentPostProcessor 在 metadata_resolver 为空时为空操作（结构对齐，不重复回表）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from core.retrieval.scope_resolver import RetrievalScope
from core.retrieval.rerank import SiliconFlowReranker
from core.retrieval.types import (
    ChannelType,
    KnowledgeRetrievalResult,
    RetrievedChunk,
    RetrievalBudget,
    SearchChannelResult,
    SearchContext,
)

_logger = logging.getLogger("sentinel.retrieval.engine")

# 通道类型枚举序（对应 ragent SearchChannelType.ordinal()）：用于稳定派发/日志顺序
_CHANNEL_ORDER: dict[ChannelType, int] = {t: i for i, t in enumerate(ChannelType)}

# 1:1 翻译 ragent GraphSearchChannel.FILTER_TOPK_BOOST：定向过滤时向 LightRAG 多取以补召回
_GRAPH_DIRECTED_TOPK_BOOST = 3


def _merge_by_score(primary: list[RetrievedChunk], supplement: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """1:1 翻译 ragent ChunkRanking.mergeByScore：合并主路/补充路并按相关性降序。

    两路分数同源，必须按分数混排而非主路在前补充路拼在后（否则补充路的强命中恒排在主路
    弱命中之后，RRF 按名次取分等于名额给了、排序又把它按回去）。NaN/Inf 经
    RetrievedChunk._sort_score 归一为 -inf，与 ragent BY_SCORE_DESC 一致。
    """
    if not supplement:
        merged = list(primary)
    else:
        merged = list(primary) + list(supplement)
    merged.sort(key=lambda c: RetrievedChunk._sort_score(c.score), reverse=True)
    return merged


# ---------------------------------------------------------------------------
# 检索上下文与结果类型已在 core.retrieval.types 中 1:1 定义（SearchContext / KnowledgeRetrievalResult）
# ---------------------------------------------------------------------------


@dataclass
class ChannelConfig:
    enabled: bool = True
    weight: float = 1.0                 # RRF 贡献权重（1:1 翻译 ragent Fusion.ChannelWeights）


@dataclass
class RetrievalConfig:
    rrf_k: int = 60
    channels: dict[ChannelType, ChannelConfig] = field(default_factory=dict)
    rerank_enabled: bool = False
    # 1:1 翻译 ragent SearchChannelProperties.Fusion.strategy（"rrf" 启用融合，其它关闭）
    fusion_strategy: str = "rrf"
    # 1:1 翻译 ragent RAGConfigProperties.getContextEnrichEnabled()
    context_enrich_enabled: bool = True
    # 1:1 翻译 ragent SearchChannelProperties.Scope.supplementRatio：划给补充路的份额
    supplement_ratio: float = 0.25
    # 1:1 翻译 ragent SearchChannelProperties.Channels.timeoutMs：单一全局通道级超时
    # （Java 不按通道各自配超时，所有通道共用这一个值；<=0 表示不另设超时）
    channel_timeout_ms: float = 15_000.0
    # 双模型 rerank 相关性门控（P1 负样本治理；对应 Java rag.retrieval-gate.*）
    gate_enabled: bool = False
    gate_bge_threshold: float = 0.10
    gate_qwen3_threshold: float = 0.20
    gate_top_n: int = 8

    @classmethod
    def default(cls) -> "RetrievalConfig":
        return cls(
            rrf_k=60,
            channels={
                ChannelType.VECTOR: ChannelConfig(enabled=True, weight=1.0),
                ChannelType.KEYWORD: ChannelConfig(enabled=True, weight=0.8),
            },
            rerank_enabled=False,
            supplement_ratio=0.25,
            channel_timeout_ms=15_000.0,
        )


# ---------------------------------------------------------------------------
# 后置处理器契约与实现（1:1 翻译 ragent SearchResultPostProcessor 及其四个实现）
# ---------------------------------------------------------------------------


class SearchResultPostProcessor(Protocol):
    """1:1 翻译 ragent SearchResultPostProcessor 接口。"""

    name: str
    order: int

    def is_enabled(self, context: SearchContext) -> bool:
        ...

    async def process(
        self,
        chunks: list[RetrievedChunk],
        results: list[SearchChannelResult],
        context: SearchContext,
    ) -> list[RetrievedChunk]:
        ...


class DeduplicationPostProcessor:
    """1:1 翻译 ragent DeduplicationPostProcessor（order=1，常开）。

    合并多通道结果并按 key 去重，同一 Chunk 多路命中时保留首次出现的实例
    （不比较跨通道原始分数，最终名次由下游 RRF 融合赋分）。
    注意：去重遍历的是原始 results（而非输入 chunks），与 ragent 一致。
    """

    name = "Deduplication"
    order = 1

    def is_enabled(self, context: SearchContext) -> bool:
        return True

    async def process(
        self,
        chunks: list[RetrievedChunk],
        results: list[SearchChannelResult],
        context: SearchContext,
    ) -> list[RetrievedChunk]:
        seen: dict[str, RetrievedChunk] = {}
        for result in results:
            for chunk in result.chunks:
                # putIfAbsent：首次出现胜出
                seen.setdefault(chunk.key(), chunk)
        return list(seen.values())


class FusionPostProcessor:
    """1:1 翻译 ragent FusionPostProcessor（order=5，strategy=rrf 时启用）。

    RRF 融合：score(chunk) = Σ_channel weight_channel / (k + rank_channel)
    名次取自不可变的 SearchChannelResult 列表（每个通道的原始召回顺序）。
    融合排序后按 candidateLimit 截断候选池，仅把高分前 N 个送入下游 Rerank。
    """

    name = "Fusion"
    order = 5

    def __init__(self, config: RetrievalConfig) -> None:
        self.config = config

    def is_enabled(self, context: SearchContext) -> bool:
        return self.config.fusion_strategy.lower() == "rrf"

    async def process(
        self,
        chunks: list[RetrievedChunk],
        results: list[SearchChannelResult],
        context: SearchContext,
    ) -> list[RetrievedChunk]:
        if not chunks:
            return chunks
        # 多通道才做 RRF 融合重排；单通道保持原召回顺序
        ranked = self._fuse_rrf(chunks, results) if (results and len(results) > 1) else chunks
        return self._truncate_for_rerank(ranked, results, context.budget.candidate_limit)

    def _fuse_rrf(
        self, chunks: list[RetrievedChunk], results: list[SearchChannelResult]
    ) -> list[RetrievedChunk]:
        k = self.config.rrf_k
        rrf: dict[str, float] = {}
        for result in results:
            weight = self._weight_of(result.channel_type)
            for rank, chunk in enumerate(result.chunks):
                rrf[chunk.key()] = rrf.get(chunk.key(), 0.0) + weight / (k + rank + 1)

        fused = list(chunks)
        for chunk in fused:
            chunk.score = rrf.get(chunk.key(), 0.0)
        fused.sort(key=lambda c: c.score, reverse=True)
        return fused

    def _weight_of(self, ctype: ChannelType) -> float:
        """1:1 翻译 ragent FusionPostProcessor.weightOf：各通道 RRF 贡献权重。"""
        cfg = self.config.channels.get(ctype)
        if cfg is not None:
            return cfg.weight
        return 1.0

    def _truncate_for_rerank(
        self,
        ranked: list[RetrievedChunk],
        results: list[SearchChannelResult],
        limit: int,
    ) -> list[RetrievedChunk]:
        """1:1 翻译 ragent FusionPostProcessor.truncateForRerank：按候选池上限截断。"""
        truncate = limit and limit > 0 and len(ranked) > limit
        candidates = ranked[:limit] if truncate else ranked

        channel_count = len(results) if results else 0
        _logger.info(
            "RRF 融合完成 - 通道数: %d, k: %d, 融合后: %d 个, 截断上限: %s, 送入 Rerank: %d 个",
            channel_count,
            self.config.rrf_k,
            len(ranked),
            str(limit) if limit and limit > 0 else "不限",
            len(candidates),
        )

        # 截断是弱势通道证据消失的第一现场：池内与出池分布并排打，供可观测
        if channel_count > 1:
            index = ChannelAttribution.index(results)
            _logger.info(
                "检索归因 - 融合池按通道: %s, 送入 Rerank 按通道: %s",
                ChannelAttribution.format(ChannelAttribution.count_by_channel(ranked, index)),
                ChannelAttribution.format(ChannelAttribution.count_by_channel(candidates, index)),
            )
        return candidates


class RerankPostProcessor:
    """1:1 翻译 ragent RerankPostProcessor（order=10，rerank_enabled 时启用）。

    使用 Reranker 对结果重排序，输出最终 Top-K（contextTopK）。
    打印归因日志：对比 Rerank 前后各通道候选数，重点「图谱证据存活率」。
    """

    name = "Rerank"
    order = 10

    def __init__(self, reranker: Any | None, config: RetrievalConfig) -> None:
        self.reranker = reranker
        self.config = config

    def is_enabled(self, context: SearchContext) -> bool:
        return self.config.rerank_enabled and self.reranker is not None

    async def process(
        self,
        chunks: list[RetrievedChunk],
        results: list[SearchChannelResult],
        context: SearchContext,
    ) -> list[RetrievedChunk]:
        if not chunks:
            _logger.info("Chunk 列表为空，跳过 Rerank")
            return chunks

        reranked = await self.reranker.rerank(
            context.main_question, chunks, context.budget.context_top_k
        )
        self._log_attribution(chunks, reranked, results)
        return reranked

    def _log_attribution(
        self,
        before: list[RetrievedChunk],
        after: list[RetrievedChunk],
        results: list[SearchChannelResult],
    ) -> None:
        """1:1 翻译 ragent RerankPostProcessor.logAttribution。"""
        if not results or len(results) <= 1:
            return
        index = ChannelAttribution.index(results)
        _logger.info(
            "检索归因 - Rerank 输入按通道: %s, 输出 top%d 按通道: %s",
            ChannelAttribution.format(ChannelAttribution.count_by_channel(before, index)),
            len(after),
            ChannelAttribution.format(ChannelAttribution.count_by_channel(after, index)),
        )

        # 按图谱通道在场判断而非 graphIn > 0：0/0 恰是最需要看见的形态
        graph_present = any(r.channel_type == ChannelType.GRAPH for r in results)
        if graph_present:
            graph_in = ChannelAttribution.count_of_channel(before, index, ChannelType.GRAPH)
            graph_out = ChannelAttribution.count_of_channel(after, index, ChannelType.GRAPH)
            _logger.info("检索归因 - 图谱证据存活: %d/%d", graph_out, graph_in)


class RelevanceGatePostProcessor:
    """双模型 rerank 相关性门控（order=15，Rerank 之后、MetadataEnrichment 之前）。

    负样本治理：问题与知识库无关时，检索/精排仍会返回"高分垃圾"chunk，下游
    基于垃圾上下文作答即幻觉。本处理器用 bge-reranker-v2-m3 × Qwen3-Reranker-0.6B
    双模型对精排后的最终 chunk 打分，AND 门控（两个模型的 top1 同时低于阈值）
    才判定"库中大概率无相关信息"，返回空列表 → 上层走"库中无此信息"拒答。

    bge 分数复用与 qwen3 惰性确认（P2 延迟优化，AND 门控语义不变）：
      - Rerank 阶段（order=10）已用同一 bge 模型对同一批 chunk 打过分，gate 直接
        复用 chunk.score 作为 bge_top1，省一次重复 API 调用
      - AND 语义下 bge_top1 达阈值即确定放行，仅 bge 低置信（可能切空）时才调
        qwen3 交叉确认 —— 正常请求零 gate API 调用
      - 复用前提：Rerank 实际由 SiliconFlowReranker（bge）执行。RoutingReranker
        降级到 LLMReranker 或全部失败时，chunk.score 是 LLM 分/RRF 融合分，
        语义不符，回退到 gate 双模型自打分的原路径
    - 打分对象与离线校准（eval/rerank_gate_eval.py）一致：Rerank 后的最终 chunk
    - 打分是同步 GPU/CPU 密集任务，包 asyncio.to_thread 避免阻塞事件循环
    - fail-open：gate 为 None / 抛异常 / 打分失败 → 原样放行，绝不因门控故障误拒
    """

    name = "RelevanceGate"
    order = 15

    def __init__(self, gate: Any | None, config: RetrievalConfig, reranker: Any | None = None) -> None:
        self.gate = gate
        self.config = config
        self.reranker = reranker

    def is_enabled(self, context: SearchContext) -> bool:
        return self.config.gate_enabled and self.gate is not None

    def _reusable_bge_top1(self, chunks: list[RetrievedChunk]) -> float | None:
        """chunks[0].score 可作 bge_top1 时返回该值，否则 None（回退双模型路径）。

        前提：Rerank 由 SiliconFlowReranker 实际执行（RoutingReranker.last_used
        记录实际成功者；非 Routing 包装时注入即 SiliconFlow 本体）。
        """
        rr = self.reranker
        if rr is None or not chunks:
            return None
        effective = getattr(rr, "last_used", rr)
        if not isinstance(effective, SiliconFlowReranker):
            return None
        return float(chunks[0].score)

    async def process(
        self,
        chunks: list[RetrievedChunk],
        results: list[SearchChannelResult],
        context: SearchContext,
    ) -> list[RetrievedChunk]:
        if not chunks:
            return chunks
        texts = [c.text for c in chunks]
        bge_top1 = self._reusable_bge_top1(chunks)
        gate_result = await asyncio.to_thread(
            self.gate.gate,
            context.main_question,
            texts,
            self.config.gate_bge_threshold,
            self.config.gate_qwen3_threshold,
            self.config.gate_top_n,
            bge_scores=([bge_top1] if bge_top1 is not None else None),
        )
        if gate_result.error:
            _logger.warning(
                "相关性门控 fail-open（%s），放行 %d 条 chunk", gate_result.error, len(chunks)
            )
            return chunks
        qwen3_disp = (
            "skipped(惰性)" if gate_result.qwen3_top1 < 0 else f"{gate_result.qwen3_top1:.4f}"
        )
        _logger.info(
            "相关性门控 - bge_top1=%.4f qwen3_top1=%s 阈值=(%.2f, %.2f) 触发=%s scored=%d "
            "bge来源=%s",
            gate_result.bge_top1, qwen3_disp,
            self.config.gate_bge_threshold, self.config.gate_qwen3_threshold,
            gate_result.triggered, gate_result.scored,
            "复用rerank" if bge_top1 is not None else "独立打分",
        )
        if gate_result.triggered:
            _logger.warning(
                "相关性门控触发，切空 %d 条 chunk，走无知识拒答（题目: %s）",
                len(chunks), context.main_question[:50],
            )
            return []
        return chunks




class MetadataEnrichmentPostProcessor:
    """1:1 翻译 ragent MetadataEnrichmentPostProcessor（order=20，context_enrich_enabled 时启用）。

    处于处理链末端（Rerank 之后），对最终 Top-K 结果按 chunkId 回表补齐文档归属信息
    （文档ID、文档内序号、文档标题），供上下文组装时按文档聚合与标注来源。
    只富化、不重排：保持进入时的相关性顺序不变。

    Python 端 pgvector 在召回时即填充 docId/chunkIndex/docName，故 metadata_resolver 为空时
    本处理器为空操作（结构对齐，不重复回表）；传入 resolver 时按 ragent 语义回表补齐。
    """

    name = "MetadataEnrichment"
    order = 20

    def __init__(self, metadata_resolver: Any | None = None, config: RetrievalConfig | None = None) -> None:
        self.metadata_resolver = metadata_resolver
        self.config = config

    def is_enabled(self, context: SearchContext) -> bool:
        if self.config is not None:
            return self.config.context_enrich_enabled
        return self.metadata_resolver is not None

    async def process(
        self,
        chunks: list[RetrievedChunk],
        results: list[SearchChannelResult],
        context: SearchContext,
    ) -> list[RetrievedChunk]:
        if not chunks:
            return chunks
        if self.metadata_resolver is None:
            return chunks

        chunk_ids = [c.id for c in chunks]
        meta_by_id = self.metadata_resolver.resolve(chunk_ids) or {}

        # 1）按 chunkId 富化：向量/关键词证据的 chunk.id 即主键，回表补齐 docId/序号/标题
        for chunk in chunks:
            meta = meta_by_id.get(chunk.id)
            if not meta:
                continue
            chunk.doc_id = meta.get("doc_id") or meta.get("docId") or chunk.doc_id
            if meta.get("chunk_index") is not None:
                chunk.chunk_index = meta.get("chunk_index")
            chunk.doc_name = meta.get("doc_name") or meta.get("docName") or chunk.doc_name

        # 2）按 docId 补标题：图谱等证据 chunk.id 非主键、上一步未命中，但已带归属 docId，据此补真实标题
        self._fill_doc_names_by_doc_id(chunks)
        return chunks

    def _fill_doc_names_by_doc_id(self, chunks: list[RetrievedChunk]) -> None:
        pending = [c.doc_id for c in chunks if (not c.doc_name) and c.doc_id]
        if not pending:
            return
        doc_name_by_id = self.metadata_resolver.resolve_doc_names(pending) or {}
        if not doc_name_by_id:
            return
        for chunk in chunks:
            if (not chunk.doc_name) and chunk.doc_id and chunk.doc_id in doc_name_by_id:
                chunk.doc_name = doc_name_by_id[chunk.doc_id]


# ---------------------------------------------------------------------------
# 检索通道归因工具（1:1 翻译 ragent ChannelAttribution）
# ---------------------------------------------------------------------------


class ChannelAttribution:
    """1:1 翻译 ragent ChannelAttribution。

    RetrievedChunk 不携带来源通道字段（框架层 DTO 保持纯净），故按 chunk key 从不可变的
    SearchChannelResult 反查每条证据来自哪些通道，供融合/Rerank 打印「各通道贡献/存活率」日志。
    归因键统一由 RetrievedChunk.key() 生成，避免去重、融合、归因采用不同身份规则。
    """

    @staticmethod
    def index(results: list[SearchChannelResult]) -> dict[str, set[ChannelType]]:
        """反查每个 chunk key 命中的通道集合（一条证据可被多路命中，故值为集合）。"""
        idx: dict[str, set[ChannelType]] = {}
        if not results:
            return idx
        for result in results:
            if result is None or not result.chunks:
                continue
            for chunk in result.chunks:
                key = chunk.key()
                s = idx.get(key)
                if s is None:
                    s = set()
                    idx[key] = s
                s.add(result.channel_type)
        return idx

    @staticmethod
    def count_by_channel(
        chunks: list[RetrievedChunk], index: dict[str, set[ChannelType]]
    ) -> dict[ChannelType, int]:
        """统计给定 chunks 按通道的分布（多路命中的 chunk 在每个命中通道各计一次）。"""
        counts: dict[ChannelType, int] = {}
        for chunk in chunks:
            channels = index.get(chunk.key())
            if not channels:
                continue
            for channel in channels:
                counts[channel] = counts.get(channel, 0) + 1
        return counts

    @staticmethod
    def count_of_channel(
        chunks: list[RetrievedChunk], index: dict[str, set[ChannelType]], channel: ChannelType
    ) -> int:
        """命中给定通道的 chunk 数（用于「图谱存活率」这类单通道口径前后对比）。"""
        return sum(1 for chunk in chunks if channel in (index.get(chunk.key()) or set()))

    @staticmethod
    def format(counts: dict[ChannelType, int]) -> str:
        """通道分布转中文可读串，如「向量=4 图谱=8 关键词=6」。"""
        if not counts:
            return "无"
        return " ".join(f"{ChannelAttribution.label(t)}={n}" for t, n in counts.items()).strip()

    @staticmethod
    def label(ctype: ChannelType) -> str:
        """通道类型中文标签。"""
        return {
            ChannelType.VECTOR: "向量",
            ChannelType.KEYWORD: "关键词",
            ChannelType.GRAPH: "图谱",
            ChannelType.WEB_SEARCH: "联网",
            ChannelType.HYBRID: "混合",
        }.get(ctype, str(ctype))


# ---------------------------------------------------------------------------
# 归因推导（1:1 翻译 ragent MultiChannelRetrievalEngine.deriveAttribution）
# ---------------------------------------------------------------------------


def derive_attribution(
    chunks: list[RetrievedChunk],
    scope: RetrievalScope | None,
    intents: list | None = None,
) -> dict[str, set[str]]:
    """按库推导意图归属。

    定向作用域下，最终存活 chunk 的 collection 属于某命中意图的绑定库即归属该意图
    （确定性多归属）。补充路证据的库不在任何命中意图绑定里，天然无归属；
    全局作用域没有命中意图，整体无归属。

    参数：
      chunks：后处理后的最终 chunk 列表
      scope：检索作用域（RetrievalScope）；非 INTENT 模式返回空
      intents：NodeScore 列表（提供 意图id -> 绑定collection 的映射）
    """
    if scope is None or getattr(scope, "mode", None) != "INTENT" or not chunks:
        return {}

    # 意图 id -> 绑定 collection 集合
    intent_ids_by_collection: dict[str, set[str]] = {}
    for ns in intents or []:
        node = getattr(ns, "node", None)
        if node is None or not getattr(node, "id", None):
            continue
        for collection in getattr(node, "collection_names", None) or []:
            intent_ids_by_collection.setdefault(collection, set()).add(node.id)

    # chunk key -> 归属意图 id 集合
    intent_ids_by_chunk_key: dict[str, set[str]] = {}
    for chunk in chunks:
        ids = intent_ids_by_collection.get(chunk.collection_name) if chunk.collection_name else None
        if ids:
            intent_ids_by_chunk_key[chunk.key()] = set(ids)
    return intent_ids_by_chunk_key


# ---------------------------------------------------------------------------
# 多通道检索引擎（1:1 翻译 ragent MultiChannelRetrievalEngine）
# ---------------------------------------------------------------------------


class MultiChannelRetrievalEngine:
    def __init__(
        self,
        channel_runners: dict[ChannelType, Callable[[str, list[str], int], Any]],
        config: RetrievalConfig | None = None,
        reranker: Any | None = None,
        metadata_resolver: Any | None = None,
        post_processors: list[SearchResultPostProcessor] | None = None,
        relevance_gate: Any | None = None,
    ):
        """channel_runners: 通道类型 -> 协程工厂(query, collections, top_k) -> list[RetrievedChunk]"""
        self.runners = channel_runners
        self.config = config or RetrievalConfig.default()
        self.reranker = reranker
        self.metadata_resolver = metadata_resolver
        self.relevance_gate = relevance_gate
        self.post_processors = post_processors or self._default_post_processors()

    def _default_post_processors(self) -> list[SearchResultPostProcessor]:
        return [
            DeduplicationPostProcessor(),
            FusionPostProcessor(self.config),
            RerankPostProcessor(self.reranker, self.config),
            RelevanceGatePostProcessor(self.relevance_gate, self.config, self.reranker),
            MetadataEnrichmentPostProcessor(self.metadata_resolver, self.config),
        ]

    async def retrieve(
        self,
        query: str,
        collections: list[str] | None = None,
        budget: RetrievalBudget | None = None,
        *,
        scope: RetrievalScope | None = None,
        intents: list | None = None,
        timing: dict | None = None,
    ) -> list[RetrievedChunk]:
        """兼容入口：返回后处理后的 chunk 列表（与旧调用方 agent.retriever.retrieve(...) 一致）。

        完整 Java 等价流程（含 KnowledgeRetrievalResult 与归因）见 _retrieve_knowledge_channels。
        scope / intents 为可选关键字参数：传入后引擎即可执行 1:1 的归因推导。
        timing 为可选输出参数（并发安全：每次调用传独立 dict）：收回 RRF/Rerank/RelevanceGate
        各段耗时（毫秒，键为 processor.name），供 TTFT 五段归因。
        """
        budget = budget or RetrievalBudget()
        context = self._build_search_context(query, budget, scope, intents, collections)
        result = await self._retrieve_knowledge_channels(context)
        if timing is not None:
            # 多子查询串行多次 retrieve：同名 processor 耗时累加（调用方传独立 dict，并发安全）
            for rec in context.metadata.get("post_processors", []):
                timing[rec["name"]] = round((timing.get(rec["name"]) or 0) + (rec.get("ms") or 0), 1)
        return result.chunks

    def _apply_graph_scope_quota(
        self,
        chunks: list[RetrievedChunk],
        scope: "RetrievalScope",
        base_top_k: int,
    ) -> list[RetrievedChunk]:
        """1:1 翻译 ragent GraphSearchChannel + ScopeQuota：图谱通道按库切分主路/补充路并各自封顶。

        与 Java 完全一致：
          - 全局作用域 / 无补路库 / 比例置零 / 无额度 → 额度全归主路，补充路为 0（未命中证据丢弃）
          - 定向且有补路库 → 主路封顶 primary、补充路封顶 supplement，两路按分数混排
        base_top_k 为 recallBudget（与 Java 的 ScopeQuota.split(scope, baseTopK, ...) 一致，
        不受定向上浮的 top_k 影响）。
        """
        ratio = self.config.supplement_ratio
        target_set = set(scope.collections)
        matched = [c for c in chunks if c.collection_name in target_set]
        unmatched = [c for c in chunks if c.collection_name not in target_set]
        # 不补的四种情况（对应 ragent ScopeQuota.split 早返）：比例<=0 / 无额度 / 非定向 / 无补路库
        if ratio <= 0 or base_top_k <= 0 or not scope.is_intent or not scope.supplement_collections:
            return matched[:base_top_k]
        # 夹在 [base_top_k-1 与 1 之间]：上界保证主路至少留 1 个名额，下界保证补路至少 1 个
        supplement = min(base_top_k - 1, max(1, round(base_top_k * ratio)))
        primary_limit = base_top_k - supplement
        return _merge_by_score(matched[:primary_limit], unmatched[:supplement])

    def _build_search_context(
        self,
        query: str,
        budget: RetrievalBudget,
        scope: RetrievalScope | None,
        intents: list | None,
        collections: list[str] | None,
    ) -> SearchContext:
        """1:1 翻译 ragent buildSearchContext：作用域算一次挂进上下文，各通道只读不判。"""
        if scope is None:
            # 未传入作用域时构造 GLOBAL 占位（归因自然为空，对应 ragent scope==null -> Map.of()）
            scope = RetrievalScope(
                mode="GLOBAL",
                collections=list(collections or []),
                supplement_collections=[],
                top_score=0.0,
                node_ids=[],
            )
        return SearchContext(
            original_question=query,
            rewritten_question=query,
            sub_questions=[query],
            intents=list(intents or []),
            budget=budget,
            retrieval_scope=scope,
        )

    async def _retrieve_knowledge_channels(self, context: SearchContext) -> KnowledgeRetrievalResult:
        """1:1 翻译 ragent retrieveKnowledgeChannels。"""
        channel_results = await self._execute_search_channels(context)
        if not channel_results:
            return KnowledgeRetrievalResult.empty()

        chunks = await self._execute_post_processors(channel_results, context)
        scope = context.retrieval_scope
        # 1:1 翻译 ragent deriveAttribution + scope.directedIntentIds()
        attribution = derive_attribution(chunks, scope, context.intents)
        directed_ids = set(scope.node_ids) if (scope and scope.mode == "INTENT") else set()
        return KnowledgeRetrievalResult(chunks, attribution, directed_ids)

    async def retrieve_debug(
        self, context: SearchContext
    ) -> tuple[list[SearchChannelResult], list[RetrievedChunk]]:
        """诊断专用：通道明细 + 后置处理链中间态（经 context.metadata）+ 最终 chunks。"""
        channel_results = await self._execute_search_channels(context)
        chunks = await self._execute_post_processors(channel_results, context)
        return channel_results, chunks

    async def _execute_search_channels(self, context: SearchContext) -> list[SearchChannelResult]:
        """1:1 翻译 ragent executeSearchChannels + withTimeout。

        按通道类型枚举序做稳定排序（仅为日志/派发顺序稳定可复现，不承载检索优先级语义）；
        并行执行所有启用通道；单通道超时/异常按空结果降级，不让最慢一条钳制其余通道融合。
        """
        enabled = [
            ctype
            for ctype, runner in self.runners.items()
            if self.config.channels.get(ctype, ChannelConfig()).enabled
        ]
        enabled.sort(key=lambda t: _CHANNEL_ORDER.get(t, 999))

        if not enabled:
            # 全站无任何知识召回、退化为裸 LLM，属配置事故而非正常降级，不能静默
            _logger.warning(
                "没有任何启用的检索通道，本次不做知识召回；请检查 channels 配置与对应后端开关"
            )
            return []

        _logger.info("启用的检索通道：%s", [t.value for t in enabled])

        collections = context.retrieval_scope.collections if context.retrieval_scope else None

        async def run_one(ctype: ChannelType) -> SearchChannelResult:
            # 1:1 翻译 ragent MultiChannelRetrievalEngine.withTimeout：通道级超时取全局
            # searchProperties.channels.timeoutMs（单一值），不按通道各自配超时；<=0 不另设
            channel_timeout_ms = self.config.channel_timeout_ms
            timeout = None if channel_timeout_ms <= 0 else channel_timeout_ms / 1000.0
            start = time.monotonic()
            try:
                base_top_k = context.budget.recall_budget
                top_k = base_top_k
                # 1:1 翻译 ragent GraphSearchChannel：定向时上浮 top_k 补偿结果侧过滤的召回损失
                if ctype == ChannelType.GRAPH and context.retrieval_scope is not None and context.retrieval_scope.is_intent:
                    top_k = base_top_k * _GRAPH_DIRECTED_TOPK_BOOST
                chunks = await asyncio.wait_for(
                    self.runners[ctype](
                        context.original_question, collections, top_k
                    ),
                    timeout=timeout,
                )
                # 1:1 翻译 ragent GraphSearchChannel：图谱通道按库切分主路/补充路并各自封顶
                if ctype == ChannelType.GRAPH and context.retrieval_scope is not None:
                    chunks = self._apply_graph_scope_quota(list(chunks), context.retrieval_scope, base_top_k)
                latency_ms = int((time.monotonic() - start) * 1000)
                chunks = list(chunks)
                # 1:1 翻译 ragent executeSearchChannels 的逐通道完成日志（成功 / 无结果）
                if chunks:
                    _logger.info("通道 %s 完成 ✓ - 检索到 %d 个 Chunk，耗时：%dms",
                                 ctype.value, len(chunks), latency_ms)
                else:
                    _logger.warning("通道 %s 完成但无结果 - 耗时：%dms", ctype.value, latency_ms)
                return SearchChannelResult(
                    channel_type=ctype,
                    channel_name=ctype.value,
                    chunks=chunks,
                    latency_ms=latency_ms,
                )
            except asyncio.TimeoutError:
                latency_ms = int((time.monotonic() - start) * 1000)
                _logger.warning("检索通道 %s 超过通道级超时 %dms，放弃其结果，其余通道照常融合",
                                ctype.value, int(channel_timeout_ms))
                return SearchChannelResult(
                    channel_type=ctype, channel_name=ctype.value, chunks=[], latency_ms=latency_ms
                )
            except Exception as exc:  # noqa: BLE001
                latency_ms = int((time.monotonic() - start) * 1000)
                _logger.error("检索通道 %s 执行失败", ctype.value, exc)
                return SearchChannelResult(
                    channel_type=ctype, channel_name=ctype.value, chunks=[], latency_ms=latency_ms
                )

        results = list(await asyncio.gather(*(run_one(t) for t in enabled)))

        success_count = sum(1 for r in results if r.chunks)
        failure_count = len(results) - success_count
        total_chunks = sum(len(r.chunks) for r in results)
        _logger.info(
            "多通道检索统计 - 总通道数: %d, 有结果: %d, 无结果: %d, Chunk 总数: %d",
            len(results),
            success_count,
            failure_count,
            total_chunks,
        )
        return results

    async def _execute_post_processors(
        self, results: list[SearchChannelResult], context: SearchContext
    ) -> list[RetrievedChunk]:
        """1:1 翻译 ragent executePostProcessors：按 order 顺序执行启用处理器，单处理器失败跳过。"""
        enabled = [p for p in self.post_processors if p.is_enabled(context)]
        enabled.sort(key=lambda p: p.order)

        if not enabled:
            _logger.warning("没有启用的后置处理器，直接返回原始结果")
            return [c for r in results for c in r.chunks]

        chunks = [c for r in results for c in r.chunks]
        initial_size = len(chunks)

        for processor in enabled:
            try:
                before_size = len(chunks)
                _pp_t0 = time.perf_counter()
                chunks = await processor.process(chunks, results, context)
                after_size = len(chunks)
                context.metadata.setdefault("post_processors", []).append(
                    {"name": processor.name, "before": before_size, "after": after_size,
                     "ms": round((time.perf_counter() - _pp_t0) * 1000, 1)}
                )
                _logger.info(
                    "后置处理器 %s 完成 - 输入: %d 个 Chunk, 输出: %d 个 Chunk, 变化: %+d",
                    processor.name,
                    before_size,
                    after_size,
                    after_size - before_size,
                )
            except Exception as exc:  # noqa: BLE001
                _logger.error("后置处理器 %s 执行失败，跳过该处理器: %s", processor.name, exc)

        _logger.info(
            "后置处理器链执行完成 - 初始: %d 个 Chunk, 最终: %d 个 Chunk",
            initial_size,
            len(chunks),
        )
        return chunks
