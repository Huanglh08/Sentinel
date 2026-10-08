"""回答来源装配器：严格对应 ragent rag.core.source.SourcesAssembler。

把检索片段（KB 命中）按 doc_id 去重、按相关度赋号、产出文档级来源列表。
该列表既用于 SSE 下发/面板展示，也作为行内角标的唯一编号源。

与 ragent 的差异：
  - ragent 走 documentMapper.selectBatchIds 回表补 doc_name / sourceType / fileType / url
  - 当前 Python 版没有 document 实体表：直接用 RetrievedChunk 携带的 doc_name（检索后端已
    保证 doc_name 字段非空，行为等价 ragent MetadataEnrichmentPostProcessor 之后的形态）
"""
from __future__ import annotations
import logging

from dataclasses import dataclass, field
from typing import Any

from core.retrieval.types import RetrievedChunk

_logger = logging.getLogger(__name__)


EXCERPT_MAX_LENGTH = 100  # 严格对齐 ragent SourcesAssembler.EXCERPT_MAX_LENGTH


@dataclass
class SourceRef:
    """严格对应 ragent framework.convention.SourceRef。"""

    index: int
    doc_id: str
    doc_name: str
    excerpt: str
    source_type: str | None = None
    file_type: str | None = None
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "doc_id": self.doc_id,
            "doc_name": self.doc_name,
            "excerpt": self.excerpt,
            "source_type": self.source_type,
            "file_type": self.file_type,
            "url": self.url,
        }


class SourcesAssembler:
    """严格对应 ragent rag.core.source.SourcesAssembler。"""

    def assemble(
        self,
        chunks: list[RetrievedChunk],
        doc_meta_provider: Any | None = None,
    ) -> list[SourceRef]:
        """chunks: 检索片段（多通道融合 + 去重 + Rerank 之后的有序列表）。

        流程：
          1. 按 doc_id 归并，保留最高分片段
          2. 按 score 降序（doc_id 作为 tie-breaker，保证稳定）
          3. 分配 1-based index
          4. 摘录文本截断到 EXCERPT_MAX_LENGTH
        """
        if not chunks:
            return []

        # 1) 按 doc_id 归并，保留最高分片段
        best_by_doc: dict[str, RetrievedChunk] = {}
        for chunk in chunks:
            if not chunk or not chunk.doc_id:
                continue
            existing = best_by_doc.get(chunk.doc_id)
            if existing is None or (chunk.score or 0) > (existing.score or 0):
                best_by_doc[chunk.doc_id] = chunk

        if not best_by_doc:
            return []

        # 2) 按 score 降序，doc_id 作为 tie-breaker
        ordered = sorted(
            best_by_doc.values(),
            key=lambda c: (-(c.score or 0), c.doc_id or ""),
        )

        # 3) 补 doc_name（doc_meta_provider 可选；未提供时直接用 chunk.doc_name）
        sources: list[SourceRef] = []
        for idx, chunk in enumerate(ordered, start=1):
            doc_name = chunk.doc_name or chunk.doc_id or ""
            excerpt = self._trim_excerpt(chunk.text or "")
            meta = self._resolve_doc_meta(doc_meta_provider, chunk.doc_id) if doc_meta_provider else {}
            sources.append(SourceRef(
                index=idx,
                doc_id=chunk.doc_id or "",
                doc_name=meta.get("doc_name") or doc_name,
                excerpt=excerpt,
                source_type=meta.get("source_type") or meta.get("sourceType") or chunk.source_type,
                file_type=meta.get("file_type") or meta.get("fileType") or chunk.file_type,
                url=meta.get("url") or chunk.url,
            ))
        return sources

    @staticmethod
    def _trim_excerpt(text: str) -> str:
        if len(text) <= EXCERPT_MAX_LENGTH:
            return text.strip()
        return text[:EXCERPT_MAX_LENGTH].rstrip()

    @staticmethod
    def _resolve_doc_meta(provider: Any, doc_id: str | None) -> dict[str, Any]:
        if not doc_id:
            return {}
        # doc_meta_provider 可调用 .get_doc(doc_id) -> dict{...}
        try:
            getter = getattr(provider, "get_doc", None)
            if callable(getter):
                v = getter(doc_id)
                if isinstance(v, dict):
                    return v
        except Exception as e:  # noqa: BLE001
            _logger.debug("解析文档元信息失败: %s", e)
            return {}
        return {}
