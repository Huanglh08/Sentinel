"""向量写入的图谱/关键词同步装饰器：1:1 翻译 ragent
GraphSyncVectorStorePostProcessor / GraphSyncingVectorStoreService /
KeywordSyncVectorStorePostProcessor / KeywordSyncingVectorStoreService。

装饰器链与 Java 同构：GraphSyncing(KeywordSyncing(真实实现))，两者独立开关、互不影响。
图谱抽取为文档级（跨 chunk 合并实体），故 indexDocumentChunks / deleteDocumentVectors 同步，
单块粒度 updateChunk / deleteChunkById / deleteChunksByIds 不同步。所有同步均为 best-effort：
失败仅记日志、不回滚向量、不中断主链路。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from typing import List, Optional, Protocol, runtime_checkable

_logger = logging.getLogger("sentinel.retrieval.sync")


@runtime_checkable
class VectorStoreService(Protocol):
    """1:1 对应 ragent VectorStoreService 写接口。"""

    def indexDocumentChunks(self, collection_name: str, doc_id: str, chunks: list) -> None: ...

    def updateChunk(self, collection_name: str, doc_id: str, chunk: object) -> None: ...

    def deleteDocumentVectors(self, collection_name: str, doc_id: str) -> None: ...

    def deleteChunkById(self, collection_name: str, chunk_id: str) -> None: ...

    def deleteChunksByIds(self, collection_name: str, chunk_ids: list) -> None: ...


@runtime_checkable
class LightRagClient(Protocol):
    """1:1 对应 ragent LightRagClient（图谱后端，可为 async）。"""

    def insert_text(self, text: str, file_source: str) -> None: ...

    def delete_by_doc(self, doc_id: str) -> None: ...


@runtime_checkable
class KeywordIndexService(Protocol):
    """1:1 对应 ragent KeywordIndexService（关键词索引后端）。"""

    def indexDocumentChunks(self, collection_name: str, doc_id: str, chunks: list) -> None: ...

    def updateChunk(self, collection_name: str, doc_id: str, chunk: object) -> None: ...

    def deleteDocumentIndex(self, collection_name: str, doc_id: str) -> None: ...

    def deleteChunkById(self, collection_name: str, chunk_id: str) -> None: ...

    def deleteChunksByIds(self, collection_name: str, chunk_ids: list) -> None: ...


class GraphFileSource:
    """1:1 对应 ragent GraphFileSource.encode：图谱 file_source 编码。"""

    @staticmethod
    def encode(collection_name: str, doc_id: str) -> str:
        return f"{collection_name}::{doc_id}"


def _run_async(coro):
    """在独立线程的事件循环中运行协程，避免与调用方（如 ingest 协程）事件循环冲突。"""
    if not inspect.iscoroutine(coro):
        return coro
    result: list = []
    exc: list = [None]

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            result.append(loop.run_until_complete(coro))
        except Exception as err:  # noqa: BLE001
            exc[0] = err
        finally:
            loop.close()

    worker = threading.Thread(target=_runner)
    worker.start()
    worker.join()
    if exc[0] is not None:
        raise exc[0]
    return result[0] if result else None


class GraphSyncingVectorStoreService:
    """1:1 翻译 ragent GraphSyncingVectorStoreService。"""

    def __init__(self, delegate: VectorStoreService, light_rag_client: LightRagClient) -> None:
        self._delegate = delegate
        self._light_rag = light_rag_client

    def indexDocumentChunks(self, collection_name: str, doc_id: str, chunks: list) -> None:
        self._delegate.indexDocumentChunks(collection_name, doc_id, chunks)
        coro = self._light_rag.insert_text(
            self._concat_content(chunks), GraphFileSource.encode(collection_name, doc_id))
        self._sync_graph(doc_id, coro)

    def updateChunk(self, collection_name: str, doc_id: str, chunk: object) -> None:
        self._delegate.updateChunk(collection_name, doc_id, chunk)
        # 子文档粒度，Phase1 不单独同步图谱（见类注释）

    def deleteDocumentVectors(self, collection_name: str, doc_id: str) -> None:
        self._delegate.deleteDocumentVectors(collection_name, doc_id)
        coro = self._light_rag.delete_by_doc(doc_id)
        self._sync_graph(doc_id, coro)

    def deleteChunkById(self, collection_name: str, chunk_id: str) -> None:
        self._delegate.deleteChunkById(collection_name, chunk_id)
        # 子文档粒度，Phase1 不单独同步图谱（见类注释）

    def deleteChunksByIds(self, collection_name: str, chunk_ids: list) -> None:
        self._delegate.deleteChunksByIds(collection_name, chunk_ids)
        # 子文档粒度，Phase1 不单独同步图谱（见类注释）

    @staticmethod
    def _concat_content(chunks: list) -> str:
        if not chunks:
            return ""
        parts: list[str] = []
        for c in chunks:
            text = c.get("content") if isinstance(c, dict) else getattr(c, "content", None)
            if text and str(text).strip():
                parts.append(str(text))
        return "\n\n".join(parts)

    def _sync_graph(self, doc_id: str, coro) -> None:
        try:
            _run_async(coro)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("图谱同步失败，已跳过 docId=%s: %s", doc_id, exc)


class KeywordSyncingVectorStoreService:
    """1:1 翻译 ragent KeywordSyncingVectorStoreService。"""

    def __init__(self, delegate: VectorStoreService,
                 keyword_index_service: KeywordIndexService) -> None:
        self._delegate = delegate
        self._keyword = keyword_index_service

    def indexDocumentChunks(self, collection_name: str, doc_id: str, chunks: list) -> None:
        self._delegate.indexDocumentChunks(collection_name, doc_id, chunks)
        self._sync_keyword(doc_id, lambda: self._keyword.indexDocumentChunks(
            collection_name, doc_id, chunks))

    def updateChunk(self, collection_name: str, doc_id: str, chunk: object) -> None:
        self._delegate.updateChunk(collection_name, doc_id, chunk)
        self._sync_keyword(doc_id, lambda: self._keyword.updateChunk(
            collection_name, doc_id, chunk))

    def deleteDocumentVectors(self, collection_name: str, doc_id: str) -> None:
        self._delegate.deleteDocumentVectors(collection_name, doc_id)
        self._sync_keyword(doc_id, lambda: self._keyword.deleteDocumentIndex(
            collection_name, doc_id))

    def deleteChunkById(self, collection_name: str, chunk_id: str) -> None:
        self._delegate.deleteChunkById(collection_name, chunk_id)
        self._sync_keyword(chunk_id, lambda: self._keyword.deleteChunkById(
            collection_name, chunk_id))

    def deleteChunksByIds(self, collection_name: str, chunk_ids: list) -> None:
        self._delegate.deleteChunksByIds(collection_name, chunk_ids)
        self._sync_keyword(None, lambda: self._keyword.deleteChunksByIds(
            collection_name, chunk_ids))

    def _sync_keyword(self, doc_id: Optional[str], action) -> None:
        try:
            action()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("关键词索引同步失败，已跳过 docId=%s: %s", doc_id, exc)


def apply_graph_sync(bean, light_rag_client: Optional[LightRagClient]):
    """1:1 对应 GraphSyncVectorStorePostProcessor.postProcessAfterInitialization。"""
    if (isinstance(bean, VectorStoreService)
            and not isinstance(bean, GraphSyncingVectorStoreService)):
        if light_rag_client is not None:
            _logger.info("检测到图谱后端实现，向量写入将同步维护知识图谱")
            return GraphSyncingVectorStoreService(bean, light_rag_client)
    return bean


def apply_keyword_sync(bean, keyword_index_service: Optional[KeywordIndexService]):
    """1:1 对应 KeywordSyncVectorStorePostProcessor.postProcessAfterInitialization。"""
    if (isinstance(bean, VectorStoreService)
            and not isinstance(bean, KeywordSyncingVectorStoreService)):
        if keyword_index_service is not None:
            _logger.info("检测到关键词索引实现，向量写入将同步维护关键词索引")
            return KeywordSyncingVectorStoreService(bean, keyword_index_service)
    return bean


def build_vector_store_service(delegate, light_rag_client=None, keyword_index_service=None):
    """1:1 对应两个 PostProcessor 叠加：GraphSyncing(KeywordSyncing(真实实现))。"""
    svc = delegate
    svc = apply_keyword_sync(svc, keyword_index_service)
    svc = apply_graph_sync(svc, light_rag_client)
    return svc


__all__ = [
    "VectorStoreService", "LightRagClient", "KeywordIndexService",
    "GraphFileSource", "GraphSyncingVectorStoreService", "KeywordSyncingVectorStoreService",
    "apply_graph_sync", "apply_keyword_sync", "build_vector_store_service",
]
