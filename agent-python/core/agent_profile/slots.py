"""智能体提示词槽位与编排架构：1:1 翻译 ragent rag.core.prompt.AgentPromptSlot / config.OrchestrationMode。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Set


class OrchestrationMode(str, enum.Enum):
    """1:1 翻译 ragent OrchestrationMode（由 ragent.engine.type 指定）。"""

    WORKFLOW = "WORKFLOW"
    AGENT = "AGENT"

    @classmethod
    def of(cls, value: str | None) -> "OrchestrationMode":
        # 无法识别时回落 WORKFLOW
        if not value:
            return cls.WORKFLOW
        for mode in cls:
            if mode.name.lower() == value.strip().lower():
                return mode
        return cls.WORKFLOW


class Group(str, enum.Enum):
    """1:1 翻译 ragent AgentPromptSlot.Group（控制台分栏）。"""

    WORKFLOW = "WORKFLOW"
    AGENT = "AGENT"
    COMMON = "COMMON"

    @property
    def display_name(self) -> str:
        return {
            Group.WORKFLOW: "WorkFlow 专属",
            Group.AGENT: "Agent 专属",
            Group.COMMON: "通用",
        }[self]


@dataclass(frozen=True)
class AgentPromptSlot:
    """1:1 翻译 ragent AgentPromptSlot（槽位元数据的唯一权威源）。"""

    name: str
    display_name: str
    group: Group
    effective_modes: Set[OrchestrationMode]
    inactive_reason: str | None
    required_placeholders: Set[str] = field(default_factory=set)

    def is_effective_in(self, mode: OrchestrationMode) -> bool:
        return mode in self.effective_modes

    @staticmethod
    def effective_in(mode: OrchestrationMode) -> list["AgentPromptSlot"]:
        return [s for s in AGENT_PROMPT_SLOTS if s.is_effective_in(mode)]

    @staticmethod
    def find(key: str) -> "AgentPromptSlot | None":
        for slot in AGENT_PROMPT_SLOTS:
            if slot.name.lower() == key.strip().lower():
                return slot
        return None


# 1:1 对齐 Java 的枚举声明顺序与字段
AGENT_PROMPT_SLOTS: list[AgentPromptSlot] = [
    AgentPromptSlot(
        name="SYSTEM_CHAT", display_name="闲聊 / 关于助手", group=Group.WORKFLOW,
        effective_modes={OrchestrationMode.WORKFLOW},
        inactive_reason="Agent 模式下由主 Agent 直接应答",
        required_placeholders=set(),
    ),
    AgentPromptSlot(
        name="MCP_ANSWER", display_name="MCP 问答", group=Group.WORKFLOW,
        effective_modes={OrchestrationMode.WORKFLOW},
        inactive_reason="Agent 模式下改用原生工具调用，无独立的数据合成环节",
        required_placeholders=set(),
    ),
    AgentPromptSlot(
        name="MIXED_ANSWER", display_name="混合问答", group=Group.WORKFLOW,
        effective_modes={OrchestrationMode.WORKFLOW},
        inactive_reason="Agent 模式下由主 Agent 综合多个工具的结果",
        required_placeholders=set(),
    ),
    AgentPromptSlot(
        name="AGENT_MAIN", display_name="Agent 人设", group=Group.AGENT,
        effective_modes={OrchestrationMode.AGENT},
        inactive_reason="WorkFlow 模式不经过 ReAct 架构",
        required_placeholders=set(),
    ),
    AgentPromptSlot(
        name="KB_ANSWER", display_name="知识库问答", group=Group.COMMON,
        effective_modes={OrchestrationMode.WORKFLOW, OrchestrationMode.AGENT},
        inactive_reason=None,
        required_placeholders=set(),
    ),
    AgentPromptSlot(
        name="CONVERSATION_SUMMARY", display_name="会话压缩", group=Group.COMMON,
        effective_modes={OrchestrationMode.WORKFLOW, OrchestrationMode.AGENT},
        inactive_reason=None,
        required_placeholders={"{summary_max_chars}"},
    ),
    AgentPromptSlot(
        name="RECOMMENDED_QUESTIONS", display_name="推荐问题", group=Group.COMMON,
        effective_modes={OrchestrationMode.WORKFLOW, OrchestrationMode.AGENT},
        inactive_reason=None,
        required_placeholders={"{chunks}", "{count}", "{question}", "{answer}"},
    ),
]
