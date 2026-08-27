"""向量写入同步：1:1 翻译 ragent VectorStoreService + Graph/Keyword 同步装饰器 + BeanPostProcessor 接线。

- VectorStoreService：向量写服务接口（indexDocumentChunks / updateChunk / deleteDocumentVectors / deleteChunkById / deleteChunksByIds）
- GraphSyncingVectorStoreService：写入/删除成功后同步维护 LightRAG 知识图谱（best-effort）
- KeywordSyncingVectorStoreService：写入/删除成功后同步维护关键词索引（best-effort）
- GraphSyncVectorStorePostProcessor / KeywordSyncVectorStorePostProcessor：对应 Java BeanPostProcessor，
  当容器中存在 LightRagClient / KeywordIndexService 时把真实 VectorStoreService 包成同步装饰器
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from core.retrieval.external_channels import GraphFileSource

_logger = logging.getLogger("sentinel.vector.sync")


@dataclass
class EmbeddedChunk:
    """1:1 翻译 ragent core.chunk.model.EmbeddedChunk（最小字段）。"""

    id: str
    doc_id: str
    collection_name: str
    content: str
    embedding: list[float] | None = None


@runtime_checkable
class LightRagClient(Protocol):
    async def insert_text(self, text: str, file_source: str) -> None: ...
    async def delete_by_doc(self, doc_id: str) -> None: ...


@runtime_checkable
class KeywordIndexService(Protocol):
    def index_document_chunks(self, collection_name: str, doc_id: str, chunks: list[EmbeddedChunk]) -> None: ...
    def update_chunk(self, collection_name: str, doc_id: str, chunk: EmbeddedChunk) -> None: ...
    def delete_document_index(self, collection_name: str, doc_id: str) -> None: ...
    def delete_chunk_by_id(self, collection_name: str, chunk_id: str) -> None: ...
    def delete_chunks_by_ids(self, collection_name: str, chunk_ids: list[str]) -> None: ...


class VectorStoreService(Protocol):
    """1:1 翻译 ragent VectorStoreService。"""

    def index_document_chunks(self, collection_name: str, doc_id: str, chunks: list[EmbeddedChunk]) -> None: ...
    def update_chunk(self, collection_name: str, doc_id: str, chunk: EmbeddedChunk) -> None: ...
    def delete_document_vectors(self, collection_name: str, doc_id: str) -> None: ...
    def delete_chunk_by_id(self, collection_name: str, chunk_id: str) -> None: ...
    def delete_chunks_by_ids(self, collection_name: str, chunk_ids: list[str]) -> None: ...


class GraphSyncingVectorStoreService:
    """1:1 翻译 ragent GraphSyncingVectorStoreService。"""

    def __init__(self, delegate: VectorStoreService, light_rag_client: LightRagClient) -> None:
        self.delegate = delegate
        self.light_rag_client = light_rag_client

    def index_document_chunks(self, collection_name: str, doc_id: str, chunks: list[EmbeddedChunk]) -> None:
        self.delegate.index_document_chunks(collection_name, doc_id, chunks)
        self._sync_graph(doc_id, lambda: self.light_rag_client.insert_text(
            self._concat_content(chunks), GraphFileSource.encode(collection_name, doc_id)))

    def update_chunk(self, collection_name: str, doc_id: str, chunk: EmbeddedChunk) -> None:
        self.delegate.update_chunk(collection_name, doc_id, chunk)
        # 子文档粒度，Phase1 不单独同步图谱（见 Java 类注释）

    def delete_document_vectors(self, collection_name: str, doc_id: str) -> None:
        self.delegate.delete_document_vectors(collection_name, doc_id)
        self._sync_graph(doc_id, lambda: self.light_rag_client.delete_by_doc(doc_id))

    def delete_chunk_by_id(self, collection_name: str, chunk_id: str) -> None:
        self.delegate.delete_chunk_by_id(collection_name, chunk_id)
        # 子文档粒度，Phase1 不单独同步图谱

    def delete_chunks_by_ids(self, collection_name: str, chunk_ids: list[str]) -> None:
        self.delegate.delete_chunks_by_ids(collection_name, chunk_ids)
        # 子文档粒度，Phase1 不单独同步图谱

    @staticmethod
    def _concat_content(chunks: list[EmbeddedChunk]) -> str:
        if not chunks:
            return ""
        return "\n\n".join(c.content for c in chunks if c.content and c.content.strip())

    @staticmethod
    def _sync_graph(doc_id: str, action) -> None:
        try:
            import asyncio

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(action())
            finally:
                loop.close()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("图谱同步失败，已跳过 docId=%s: %s", doc_id, exc)


class KeywordSyncingVectorStoreService:
    """1:1 翻译 ragent KeywordSyncingVectorStoreService。"""

    def __init__(self, delegate: VectorStoreService, keyword_index_service: KeywordIndexService) -> None:
        self.delegate = delegate
        self.keyword_index_service = keyword_index_service

    def index_document_chunks(self, collection_name: str, doc_id: str, chunks: list[EmbeddedChunk]) -> None:
        self.delegate.index_document_chunks(collection_name, doc_id, chunks)
        self._sync_keyword(doc_id, lambda: self.keyword_index_service.index_document_chunks(
            collection_name, doc_id, chunks))

    def update_chunk(self, collection_name: str, doc_id: str, chunk: EmbeddedChunk) -> None:
        self.delegate.update_chunk(collection_name, doc_id, chunk)
        self._sync_keyword(doc_id, lambda: self.keyword_index_service.update_chunk(collection_name, doc_id, chunk))

    def delete_document_vectors(self, collection_name: str, doc_id: str) -> None:
        self.delegate.delete_document_vectors(collection_name, doc_id)
        self._sync_keyword(doc_id, lambda: self.keyword_index_service.delete_document_index(collection_name, doc_id))

    def delete_chunk_by_id(self, collection_name: str, chunk_id: str) -> None:
        self.delegate.delete_chunk_by_id(collection_name, chunk_id)
        self._sync_keyword(chunk_id, lambda: self.keyword_index_service.delete_chunk_by_id(collection_name, chunk_id))

    def delete_chunks_by_ids(self, collection_name: str, chunk_ids: list[str]) -> None:
        self.delegate.delete_chunks_by_ids(collection_name, chunk_ids)
        self._sync_keyword(None, lambda: self.keyword_index_service.delete_chunks_by_ids(collection_name, chunk_ids))

    @staticmethod
    def _sync_keyword(doc_id: str | None, action) -> None:
        try:
            action()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("关键词索引同步失败，已跳过 docId=%s: %s", doc_id, exc)


# ---------------------------------------------------------------------------
# 接线后处理器（1:1 翻译 Java BeanPostProcessor）
# ---------------------------------------------------------------------------
class GraphSyncVectorStorePostProcessor:
    """1:1 翻译 ragent GraphSyncVectorStorePostProcessor。"""

    def __init__(self, light_rag_client: LightRagClient | None) -> None:
        self.light_rag_client = light_rag_client

    def post_process(self, bean_name: str, bean: Any) -> Any:
        if isinstance(bean, VectorStoreService) and not isinstance(bean, GraphSyncingVectorStoreService):
            if self.light_rag_client is not None:
                _logger.info("检测到图谱后端实现，向量写入将同步维护知识图谱, vectorStore=%s", bean_name)
                return GraphSyncingVectorStoreService(bean, self.light_rag_client)
        return bean


class KeywordSyncVectorStorePostProcessor:
    """1:1 翻译 ragent KeywordSyncVectorStorePostProcessor。"""

    def __init__(self, keyword_index_service: KeywordIndexService | None) -> None:
        self.keyword_index_service = keyword_index_service

    def post_process(self, bean_name: str, bean: Any) -> Any:
        if isinstance(bean, VectorStoreService) and not isinstance(bean, KeywordSyncingVectorStoreService):
            if self.keyword_index_service is not None:
                _logger.info("检测到关键词索引实现，向量写入将同步维护关键词索引, vectorStore=%s", bean_name)
                return KeywordSyncingVectorStoreService(bean, self.keyword_index_service)
        return bean
