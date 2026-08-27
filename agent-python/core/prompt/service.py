"""RAG 提示词编排服务：1:1 翻译 ragent rag.core.prompt.RAGPromptService。

核心行为（严格对应 ragent RAGPromptService.planPrompt）：
  - hasMcp=false, hasKb=true  → planKbOnly
  - hasMcp=true,  hasKb=false → planMcpOnly（mcp_intents.size==1 且 promptTemplate 非空 → 整段替换）
  - hasMcp=true,  hasKb=true  → planMixed
  - 均为 false                 → EMPTY（plan.base_template=None，调用方处理）
  - 全部 → 抛 IllegalStateException（ragent 行为）；Python 用返回 base_template=None 替代

模板选择规则（planKbOnly 内）：
  - planPrompt(kb_intents, eligible_ids)：
    - 0 候选 → AgentPromptResolver.resolve(slot) 基线
    - 1 候选且 promptTemplate 非空 → 整段替换基线
    - 2+ 候选 → 基线（ragent: 多意图走基础模板）
"""
from __future__ import annotations

from typing import Any

from core.intent import NodeScore
from core.prompt.context import PromptBuildPlan, PromptContext, PromptPlan
from core.prompt.formatter import ContextFormatter
from core.prompt.prompt_resolver import AgentPromptResolver, AgentPromptSlot
from core.prompt.scene import PromptScene
from core.prompt.template_loader import cleanup_prompt


def _pick_scene(has_mcp: bool, has_kb: bool) -> PromptScene:
    if has_mcp and has_kb:
        return PromptScene.MIXED
    if has_mcp:
        return PromptScene.MCP_ONLY
    if has_kb:
        return PromptScene.KB_ONLY
    return PromptScene.EMPTY


def _slot_for(scene: PromptScene) -> str:
    if scene == PromptScene.MCP_ONLY:
        return AgentPromptSlot.MCP_ANSWER
    if scene == PromptScene.MIXED:
        return AgentPromptSlot.MIXED_ANSWER
    return AgentPromptSlot.KB_ANSWER


def _eligible_retained(
    intents: list[NodeScore],
    eligible_ids: set[str],
) -> list[NodeScore]:
    """1:1 翻译 ragent RAGPromptService.retainedIntents。"""
    if not eligible_ids:
        return list(intents)
    return [s for s in intents if s.node and s.node.id in eligible_ids]


def _pick_intent_template(
    retained: list[NodeScore],
    base_template: str,
) -> str:
    """1:1 翻译 ragent RAGPromptService.pickAgentPromptTemplate：
      - 0 候选 → 基础模板
      - 1 候选且 promptTemplate 非空 → 整体替换
      - 2+ 候选 → 基础模板
    """
    if len(retained) == 1:
        tpl = retained[0].node.prompt_template
        if tpl and tpl.strip():
            return tpl
    return base_template


class RAGPromptService:
    """1:1 翻译 ragent rag.core.prompt.RAGPromptService。"""

    def __init__(
        self,
        resolver: AgentPromptResolver | None = None,
        formatter: ContextFormatter | None = None,
        citation_enabled: bool = False,  # ragent 默认 false
    ) -> None:
        self.resolver = resolver or AgentPromptResolver()
        self.formatter = formatter or ContextFormatter()
        self.citation_enabled = citation_enabled

    def _default_template(self, scene: PromptScene) -> str:
        """1:1 翻译 ragent RAGPromptService.defaultTemplate。"""
        if scene == PromptScene.MCP_ONLY:
            return self.resolver.resolve(AgentPromptSlot.MCP_ANSWER)
        if scene == PromptScene.MIXED:
            return self.resolver.resolve(AgentPromptSlot.MIXED_ANSWER)
        if scene == PromptScene.KB_ONLY:
            return self.resolver.resolve(AgentPromptSlot.KB_ANSWER)
        return ""

    def _plan_prompt(
        self,
        intents: list[NodeScore],
        eligible_ids: set[str],
        scene: PromptScene,
    ) -> PromptPlan:
        """1:1 翻译 ragent RAGPromptService.planPrompt(intents, eligibleIntentIds, scene)。"""
        retained = _eligible_retained(intents, eligible_ids)
        base = self._default_template(scene)
        chosen = _pick_intent_template(retained, base)
        return PromptPlan(retained_intents=retained, base_template=chosen)

    def plan(self, context: PromptContext) -> PromptPlan:
        """1:1 翻译 ragent RAGPromptService.planPrompt：三分支 + EMPTY 兜底。"""
        scene = _pick_scene(context.has_mcp(), context.has_kb())
        if scene == PromptScene.EMPTY:
            return PromptPlan(retained_intents=[], base_template=None)
        if scene == PromptScene.MCP_ONLY:
            # 1:1 翻译 ragent RAGPromptService.planMcpOnly：单意图且 promptTemplate 非空 → 整段替换
            intents = context.mcp_intents
            if len(intents) == 1 and intents[0].node and intents[0].node.prompt_template:
                tpl = intents[0].node.prompt_template.strip()
                if tpl:
                    return PromptPlan(retained_intents=list(intents), base_template=tpl)
            # 否则走基础模板
            base = self._default_template(scene)
            return PromptPlan(retained_intents=list(intents), base_template=base)
        if scene == PromptScene.KB_ONLY:
            return self._plan_prompt(context.kb_intents, context.eligible_intent_ids, scene)
        # MIXED：KB 走 planPrompt；MCP 不替换模板
        return self._plan_prompt(context.kb_intents, context.eligible_intent_ids, scene)

    def build_system_prompt(self, plan: PromptPlan) -> str:
        """保留旧签名：默认按 has_kb=True 行为追加引用规则（向后兼容）。"""
        return self.build_system_prompt_for(plan, has_kb=True)

    def build_system_prompt_for(self, plan: PromptPlan, has_kb: bool) -> str:
        """1:1 翻译 ragent RAGPromptService.buildSystemPrompt：
          - hasMcp && !hasKb → 不追加引用规则
          - hasKb             → 追加（受 citation_enabled 控制）
        """
        if plan.base_template is None:
            return ""
        body = cleanup_prompt(plan.base_template)
        if has_kb and self.citation_enabled:
            rules = self.formatter.citation_rules()
            if rules:
                body = body + "\n\n" + rules
        return body

    def build_structured_messages(
        self,
        plan: PromptPlan,
        context: PromptContext,
    ) -> dict[str, list[dict[str, str]]]:
        """1:1 翻译 ragent RAGPromptService.buildStructuredMessages。

        system：base_template（已含引用规则开关）
        user：evidence body（KB + MCP）+ 单/多问题
        """
        if plan.base_template is None:
            raise ValueError("RAGPromptService.buildStructuredMessages 不接受空场景 plan")
        has_kb = bool(context.kb_context and context.kb_context.strip())
        system_prompt = self.build_system_prompt_for(plan, has_kb=has_kb)
        question_section = self.formatter.format_question(context.question, None)

        user_parts: list[str] = []
        if context.kb_context:
            user_parts.append(self.formatter.format_kb_evidence(context.kb_context))
        if context.mcp_context:
            user_parts.append(self.formatter.format_mcp_evidence(context.mcp_context))
        user_parts.append(question_section)
        # 1:1 翻译 ragent mergeEvidenceAndQuestion：先判空再合并，避免连续空行
        user_body = "\n\n".join(p for p in user_parts if p)

        return {
            "system": [{"role": "system", "content": system_prompt}],
            "user": [{"role": "user", "content": user_body}],
        }
