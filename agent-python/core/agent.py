"""Agent 编排主链路：1:1 翻译 ragent rag.core 编排。

链路（严格对应 ragent RAGPipelineService / RetrievalService）：
  rewrite(问题改写+拆子问题)        ← core.rewrite.QueryRewriter
    → IntentResolver.resolve       ← core.intent.IntentResolver（并发 + capTotalIntents）
    → IntentGuidanceService        ← core.intent.guidance（歧义引导）
    → handleSystemOnly             ← 纯系统响应（SYSTEM意图直接响应）
    → MCP 工具调用                 ← core.mcp_client.MCPToolClient
    → RetrievalScopeResolver       ← core.retrieval.scope_resolver
    → MultiChannelRetrievalEngine  ← core.retrieval.engine
    → SourcesAssembler             ← core.source.SourcesAssembler
    → CitationContextEnricher      ← core.source.CitationContextEnricher
    → RAGPromptService             ← core.prompt.RAGPromptService
    → LLM.stream

保留原版关键语义：
  - 全链路降级：LLM 失败/解析失败不崩，回退到无意图/原问题/纯生成
  - 单轮会话即可（记忆存储留在 Java 侧 JdbcConversationMemoryStore）
  - 歧义引导：多意图得分接近时主动引导用户澄清
  - 纯系统响应：SYSTEM意图直接使用系统Prompt响应，不检索
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import asdict
from typing import AsyncIterator

from core.config import RAGConfigProperties, SearchChannelProperties
from core.llm import LLMClient
from core.intent import IntentResolver, NodeScore, SubQuestionIntent, IntentGuidanceService, IntentKind
from core.rewrite import QueryRewriter
from core.mcp_client import MCPToolClient
from core.prompt import (
    ContextFormatter,
    PromptContext,
    PromptPlan,
    RAGPromptService,
)
from core.retrieval.engine import MultiChannelRetrievalEngine
from core.retrieval.scope_resolver import RetrievalScope, RetrievalScopeResolver
from core.retrieval.types import RetrievedChunk, RetrievalBudget
from core.source import CitationContextEnricher, SourceRef, SourcesAssembler


_logger = logging.getLogger(__name__)


class Agent:
    def __init__(
        self,
        llm: LLMClient,
        intent_resolver: IntentResolver | None = None,
        rewriter: QueryRewriter | None = None,
        mcp: MCPToolClient | None = None,
        retriever: MultiChannelRetrievalEngine | None = None,
        prompt_service: RAGPromptService | None = None,
        scope_resolver: RetrievalScopeResolver | None = None,
        sources_assembler: SourcesAssembler | None = None,
        citation_enricher: CitationContextEnricher | None = None,
        doc_meta_provider: Any | None = None,
        active_collections: list[str] | None = None,
        rag_config: RAGConfigProperties | None = None,
        search_channel_config: SearchChannelProperties | None = None,
        guidance_service: IntentGuidanceService | None = None,
    ):
        self.llm = llm
        self.rag_config = rag_config or RAGConfigProperties.from_env()
        self.search_channel_config = search_channel_config or SearchChannelProperties.from_env()
        self.resolver = intent_resolver or IntentResolver(llm)
        self.rewriter = rewriter or QueryRewriter(llm)
        self.mcp = mcp or MCPToolClient()
        self.retriever = retriever
        self.doc_meta_provider = doc_meta_provider
        self._active_collections = list(active_collections if active_collections is not None else self.rag_config.active_collections)
        # 1:1 翻译 ragent RAGPromptService / RetrievalScopeResolver / SourcesAssembler / CitationContextEnricher
        self.prompt_service = prompt_service or RAGPromptService(
            citation_enabled=self.rag_config.citation_enabled,
        )
        self.scope_resolver = scope_resolver or RetrievalScopeResolver(
            active_collections=self._active_collections,
        )
        self.sources_assembler = sources_assembler or SourcesAssembler()
        self.citation_enricher = citation_enricher or CitationContextEnricher(
            citation_enabled=self.rag_config.citation_enabled,
        )
        # 1:1 翻译 ragent IntentGuidanceService（歧义引导）
        self.guidance_service = guidance_service or IntentGuidanceService()

    async def answer(self, question: str, scope: dict | None = None) -> AsyncIterator[dict]:
        """流式回答：yield dict（type+delta）。检索后自动注入引用编号与来源面板数据。

        yield 结构：{"type": "response", "delta": str} 或 {"type": "think", "delta": str}

        取消协议：1:1 翻译 ragent StreamCancellationHandle
          - 接受 scope["_cancelled"] = threading.Event
          - 编排主链路每步检查；流式生成阶段由 server.py 直接 raise asyncio.CancelledError 终止
        """
        scope = scope or {}
        knowledge_base_ids: list[str] = list(scope.get("knowledgeBaseIds") or [])
        history: list[dict] = list(scope.get("history") or [])
        cancelled: threading.Event | None = scope.get("_cancelled")

        def _check_cancel() -> None:
            if cancelled is not None and cancelled.is_set():
                raise asyncio.CancelledError()

        # 1) 改写 + 拆子问题（1:1 翻译 ragent QueryRewriteService）
        _check_cancel()
        sub_queries = await self.rewriter.rewrite(question)

        # 2) 意图解析：并发 + cap_total_intents（1:1 翻译 ragent IntentResolver.resolve）
        _check_cancel()
        sub_intents: list[SubQuestionIntent] = await self.resolver.resolve(sub_queries)
        mcp_intents, kb_intents = self.resolver.merge_intent_group(sub_intents)

        # 2.1) 歧义引导（1:1 翻译 ragent IntentGuidanceService.detectAmbiguity）
        # 当用户问题存在歧义时，直接输出引导提示并返回
        _check_cancel()
        rewritten_question = sub_queries[0] if sub_queries else question
        guidance_decision = self.guidance_service.detect_ambiguity(rewritten_question, sub_intents)
        if guidance_decision.is_prompt and guidance_decision.prompt:
            yield {"type": "response", "delta": guidance_decision.prompt}
            return

        # 2.2) 纯系统响应（1:1 翻译 ragent handleSystemOnly）
        # 当意图全部为SYSTEM类型时，直接使用系统Prompt响应，不进行检索
        _check_cancel()
        if self._is_system_only(sub_intents):
            custom_prompt = self._extract_system_prompt(sub_intents)
            async for token in self._stream_system_response(
                rewritten_question, history, custom_prompt
            ):
                yield token
            return

        # 3) MCP 工具调用（1:1 翻译 ragent McpService.call）
        mcp_records: list[dict] = []
        for si in sub_intents:
            _check_cancel()
            for s in si.node_scores:
                if s.node and s.node.kind.value == "MCP" and s.node.tool_id:
                    is_error, text = await self.mcp.call(s.node.tool_id, {"query": si.question})
                    mcp_records.append({
                        "question": si.question,
                        "tool_id": s.node.tool_id,
                        "result": text,
                        "is_error": is_error,
                    })

        # 4) KB 检索作用域：1:1 翻译 ragent RetrievalScopeResolver.resolve
        #    顶层一次解析（跨子查询取 topScore），同时 per_sub_query 拿到每个 sq 的 scope
        sub_query_to_scores = [(si.question, list(si.node_scores)) for si in sub_intents]
        kb_top_scope: RetrievalScope = self.scope_resolver.resolve(sub_query_to_scores)
        per_sq_scopes: list[tuple[str, RetrievalScope]] = self.scope_resolver.per_sub_query(
            sub_query_to_scores
        )

        # 用户显式 knowledge_base_ids 时，强制覆盖 scope.collections
        # 1:1 翻译 ragent user_explicit_knowledge_base_ids 分支
        if knowledge_base_ids:
            explicit = [c for c in knowledge_base_ids if c]
            if explicit:
                shared = RetrievalScope(
                    mode="INTENT",
                    collections=explicit,
                    supplement_collections=kb_top_scope.supplement_collections,
                    top_score=kb_top_scope.top_score,
                    node_ids=kb_top_scope.node_ids,
                )
                per_sq_scopes = [(sq, shared) for sq, _ in per_sq_scopes]

        # 5) KB 检索：按子查询的 scope 分批（修跨库混淆）
        kb_hits: list[RetrievedChunk] = []
        if self.retriever:
            for sq, scope_resolved in per_sq_scopes:
                _check_cancel()
                try:
                    context_top_k = self.search_channel_config.default_top_k
                    hits = await self.retriever.retrieve(
                        sq,
                        scope_resolved.collections or None,
                        RetrievalBudget(
                            recall_budget=self.search_channel_config.resolve_recall_budget(context_top_k),
                            candidate_limit=self.search_channel_config.fusion.rerank_candidate_limit,
                            context_top_k=context_top_k,
                        ),
                    )
                    kb_hits.extend(hits)
                except Exception as e:  # noqa: BLE001
                    _logger.warning("chat retrieve failed: %s", e)
                    # 检索失败降级为空，不阻断生成

        # 6) 来源装配（1:1 翻译 ragent SourcesAssembler.assemble）
        source_refs: list[SourceRef] = self.sources_assembler.assemble(kb_hits, self.doc_meta_provider)
        scope["_sources"] = [asdict(s) for s in source_refs]

        # 7) Prompt 编排（1:1 翻译 ragent RAGPromptService.plan + buildStructuredMessages）
        # 7a) 上下文格式化：1:1 翻译 ragent DefaultContextFormatter.formatKbContext / formatMcpContext
        formatter = self.prompt_service.formatter
        eligible_ids = {s.node.id for s in (mcp_intents + kb_intents) if s.node and s.node.id}
        kb_context = formatter.format_kb_context(
            kb_intents, eligible_ids, kb_hits, self.search_channel_config.default_top_k
        ) if kb_hits else ""
        # 1:1 翻译 ragent formatMcpContext 签名：Map<toolId, List<CallToolResult>>
        tool_results: dict[str, list[dict]] = {}
        for rec in mcp_records:
            tool_id = rec.get("tool_id", "")
            if tool_id:
                tool_results.setdefault(tool_id, []).append(rec)
        mcp_context = formatter.format_mcp_context(
            tool_results, list(mcp_intents)
        ) if tool_results else ""

        prompt_ctx = PromptContext(
            question=question,
            kb_context=kb_context,
            mcp_context=mcp_context,
            mcp_intents=list(mcp_intents),
            kb_intents=list(kb_intents),
            eligible_intent_ids=eligible_ids,
        )
        plan: PromptPlan = self.prompt_service.plan(prompt_ctx)
        if plan.base_template is None:
            # EMPTY 场景：1:1 对应 ragent RAGPromptService.plan 抛 IllegalStateException
            yield {"type": "response", "delta": "当前可用信息不足以回答该问题。"}
            return

        # 7b) CitationContextEnricher 注入 ref="N"（1:1 翻译 ragent CitationContextEnricher.enrich）
        if source_refs and self.citation_enricher.citation_enabled:
            prompt_ctx.kb_context = self.citation_enricher.enrich(
                prompt_ctx.kb_context, source_refs
            )

        # 7c) buildSystemPrompt + buildStructuredMessages
        structured = self.prompt_service.build_structured_messages(plan, prompt_ctx)

        # 8) 注入会话历史
        messages: list[dict] = list(structured["system"])
        for h in history:
            role = h.get("role")
            content = h.get("content")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.extend(structured["user"])

        try:
            async for token in self.llm.stream(messages):
                yield token
        except Exception as e:  # noqa: BLE001
            yield {"type": "response", "delta": f"\n[生成失败: {e}]"}

    def _is_system_only(self, sub_intents: list[SubQuestionIntent]) -> bool:
        """1:1 翻译 ragent IntentResolver.isSystemOnly。

        判断意图是否全部为SYSTEM类型（纯系统响应，不需要检索）。
        """
        if not sub_intents:
            return False
        for si in sub_intents:
            if not si.node_scores:
                return False
            for ns in si.node_scores:
                if ns.node is None or ns.node.kind != IntentKind.SYSTEM:
                    return False
        return True

    def _extract_system_prompt(self, sub_intents: list[SubQuestionIntent]) -> str | None:
        """1:1 翻译 ragent handleSystemOnly 中提取自定义Prompt逻辑。

        从SYSTEM意图节点中提取自定义Prompt模板。
        """
        for si in sub_intents:
            for ns in si.node_scores:
                if ns.node and ns.node.prompt_template:
                    return ns.node.prompt_template
        return None

    async def _stream_system_response(
        self,
        question: str,
        history: list[dict],
        custom_prompt: str | None = None,
    ) -> AsyncIterator[dict]:
        """1:1 翻译 ragent streamSystemResponse。

        纯系统响应：直接使用系统Prompt + 会话历史 + 用户问题，不进行检索。
        """
        system_prompt = custom_prompt or self._get_default_system_prompt()
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        # 注入会话历史
        for h in history:
            role = h.get("role")
            content = h.get("content")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": question})

        try:
            async for token in self.llm.stream(messages):
                yield token
        except Exception as e:  # noqa: BLE001
            yield {"type": "response", "delta": f"\n[生成失败: {e}]"}

    def _get_default_system_prompt(self) -> str:
        """获取默认系统Prompt。"""
        from core.agent_profile import AgentProfile
        try:
            profile = AgentProfile.get_default()
            if profile and profile.system_prompt:
                return profile.system_prompt
        except Exception:
            pass
        return "你是一个智能助手，请根据用户问题提供准确、有帮助的回答。"

    async def generate_title(self, question: str) -> str:
        """生成会话标题（替代 Java 侧本地 LLM 标题生成）。失败降级为问题截断。"""
        try:
            resp = await self.llm.chat([
                {"role": "system", "content": "用不超过 20 个字概括用户问题，只输出标题本身，不要标点结尾、不要解释。"},
                {"role": "user", "content": question},
            ], max_tokens=32)
            title = (resp or "").strip().strip("。").strip("\n")
            if title:
                return title[:40]
        except Exception:
            pass
        return question[:30] if question else "新对话"

    async def recommend_questions(self, question: str, answer: str, chunks: list[dict]) -> list[str]:
        """生成推荐追问（替代 Java 侧本地 LLM 推荐）。失败返回空。"""
        ctx = "\n".join(f"- {c.get('doc_name', '')}: {c.get('text', '')}" for c in (chunks or [])[:5])
        prompt = (
            "基于下面的问答，提出 3 个用户可能想继续追问的问题。每行一个，不要编号，不要解释。\n"
            f"用户问题：{question}\n回答：{answer}\n参考文档：\n{ctx}"
        )
        try:
            resp = await self.llm.chat([
                {"role": "system", "content": "你是追问生成器，只输出问题列表。"},
                {"role": "user", "content": prompt},
            ], max_tokens=200)
            lines = [ln.strip("0123456789.、- ").strip() for ln in (resp or "").splitlines()]
            return [ln for ln in lines if ln][:3]
        except Exception:
            return []

    async def summarize(self, messages: list[dict], existing_summary: str) -> str:
        """生成会话记忆摘要（替代 Java 侧本地 LLM 摘要）。失败返回原摘要。"""
        conv = "\n".join(f"{m.get('role', '')}: {m.get('content', '')}" for m in messages)
        base = f"已有摘要：{existing_summary}\n" if existing_summary else ""
        prompt = f"{base}请基于以下对话生成一段简洁的要点摘要（不超过 200 字），用于后续记忆压缩：\n{conv}"
        try:
            resp = await self.llm.chat([
                {"role": "system", "content": "你是会话摘要器，只输出摘要文本。"},
                {"role": "user", "content": prompt},
            ], max_tokens=300)
            return (resp or "").strip()
        except Exception:
            return existing_summary
