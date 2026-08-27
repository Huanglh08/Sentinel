"""引用上下文富化器：1:1 翻译 ragent rag.core.source.CitationContextEnricher。

为已格式化的 KB 上下文注入请求级引用编号：
  上下文格式化阶段只写入内部 data-ragent-doc-id；来源装配完成后再依据 SourceRef.index
  替换为模型可见的 ref="N"。这样 Prompt / SSE / 落库和前端始终复用同一份来源编号，
  同时不把内部文档 ID 暴露给模型。

引用开关关闭时按"无来源"处理：只抹掉内部锚点、不注入编号。无论开关如何，上下文都必须
先过这一道，内部 docId 才不会漏进模型可见文本。
"""
from __future__ import annotations

import re
from typing import Iterable

from core.source.sources_assembler import SourceRef


# 1:1 翻译 ragent CitationContextEnricher.CONTENT_TAG：
# private static final Pattern CONTENT_TAG = Pattern.compile("(?m)^<content([^>]*) data-ragent-doc-id=\"([^\"]*)\">$");
_CONTENT_TAG_RE = re.compile(r'(?m)^<content([^>]*) data-ragent-doc-id="([^"]*)">$')


def _replacement_literal(s: str) -> str:
    """1:1 翻译 ragent Matcher.quoteReplacement：转义 \\ 与 $，避免 appendReplacement 解析为反向引用 / 捕获组。"""
    return s.replace("\\", "\\\\").replace("$", "\\$")


class CitationContextEnricher:
    """1:1 翻译 ragent rag.core.source.CitationContextEnricher。"""

    def __init__(self, citation_enabled: bool = True) -> None:
        self.citation_enabled = citation_enabled

    def enrich(self, kb_context: str | None, sources: list[SourceRef]) -> str:
        """严格对应 ragent CitationContextEnricher.enrich(kbContext, sources)。"""
        if not kb_context:
            return kb_context or ""

        index_by_doc_id = self._index_by_doc_id(sources) if self.citation_enabled else {}
        # 1:1 翻译 Java Matcher.appendReplacement + appendTail 循环
        matcher = _CONTENT_TAG_RE
        result_parts: list[str] = []
        last_end = 0
        for m in matcher.finditer(kb_context):
            result_parts.append(kb_context[last_end:m.start()])
            attrs = m.group(1)
            doc_id = m.group(2)
            index = index_by_doc_id.get(doc_id)
            replacement = (
                f'<content{attrs} ref="{index}">' if index is not None
                else f'<content{attrs}>'
            )
            result_parts.append(replacement)
            last_end = m.end()
        result_parts.append(kb_context[last_end:])
        return "".join(result_parts)

    @staticmethod
    def _index_by_doc_id(sources: Iterable[SourceRef]) -> dict[str, int]:
        """1:1 翻译 ragent CitationContextEnricher.indexByDocId。"""
        out: dict[str, int] = {}
        for s in sources:
            if not s or not s.doc_id or s.index is None:
                continue
            out.setdefault(s.doc_id, s.index)
        return out
