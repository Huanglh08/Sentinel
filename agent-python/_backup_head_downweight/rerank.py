"""Rerank 后置：对应 ragent-main RerankPostProcessor + infra.rerank.RerankService。

抽象为 Reranker 接口，可插拔：
  - SiliconFlowReranker：硅基流动 rerank API 精排（当前生产首选，零本地模型依赖）
  - LLMReranker：用 LLM 对 (query, chunk) 打分（无专用 rerank 模型时的兜底）
  - CrossEncoderReranker：本地交叉编码器（已废弃——本地双模型已删除，
    误用会在请求路径上触发 huggingface_hub 下载 backoff，同步阻塞 asyncio
    事件循环导致整个 agent 假死，2026-09-01 线上事故根因，勿再启用）
  - 默认占位：当 rerank_enabled=False 时引擎直接截断，不调用
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

import httpx

from core.retrieval.types import RetrievedChunk

logger = logging.getLogger(__name__)

# 硅基流动 rerank API（与 rerank_gate 同源，复用 key 解析）
_SF_RERANK_URL = os.environ.get("RERANK_API_URL") or "https://api.siliconflow.cn/v1/rerank"
_SF_RERANK_MODEL = os.environ.get("RERANK_MODEL") or "BAAI/bge-reranker-v2-m3"
_SF_TIMEOUT_S = float(os.environ.get("RERANK_TIMEOUT_S") or "10.0")
_MAX_DOC_CHARS = 4000  # 单条 document 超 API 限制会被拒，保守截断


class Reranker(ABC):
    @abstractmethod
    async def rerank(self, query: str, chunks: list[RetrievedChunk], top_k: int) -> list[RetrievedChunk]:
        ...


class SiliconFlowReranker(Reranker):
    """硅基流动 rerank API 精排（替代本地 CrossEncoder，勿再走本地权重路线）。

    失败抛异常，由 RoutingReranker 降级到链上的下一个 reranker（LLM 兜底）。
    """

    def __init__(self, model: str = _SF_RERANK_MODEL, timeout_s: float = _SF_TIMEOUT_S):
        self.model = model
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def rerank(self, query: str, chunks: list[RetrievedChunk], top_k: int) -> list[RetrievedChunk]:
        if not chunks:
            return chunks
        from core.retrieval.rerank_gate import _load_api_key

        api_key = _load_api_key()
        if not api_key:
            raise RuntimeError("SILICONFLOW_API_KEY 未配置，rerank 降级")

        # 严格对齐 ragent：text 字段（java 端 text，不是 content）
        documents = [c.text[:_MAX_DOC_CHARS] for c in chunks]
        resp = await self._client.post(
            _SF_RERANK_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": self.model, "query": query, "documents": documents, "top_n": len(documents)},
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        for item in results:
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < len(chunks):
                chunks[idx].score = float(item.get("relevance_score", 0.0))
        ranked = sorted(chunks, key=lambda c: c.score, reverse=True)
        logger.info(
            "SiliconFlow rerank - model=%s in=%d top1=%.4f", self.model, len(chunks),
            ranked[0].score if ranked else -1.0,
        )
        return ranked[:top_k] if top_k > 0 else ranked


class CrossEncoderReranker(Reranker):
    """【已废弃】本地交叉编码器精排。

    本地双模型（models/bge-reranker-v2-m3 等）已于 2026-09-01 删除，
    实例化后首次 rerank 会经 huggingface_hub 尝试联网下载权重并进入
    HTTP backoff，同步阻塞 asyncio 事件循环，导致 agent 全端点假死。
    保留类仅为兼容 import，勿注入 RoutingReranker 链。
    """

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3"):
        self.model_name = model_name
        self._model = None
        raise RuntimeError(
            "CrossEncoderReranker 已废弃（本地模型已删除，触发会联网下载并阻塞事件循环），"
            "请改用 SiliconFlowReranker"
        )

    def _load(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder
            self._model = CrossEncoder(self.model_name)
        return self._model

    async def rerank(self, query: str, chunks: list[RetrievedChunk], top_k: int) -> list[RetrievedChunk]:
        if not chunks:
            return chunks
        model = self._load()
        pairs = [(query, c.text) for c in chunks]  # 严格对齐 ragent：text 字段（java 端 text，不是 content）
        scores = model.predict(pairs)
        for c, s in zip(chunks, scores):
            c.score = float(s)
        chunks = sorted(chunks, key=lambda c: c.score, reverse=True)
        return chunks[:top_k] if top_k > 0 else chunks


class LLMReranker(Reranker):
    """LLM 打分精排（无专用 rerank 模型时的兜底，成本较高）。"""

    def __init__(self, llm_client):
        self.llm = llm_client

    async def rerank(self, query: str, chunks: list[RetrievedChunk], top_k: int) -> list[RetrievedChunk]:
        if not chunks:
            return chunks
        # 简化：让 LLM 对整体相关性给分（实际可逐条打分后排序）
        import json
        numbered = "\n".join(f"{i}. {c.text[:200]}" for i, c in enumerate(chunks))  # 严格对齐 ragent text 字段
        prompt = (f"给定问题：{query}\n下面候选片段按相关性从强到弱排序，"
                  f"只输出编号列表，如 2,0,1：\n{numbered}")
        try:
            raw = await self.llm.chat([{"role": "user", "content": prompt}], temperature=0.1)
            order = [int(x) for x in raw.replace("[", "").replace("]", "").split(",") if x.strip().isdigit()]
            ranked = [chunks[i] for i in order if 0 <= i < len(chunks)]
            # 补回未排序项
            seen = set(order)
            ranked += [c for i, c in enumerate(chunks) if i not in seen]
            return ranked[:top_k] if top_k > 0 else ranked
        except Exception as e:  # noqa: BLE001
            logger.warning("LLM 重排失败，回退原始顺序: %s", e)
            return chunks[:top_k] if top_k > 0 else chunks


class RoutingReranker(Reranker):
    """严格对应 ragent infra-ai.rerank.RoutingRerankService。

    路由策略：按 chain 顺序降级——第一个不抛异常的 reranker 返回结果。
    与嵌入层 RoutingEmbeddingService 的 priority + 熔断策略不同：
      - 嵌入层：候选链 + ModelHealthStore 熔断（每个候选熔断独立）
      - Rerank 层：纯顺序链式（rerank 失败通常是临时计算错误，不做熔断）
    """
    def __init__(self, chain: list[Reranker]) -> None:
        self.chain = chain
        # 最近一次成功的 reranker（下游 RelevanceGate 据此判断 chunk.score 语义是否为 bge 分）
        self.last_used: Reranker | None = None

    async def rerank(self, query: str, chunks: list[RetrievedChunk], top_k: int) -> list[RetrievedChunk]:
        for reranker in self.chain:
            try:
                result = await reranker.rerank(query, chunks, top_k)
                self.last_used = reranker
                return result
            except Exception:  # noqa: BLE001
                # 降级：尝试下一个 reranker
                continue
        # 全部失败：原样截断返回（与 ragent 兜底一致）；此时 score 仍是 RRF 融合分
        self.last_used = None
        return chunks[:top_k] if top_k > 0 else chunks
