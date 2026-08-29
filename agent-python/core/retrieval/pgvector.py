"""PostgreSQL/pgvector 检索后端：直接读取 Java 端入库写入的向量库，与 ragent 原行为一致。

严格对齐 ragent-main PgVectorStoreService 的 schema：
  - 表 t_knowledge_vector(id text, collection_name text, content text, metadata jsonb, embedding vector)
  - 余弦距离用 pgvector 原生 `<#>` 算子（1 - (embedding <=> query) 即余弦相似度）
  - collection_name 字段过滤（对应 Java 知识库隔离）
  - metadata jsonb 含 doc_id / chunk_index / doc_name

这样 Python 检索与 Java 入库共用同一份 pgvector 数据，向量维度 1536 与 Java rag.default.dimension 一致，
embedding 由 RoutingEmbeddingService（Qwen3-Embedding-8B）生成，保证向量可比。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from core.embedding import EmbeddingConfig, RoutingEmbeddingService
from core.retrieval.retriever import VectorBackend
from core.retrieval.types import ChannelType, RetrievedChunk

_logger = logging.getLogger("sentinel.retrieval.pgvector")


class PgVectorBackend(VectorBackend):
    """pgvector 向量后端（对应 ragent PgVectorRetrieverService）。

    查询 SQL（Cosine，pgvector <=>）：
        SELECT id, content, metadata, 1 - (embedding <=> $1) AS score
        FROM t_knowledge_vector
        WHERE collection_name = ANY($2)
        ORDER BY embedding <=> $1
        LIMIT $3
    """

    def __init__(
        self,
        routing: RoutingEmbeddingService,
        dsn: str | None = None,
        table: str = "t_knowledge_vector",
        config: EmbeddingConfig | None = None,
    ):
        if routing is None:
            raise ValueError("PgVectorBackend 必须传入 routing（RoutingEmbeddingService）")
        self.routing = routing
        self.config = config or EmbeddingConfig()
        # DSN 统一走 core.db（与 Java application.yaml datasource 对齐：sentinel 库）
        from core.db import get_dsn

        self.dsn = dsn or get_dsn()
        self.table = table
        self._pool = None  # 延迟创建，避免无 pg 依赖时启动崩溃

    def _get_pool(self):
        if self._pool is None:
            try:
                import psycopg_pool
            except ImportError as e:
                raise ImportError(
                    "PgVectorBackend 需要 psycopg_pool，请执行：\n  pip install psycopg_pool"
                ) from e
            self._pool = psycopg_pool.ConnectionPool(
                self.dsn, open=True, max_size=4, kwargs={"autocommit": True}
            )
        return self._pool

    def get_doc(self, doc_id: str | None) -> dict[str, str]:
        if not doc_id:
            return {}
        sql = (
            "SELECT doc_name, source_type, file_type, source_location "
            "FROM t_knowledge_document "
            "WHERE id = %s AND deleted = 0 "
            "LIMIT 1"
        )
        try:
            pool = self._get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(sql, (str(doc_id),))
                row = cur.fetchone()
        except Exception as exc:
            _logger.warning("document metadata lookup failed doc_id=%s: %s", doc_id, exc)
            return {}
        if not row:
            return {}
        doc_name, source_type, file_type, source_location = row
        return {
            "doc_name": doc_name or "",
            "source_type": source_type or "",
            "file_type": file_type or "",
            "url": source_location or "",
        }

    async def embed(self, text: str) -> list[float]:
        return await asyncio.to_thread(self.routing.embed, text)

    def _row_to_chunk(
        self,
        rid: Any,
        collection_name: Any,
        content: Any,
        metadata: Any,
        score: Any,
        channel: ChannelType,
    ) -> RetrievedChunk:
        meta = metadata or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        doc_id = meta.get("doc_id") or rid
        return RetrievedChunk(
            id=str(rid),
            text=content or "",
            score=float(score or 0.0),
            collection_name=collection_name or meta.get("collection_name"),
            doc_id=str(doc_id),
            chunk_index=meta.get("chunk_index", 0),
            doc_name=meta.get("doc_name") or meta.get("document_name") or str(doc_id),
            channel=channel,
            source_type=meta.get("source_type") or meta.get("sourceType"),
            file_type=meta.get("file_type") or meta.get("fileType"),
            url=meta.get("url"),
        )

    async def retrieve(
        self, query: str, collections: list[str], top_k: int
    ) -> list[RetrievedChunk]:
        if not collections:
            return []
        qv = await self.embed(query)
        # pgvector 文本字面量：[v1,v2,...]
        vec_literal = "[" + ",".join(f"{x:.8g}" for x in qv) + "]"

        sql = (
            f"SELECT id, collection_name, content, metadata, 1 - (embedding <=> %s::vector) AS score "
            f"FROM {self.table} "
            f"WHERE collection_name = ANY(%s) "
            f"ORDER BY embedding <=> %s::vector "
            f"LIMIT %s"
        )
        params = (vec_literal, list(collections), vec_literal, int(top_k))

        def _query() -> list[RetrievedChunk]:
            pool = self._get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
            return [
                self._row_to_chunk(rid, collection_name, content, metadata, score, ChannelType.VECTOR)
                for rid, collection_name, content, metadata, score in rows
            ]

        try:
            return await asyncio.to_thread(_query)
        except Exception as exc:
            _logger.warning("pgvector 检索失败: %s", exc)
            return []

    async def retrieve_keyword(
        self, query: str, collections: list[str], top_k: int
    ) -> list[RetrievedChunk]:
        if not query.strip() or not collections:
            return []
        sql = (
            f"SELECT id, collection_name, content, metadata, "
            f"ts_rank_cd(to_tsvector('simple', coalesce(content, '')), plainto_tsquery('simple', %s)) AS score "
            f"FROM {self.table} "
            f"WHERE collection_name = ANY(%s) "
            f"AND to_tsvector('simple', coalesce(content, '')) @@ plainto_tsquery('simple', %s) "
            f"ORDER BY score DESC "
            f"LIMIT %s"
        )
        params = (query, list(collections), query, int(top_k))

        def _query() -> list[RetrievedChunk]:
            pool = self._get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
            return [
                self._row_to_chunk(rid, collection_name, content, metadata, score, ChannelType.KEYWORD)
                for rid, collection_name, content, metadata, score in rows
            ]

        try:
            return await asyncio.to_thread(_query)
        except Exception as exc:
            _logger.warning("pg keyword 检索失败: %s", exc)
            return []
