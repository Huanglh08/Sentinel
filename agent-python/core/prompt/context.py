"""Prompt 构建上下文与计划：严格对应 ragent rag.core.prompt.PromptContext / PromptBuildPlan / PromptPlan。

- PromptContext：一次 RAG 请求中用于组装提示词的全部输入数据
- PromptBuildPlan：实际传给 LLM 的 system + user 渲染数据
- PromptPlan：模板选择阶段产物（retained_intents + 选用的基模板）
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.intent import NodeScore
from core.prompt.scene import PromptScene


@dataclass
class PromptContext:
    """严格对应 ragent rag.core.prompt.PromptContext。

    字段：
      question:        用户原始问题
      mcp_context:     MCP 工具调用返回的上下文文本（已格式化）
      kb_context:      知识库检索返回的上下文文本（已格式化）
      mcp_intents:     MCP 通道命中的意图及其得分列表
      kb_intents:      知识库通道命中的意图及其得分列表
      eligible_intent_ids: 允许参与模板选择和规则注入的意图 ID
    """
    question: str
    mcp_context: str = ""
    kb_context: str = ""
    mcp_intents: list[NodeScore] = field(default_factory=list)
    kb_intents: list[NodeScore] = field(default_factory=list)
    eligible_intent_ids: set[str] = field(default_factory=set)

    def has_mcp(self) -> bool:
        return bool((self.mcp_context or "").strip())

    def has_kb(self) -> bool:
        return bool((self.kb_context or "").strip())


@dataclass
class PromptBuildPlan:
    """严格对应 ragent rag.core.prompt.PromptBuildPlan。

    实际渲染 system prompt 的输入：
      scene:        场景
      base_template: 选定的基模板（已包含引用规则，如果开启）
      mcp_context:  渲染进 system 的 MCP 上下文
      kb_context:   渲染进 system 的 KB 上下文
      question:     渲染进 user 消息的问题
    """
    scene: PromptScene
    base_template: str
    mcp_context: str
    kb_context: str
    question: str


@dataclass
class PromptPlan:
    """严格对应 ragent rag.core.prompt.PromptPlan。

    模板选择阶段产物：
      retained_intents: 参与模板选择的候选意图（已过滤）
      base_template:    选用的基模板（None 表示走 AgentPromptResolver 基础模板）
    """
    retained_intents: list[NodeScore]
    base_template: str | None = None
