"""可插拔检索后端：对应 ragent-main rag/core/vector + keyword 通道。

默认生产栈（严格对齐 ragent infra-ai.embedding 家族）：
  - 向量嵌入：RoutingEmbeddingService（候选链 qwen-emb-8b 硅基流动 → ollama 本地 → aihubmix）
  - 默认模型：Qwen/Qwen3-Embedding-8B @ 硅基流动，1536 维
  - 向量索引：faiss-cpu IndexFlatIP（内积 = 归一化后余弦，对应 Milvus）
  - 关键词通道：rank-bm25.BM25Okapi（对应 Lucene）
  - 精排：BAAI/bge-reranker-base（见 rerank.py）
  - 熔断：2 次失败 / 30s 开放（对应 ragent ai.selection）

依赖缺失时抛 ImportError 并提示安装命令（不静默退化到伪实现）。
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any

from core.embedding import EmbeddingConfig, RoutingEmbeddingService
from core.retrieval.types import ChannelType, RetrievedChunk


# =============================================================================
# 抽象接口
# =============================================================================

class VectorBackend(ABC):
    """向量检索后端接口。生产可替换为 Milvus/PGVector/ES dense。"""

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        ...

    @abstractmethod
    async def retrieve(self, query_vector: list[float], collections: list[str], top_k: int) -> list[RetrievedChunk]:
        ...


class KeywordBackend(ABC):
    """关键词检索后端接口。生产可替换为 ES BM25/Lucene。"""

    @abstractmethod
    async def retrieve(self, query: str, collections: list[str], top_k: int) -> list[RetrievedChunk]:
        ...


# =============================================================================
# 向量通道：FAISS（严格对齐 ragent infra-ai.embedding：维度硬约束 1536）
# =============================================================================

# 单次嵌入最大批大小——对应 ragent SiliconFlowEmbeddingClient.maxBatchSize()=32；
# 这里用同一个常量覆盖所有 provider 的 client（OpenAI 兼容类统一实现）。
_DEFAULT_EMBED_BATCH = 32


class FaissVectorBackend(VectorBackend):
    """FAISS-cpu 向量后端（IndexFlatIP，归一化后内积 = 余弦）。

    documents: list of {content, collection, doc_id, metadata?}
    构造时立即编码所有文档并建索引（离线一次性成本）。
    向量维度由 EmbeddingConfig.embedding_dim 决定（默认 1536，与 ragent rag.default.dimension 一致）。
    """

    def __init__(
        self,
        documents: list[dict[str, Any]],
        routing: RoutingEmbeddingService,
        config: EmbeddingConfig | None = None,
    ):
        try:
            import faiss  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "FaissVectorBackend 需要 faiss-cpu，请执行：\n"
                "  pip install faiss-cpu"
            ) from e
        if routing is None:
            raise ValueError("FaissVectorBackend 必须传入 routing（RoutingEmbeddingService）")
        self.routing = routing
        self.config = config or EmbeddingConfig()
        # 严格对齐 ragent rag.default.dimension: 1536
        self.dim: int = self.config.embedding_dim
        self.docs = documents
        self._index = None
        self._doc_ids: list[str] = []
        self._build_index()

    def _build_index(self) -> None:
        """构造期同步编码所有文档（分批走 ragent max_batch_size=32 的约定）。"""
        import faiss
        import numpy as np

        if not self.docs:
            self._index = faiss.IndexFlatIP(self.dim)
            return
        texts = [d["content"] for d in self.docs]
        all_vecs: list[list[float]] = []
        for i in range(0, len(texts), _DEFAULT_EMBED_BATCH):
            batch = texts[i:i + _DEFAULT_EMBED_BATCH]
            all_vecs.extend(self.routing.embed_batch(batch).vectors)
        arr = np.asarray(all_vecs, dtype="float32")
        self._index = faiss.IndexFlatIP(self.dim)
        self._index.add(arr)  # type: ignore[arg-type]
        self._doc_ids = [d.get("doc_id", str(i)) for i, d in enumerate(self.docs)]

    async def embed(self, text: str) -> list[float]:
        # 同步 HTTP（httpx）走 to_thread，避免阻塞事件循环
        return await asyncio.to_thread(self.routing.embed, text)

    async def retrieve(self, query_vector: list[float], collections: list[str], top_k: int) -> list[RetrievedChunk]:
        import faiss
        import numpy as np

        if self._index is None or self._index.ntotal == 0:
            return []
        # FAISS IndexFlatIP 不支持 metadata filter，使用候选子索引避免先取 top-k 再过滤导致漏召回。
        candidate_idx = [i for i, d in enumerate(self.docs) if not collections or d.get("collection") in collections]
        if not candidate_idx:
            return []
        q = np.asarray([query_vector], dtype="float32")
        candidate_vectors = np.asarray([self._index.reconstruct(i) for i in candidate_idx], dtype="float32")
        filtered_index = faiss.IndexFlatIP(self.dim)
        filtered_index.add(candidate_vectors)
        k = min(top_k, len(candidate_idx))
        scores, filtered_ids = filtered_index.search(q, k)  # type: ignore[arg-type]
        results: list[RetrievedChunk] = []
        for s, filtered_idx in zip(scores[0].tolist(), filtered_ids[0].tolist()):
            if filtered_idx < 0:
                continue
            idx = candidate_idx[filtered_idx]
            d = self.docs[idx]
            # 严格对齐 ragent RetrievedChunk 7 字段（id/text/score/collectionName/docId/chunkIndex/docName）
            doc_id = d.get("doc_id", str(idx))
            doc_meta = d.get("metadata", {}) or {}
            results.append(RetrievedChunk(
                id=f"{doc_id}::{idx}",
                text=d["content"],
                score=float(s),
                collection_name=d.get("collection"),
                doc_id=doc_id,
                chunk_index=doc_meta.get("chunk_index", idx),
                doc_name=doc_meta.get("doc_name") or doc_id,
                channel=ChannelType.VECTOR,
            ))
            if len(results) >= top_k:
                break
        return results


# =============================================================================
# 关键词通道：BM25（rank-bm25）
# =============================================================================

class BM25Backend(KeywordBackend):
    """BM25 关键词后端（rank-bm25.BM25Okapi），对应 ES/Lucene keyword 通道。"""

    def __init__(self, documents: list[dict[str, Any]]):
        try:
            from rank_bm25 import BM25Okapi
        except ImportError as e:
            raise ImportError(
                "BM25Backend 需要 rank-bm25，请执行：\n"
                "  pip install rank-bm25"
            ) from e
        self.docs = documents
        # 简单中文分词：jieba 优先，缺失则退化到字符 bigram
        try:
            import jieba
            self._tokenize = lambda t: [w for w in jieba.lcut(t) if w.strip()]
        except ImportError:
            self._tokenize = _char_bigrams
        self._bm25 = None
        if documents:
            tokenized_corpus = [self._tokenize(d["content"]) for d in documents]
            self._bm25 = BM25Okapi(tokenized_corpus)

    async def retrieve(self, query: str, collections: list[str], top_k: int) -> list[RetrievedChunk]:
        if self._bm25 is None or not self.docs:
            return []
        q_tokens = self._tokenize(query)
        if not q_tokens:
            return []
        scores = self._bm25.get_scores(q_tokens)
        # 索引 - 分数配对 + collection 过滤
        scored_idx = [
            (i, s) for i, s in enumerate(scores)
            if s > 0 and (not collections or self.docs[i].get("collection") in collections)
        ]
        scored_idx.sort(key=lambda x: x[1], reverse=True)
        results: list[RetrievedChunk] = []
        for i, s in scored_idx[:top_k]:
            d = self.docs[i]
            # 严格对齐 ragent RetrievedChunk 7 字段（id/text/score/collectionName/docId/chunkIndex/docName）
            doc_id = d.get("doc_id", str(i))
            doc_meta = d.get("metadata", {}) or {}
            results.append(RetrievedChunk(
                id=f"{doc_id}::{i}",
                text=d["content"],
                score=float(s),
                collection_name=d.get("collection"),
                doc_id=doc_id,
                chunk_index=doc_meta.get("chunk_index", i),
                doc_name=doc_meta.get("doc_name") or doc_id,
                channel=ChannelType.KEYWORD,
            ))
        return results


def _char_bigrams(text: str) -> list[str]:
    """无 jieba 时的中文退路：字符 bigram（比字符 unigram 更稳）。"""
    s = text.strip()
    if len(s) < 2:
        return [s] if s else []
    return [s[i:i + 2] for i in range(len(s) - 1)]


# =============================================================================
# 混合检索器：把 routing/faiss/bm25/rerank 串成一个 HybridRetriever
# =============================================================================

class HybridRetriever:
    """向量 + 关键词混合检索封装（多通道并行由 engine 层负责）。

    关键变化（对应 ragent 迁移）：
      - 不再用 BgeEmbedding（sentence-transformers 本地 512 维）
      - 改为注入 RoutingEmbeddingService：默认走 Qwen3-Embedding-8B @ 硅基流动（1536 维）
      - 维度由 EmbeddingConfig.embedding_dim 决定（严格对齐 ragent rag.default.dimension: 1536）
    """

    def __init__(
        self,
        documents: list[dict[str, Any]],
        routing: RoutingEmbeddingService | None = None,
        config: EmbeddingConfig | None = None,
    ):
        if routing is None:
            raise ValueError(
                "HybridRetriever 必须传入 routing（RoutingEmbeddingService）。"
                "用法：先构造 EmbeddingConfig + ModelHealthStore + ModelRoutingExecutor + RoutingEmbeddingService，"
                "再传给 HybridRetriever。"
            )
        self.routing = routing
        self.config = config or EmbeddingConfig()
        self.dim: int = self.config.embedding_dim
        self.vector = FaissVectorBackend(documents, routing=routing, config=self.config)
        self.keyword = BM25Backend(documents)

    async def embed_query(self, query: str) -> list[float]:
        return await self.vector.embed(query)

    async def vector_search(self, query: str, collections: list[str], top_k: int) -> list[RetrievedChunk]:
        qv = await self.embed_query(query)
        return await self.vector.retrieve(qv, collections, top_k)

    async def keyword_search(self, query: str, collections: list[str], top_k: int) -> list[RetrievedChunk]:
        return await self.keyword.retrieve(query, collections, top_k)
