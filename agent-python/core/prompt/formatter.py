"""上下文格式化器：1:1 翻译 ragent rag.core.prompt.ContextFormatter + DefaultContextFormatter。

负责把 <kb-section> / <mcp-section> / <kb-doc-block> / <single-question> / <multi-questions> /
<sub-question-kb-wrapper> / <sub-question-mcp-wrapper> / <summary-wrapper> 拼接成最终的
context-format 字符串。

- format_kb(docs, summary=None)             ← 旧入口（向后兼容）
- format_kb_context(kb_intents, eligible_intent_ids, reranked_chunks, context_top_k, summary=None)
                                                 ← 1:1 翻译 ragent DefaultContextFormatter.formatKbContext
- format_mcp(records, summary=None)         ← 旧入口（向后兼容）
- format_mcp_context(tool_results, mcp_intents) ← 1:1 翻译 ragent DefaultContextFormatter.formatMcpContext
- format_question(question)                 ← 单/多问题片段
"""
from __future__ import annotations

import re
from typing import Iterable

from core.intent import NodeScore
from core.prompt.scene import PromptScene
from core.prompt.template_loader import PromptTemplateLoader
from core.retrieval.types import RetrievedChunk


_CONTEXT_FORMAT_PATH = "context-format.st"
_ANSWER_CITATION_RULES_PATH = "answer-citation-rules.st"

# 1:1 翻译 ragent DefaultContextFormatter.sanitizeAttribute：清洗属性值里会破坏伪标签结构的字符
_ATTR_SANITIZE_RE = re.compile(r'["<>]')

# 1:1 翻译 ragent RetrievedChunkKey.of：去重 key（collection + docId + chunkIndex + id 拼接）
def _chunk_key(chunk: RetrievedChunk) -> str:
    parts: list[str] = []
    for attr in ("collection", "doc_id", "chunk_index", "id"):
        v = getattr(chunk, attr, None)
        parts.append(str(v) if v is not None else "")
    return "|".join(parts)


