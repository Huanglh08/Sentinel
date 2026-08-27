"""Rerank 后置：对应 ragent-main RerankPostProcessor + infra.rerank.RerankService。

抽象为 Reranker 接口，可插拔：
  - LLMReranker：用 LLM 对 (query, chunk) 打分（无专用 rerank 模型时的兜底）
  - CrossEncoderReranker：接本地交叉编码器（生产推荐，需安装 sentence-transformers）
  - 默认占位：当 rerank_enabled=False 时引擎直接截断，不调用
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from core.retrieval.types import RetrievedChunk


class Reranker(ABC):
    @abstractmethod
    async def rerank(self, query: str, chunks: list[RetrievedChunk], top_k: int) -> list[RetrievedChunk]:
        ...


class CrossEncoderReranker(Reranker):
    """本地交叉编码器精排（需 sentence-transformers）。"""

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3"):
        self.model_name = model_name
        self._model = None

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
        except Exception:
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

    async def rerank(self, query: str, chunks: list[RetrievedChunk], top_k: int) -> list[RetrievedChunk]:
        for reranker in self.chain:
            try:
                return await reranker.rerank(query, chunks, top_k)
            except Exception:  # noqa: BLE001
                # 降级：尝试下一个 reranker
                continue
        # 全部失败：原样截断返回（与 ragent 兜底一致）
        return chunks[:top_k] if top_k > 0 else chunks