class ContextFormatter:
    """1:1 翻译 ragent rag.core.prompt.DefaultContextFormatter。"""

    def __init__(self, loader: PromptTemplateLoader | None = None) -> None:
        self.loader = loader or PromptTemplateLoader()

    # ---- 顶层入口：KB 上下文（1:1 翻译 ragent formatKbContext） ----
    def format_kb_context(
        self,
        kb_intents: list[NodeScore],
        eligible_intent_ids: set[str],
        reranked_chunks: list[RetrievedChunk],
        context_top_k: int,
        summary: str | None = None,
    ) -> str:
        """严格对应 ragent DefaultContextFormatter.formatKbContext。

        分支：
          - 0 合格意图 → formatChunksWithoutIntent
          - 1 合格意图 → formatSingleIntentContext
          - ≥2 合格意图 → formatMultiIntentContext
        """
        if not reranked_chunks:
            return ""
        eligible = self._eligible_intents(kb_intents, eligible_intent_ids)
        body = self._dispatch_kb(eligible, reranked_chunks, context_top_k)
        if summary:
            body = body + "\n" + self.loader.render_section(
                _CONTEXT_FORMAT_PATH, "summary-wrapper", {"content": summary}
            )
        return body

    def _dispatch_kb(
        self,
        eligible: list[NodeScore],
        reranked_chunks: list[RetrievedChunk],
        context_top_k: int,
    ) -> str:
        if not eligible:
            return self._format_chunks_without_intent(reranked_chunks, context_top_k)
        if len(eligible) > 1:
            return self._format_multi_intent_context(eligible, reranked_chunks, context_top_k)
        return self._format_single_intent_context(eligible[0], reranked_chunks, context_top_k)

    def _eligible_intents(
        self,
        kb_intents: list[NodeScore],
        eligible_intent_ids: set[str],
    ) -> list[NodeScore]:
        """1:1 翻译 ragent DefaultContextFormatter.eligibleIntents。"""
        if not kb_intents or not eligible_intent_ids:
            return []
        return [
            ns for ns in kb_intents
            if ns and ns.node and ns.node.id in eligible_intent_ids
        ]

    def _format_single_intent_context(
        self,
        node_score: NodeScore,
        reranked_chunks: list[RetrievedChunk],
        top_k: int,
    ) -> str:
        """1:1 翻译 ragent DefaultContextFormatter.formatSingleIntentContext。"""
        snippet = (node_score.node.prompt_snippet or "").strip() if node_score.node else ""
        doc_blocks = self._render_chunks_grouped_by_doc(
            self._distinct_chunks(reranked_chunks), top_k
        )
        return self._render_kb_section(self._render_snippet_rules(snippet), doc_blocks)

    def _format_multi_intent_context(
        self,
        kb_intents: list[NodeScore],
        reranked_chunks: list[RetrievedChunk],
        top_k: int,
    ) -> str:
        """1:1 翻译 ragent DefaultContextFormatter.formatMultiIntentContext：
          1. 合并所有意图的 prompt_snippet（去空 + 去重 + 编号 1./2./...）
          2. 合并所有意图的 chunk（按 chunk key 去重、保持相关性顺序）
          3. render kb-section
        """
        # 1) snippets
        snippets: list[str] = []
        for ns in kb_intents:
            if not ns.node:
                continue
            s = (ns.node.prompt_snippet or "").strip()
            if s and s not in snippets:
                snippets.append(s)
        snippet_section = ""
        if snippets:
            numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(snippets))
            snippet_section = self._render_snippet_rules(numbered)

        # 2) chunks
        all_chunks = self._distinct_chunks(reranked_chunks)
        if not all_chunks:
            return snippet_section
        doc_blocks = self._render_chunks_grouped_by_doc(all_chunks, top_k)
        return self._render_kb_section(snippet_section, doc_blocks)

    def _format_chunks_without_intent(
        self,
        reranked_chunks: list[RetrievedChunk],
        top_k: int,
    ) -> str:
        """1:1 翻译 ragent DefaultContextFormatter.formatChunksWithoutIntent。"""
        chunks = self._distinct_chunks(reranked_chunks)
        if not chunks:
            return ""
        doc_blocks = self._render_chunks_grouped_by_doc(chunks, top_k)
        return self._render_kb_section("", doc_blocks)

    def _distinct_chunks(self, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
        """1:1 翻译 ragent DefaultContextFormatter.distinctChunks：按 chunk key 去重，保留首次出现顺序。"""
        seen: dict[str, RetrievedChunk] = {}
        for c in chunks:
            seen.setdefault(_chunk_key(c), c)
        return list(seen.values())

    def _render_chunks_grouped_by_doc(
        self,
        chunks: list[RetrievedChunk],
        top_k: int,
    ) -> str:
        """1:1 翻译 ragent DefaultContextFormatter.renderChunksGroupedByDoc：
          - 限 top_k
          - 按 docId 分组（LinkedHashMap 保首次出现顺序 = 文档间相关性排序）
          - docId 为空的块各自单独成组
        """
        limit = top_k if top_k > 0 else None
        limited = chunks[:limit] if limit is not None else chunks
        if not limited:
            return ""

        groups: dict[str, list[RetrievedChunk]] = {}
        anonymous_seq = 0
        for chunk in limited:
            doc_id = (chunk.doc_id or "").strip()
            key = doc_id if doc_id else f"__nodoc__{anonymous_seq}"
            if not doc_id:
                anonymous_seq += 1
            groups.setdefault(key, []).append(chunk)
        return "\n".join(self._render_doc_block(g) for g in groups.values())

    def _render_doc_block(self, group: list[RetrievedChunk]) -> str:
        """1:1 翻译 ragent DefaultContextFormatter.renderDocBlock：组内按 chunk_index 升序拼接。"""
        ordered = sorted(
            group,
            key=lambda c: (getattr(c, "chunk_index", None) is None, getattr(c, "chunk_index", None) or 0, c.id or ""),
        )
        chunks_text = self._join_doc_body(ordered)
        doc_id = self._sanitize_attribute(self._resolve_doc_id(group))
        if doc_id:
            return self.loader.render_section(
                _CONTEXT_FORMAT_PATH, "kb-doc-block",
                {"doc_id": doc_id, "chunks": chunks_text},
            )
        return self.loader.render_section(
            _CONTEXT_FORMAT_PATH, "kb-doc-block-anonymous",
            {"chunks": chunks_text},
        )

    @staticmethod
    def _join_doc_body(ordered: list[RetrievedChunk]) -> str:
        """1:1 翻译 ragent DefaultContextFormatter.joinDocBody。"""
        return "\n".join((c.text or "").strip() for c in ordered if (c.text or "").strip())

    @staticmethod
    def _sanitize_attribute(value: str | None) -> str:
        """1:1 翻译 ragent DefaultContextFormatter.sanitizeAttribute。"""
        if not value:
            return ""
        return _ATTR_SANITIZE_RE.sub("", value).strip()

    @staticmethod
    def _resolve_doc_id(group: list[RetrievedChunk]) -> str:
        """1:1 翻译 ragent DefaultContextFormatter.resolveDocId。"""
        for c in group:
            if (c.doc_id or "").strip():
                return c.doc_id
        return ""

    def _render_kb_section(self, snippet_section: str, doc_blocks: str) -> str:
        """1:1 翻译 ragent DefaultContextFormatter.renderKbSection。"""
        return self.loader.render_section(
            _CONTEXT_FORMAT_PATH, "kb-section",
            {"snippet_section": snippet_section, "doc_blocks": doc_blocks},
        )

    def _render_snippet_rules(self, snippet: str) -> str:
        """1:1 翻译 ragent DefaultContextFormatter.renderSnippetRules。"""
        if not (snippet or "").strip():
            return ""
        return self.loader.render_section(
            _CONTEXT_FORMAT_PATH, "snippet-rules", {"rules": snippet.strip()}
        )

    # ---- 顶层入口：MCP 上下文（1:1 翻译 ragent formatMcpContext） ----
    def format_mcp_context(
        self,
        tool_results: dict[str, list[dict]],
        mcp_intents: list[NodeScore],
    ) -> str:
        """严格对应 ragent DefaultContextFormatter.formatMcpContext。

        tool_results: {tool_id: [{"question": str, "result": str}, ...]}
        mcp_intents:  按 tool_id → IntentNode 索引
        """
        if not tool_results:
            return ""
        if not mcp_intents:
            # 无意图 → 合并所有结果文本
            all_records: list[dict] = []
            for recs in tool_results.values():
                all_records.extend(recs)
            return self._format_mcp_records(all_records)

        # toolId → IntentNode（保首次出现顺序）
        tool_to_node: dict[str, object] = {}
        for ns in mcp_intents:
            if not ns.node or not (ns.node.tool_id or "").strip():
                continue
            tool_to_node.setdefault(ns.node.tool_id, ns.node)

        blocks: list[str] = []
        for tool_id, node in tool_to_node.items():
            results = tool_results.get(tool_id, [])
            if not results:
                continue
            snippet = (getattr(node, "prompt_snippet", None) or "").strip()
            body = self._merge_records_to_text(results)
            if not body:
                continue
            # 每个工具一组：<tool-data><rules>...</rules><data>...body...</data></tool-data>
            rules_part = (
                f"<rules>{snippet}</rules>\n" if snippet else ""
            )
            blocks.append(f"<tool-data>\n{rules_part}<data>\n{body}\n</data>\n</tool-data>")
        return "\n".join(blocks)

    @staticmethod
    def _merge_records_to_text(records: list[dict]) -> str:
        """1:1 翻译 ragent DefaultContextFormatter.mergeResultsToText。"""
        parts: list[str] = []
        for r in records:
            question = (r.get("question") or "").strip()
            result = (r.get("result") or "").strip()
            if question:
                parts.append(f"Q: {question}")
            if result:
                parts.append(result)
        return "\n".join(parts)

    @staticmethod
    def _format_mcp_records(records: list[dict]) -> str:
        body = ContextFormatter._merge_records_to_text(records)
        if not body:
            return ""
        return f"<tool-data>\n<data>\n{body}\n</data>\n</tool-data>"

    # ---- 旧入口 format_kb（向后兼容：按 docs 列表渲染） ----
    def format_kb(self, docs: list[dict], summary: str | None = None) -> str:
        """向后兼容：直接按传入的 docs 列表渲染，不再走 formatKbContext 分支。"""
        body = self._render_doc_blocks(docs)
        if summary:
            body = body + "\n" + self.loader.render_section(
                _CONTEXT_FORMAT_PATH, "summary-wrapper", {"content": summary}
            )
        return self.format_kb_evidence(body)

    def _render_doc_blocks(self, docs: list[dict]) -> str:
        if not docs:
            return ""
        blocks: list[str] = []
        for d in docs:
            doc_id = d.get("doc_id")
            chunks = d.get("chunks") or []
            chunk_text = "\n\n".join(c.get("content", "") for c in chunks if c.get("content"))
            if not chunk_text:
                continue
            if doc_id:
                blocks.append(self.loader.render_section(
                    _CONTEXT_FORMAT_PATH, "kb-doc-block",
                    {"doc_id": doc_id, "chunks": chunk_text},
                ))
            else:
                blocks.append(self.loader.render_section(
                    _CONTEXT_FORMAT_PATH, "kb-doc-block-anonymous",
                    {"chunks": chunk_text},
                ))
        return "\n".join(blocks)

    # ---- 旧入口 format_mcp（向后兼容） ----
    def format_mcp(self, records: list[dict], summary: str | None = None) -> str:
        body_chunks: list[str] = []
        for idx, rec in enumerate(records, start=1):
            body_chunks.append(self.loader.render_section(
                _CONTEXT_FORMAT_PATH, "sub-question-mcp-wrapper",
                {"index": idx, "question": rec.get("question", ""), "context": rec.get("result", "")},
            ))
        body = "\n".join(body_chunks)
        if summary:
            body = body + "\n" + self.loader.render_section(
                _CONTEXT_FORMAT_PATH, "summary-wrapper", {"content": summary}
            )
        return self.format_mcp_evidence(body)

    # ---- Question 单/多问题片段 ----
    def format_question(self, question: str, sub_questions: list[str] | None = None) -> str:
        if sub_questions and len(sub_questions) > 1:
            lines = "\n".join(
                f"{i + 1}. {q}" for i, q in enumerate(sub_questions)
            )
            return self.loader.render_section(
                _CONTEXT_FORMAT_PATH, "multi-questions", {"questions": lines}
            )
        return self.loader.render_section(
            _CONTEXT_FORMAT_PATH, "single-question", {"question": question}
        )

    # ---- 引用规则（独立 section） ----
    def citation_rules(self) -> str:
        return self.loader.render(_ANSWER_CITATION_RULES_PATH)

    # ---- Evidence body ----
    def format_kb_evidence(self, kb_context: str) -> str:
        return self.loader.render_section(
            _CONTEXT_FORMAT_PATH, "kb-evidence", {"body": kb_context}
        )

    def format_mcp_evidence(self, mcp_context: str) -> str:
        return self.loader.render_section(
            _CONTEXT_FORMAT_PATH, "mcp-evidence", {"body": mcp_context}
        )
