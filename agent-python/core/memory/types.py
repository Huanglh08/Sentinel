"""对话消息类型：1:1 翻译 ragent framework.convention.ChatMessage。

字段严格对齐 Java ChatMessage（role / content / thinkingContent / thinkingDuration /
sources / retrievedChunks / replyToMessageId / messageStatus），并补充 from_dict/to_dict
以便与 t_message 表（jsonb 列）互转。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class Role(str, enum.Enum):
    """1:1 翻译 ragent ChatMessage.Role。"""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"

    @classmethod
    def from_string(cls, value: str | None) -> "Role":
        if value is None:
            return cls.USER
        v = value.strip().lower()
        if v == "user":
            return cls.USER
        if v in ("assistant", "ai", "bot"):
            return cls.ASSISTANT
        if v == "system":
            return cls.SYSTEM
        return cls.USER


@dataclass
class ChatMessage:
    """1:1 翻译 ragent ChatMessage。"""

    role: Role
    content: str
    thinking_content: str | None = None
    thinking_duration: int | None = None
    sources: list[Any] | None = None
    retrieved_chunks: list[Any] | None = None
    reply_to_message_id: str | None = None
    message_status: str | None = None

    # ---- 工厂方法（1:1 翻译 Java ChatMessage.user/assistant/system）----
    @classmethod
    def user(cls, content: str) -> "ChatMessage":
        return cls(role=Role.USER, content=content)

    @classmethod
    def assistant(cls, content: str) -> "ChatMessage":
        return cls(role=Role.ASSISTANT, content=content)

    @classmethod
    def system(cls, content: str) -> "ChatMessage":
        return cls(role=Role.SYSTEM, content=content)

    # ---- 与 t_message 行互转 ----
    @classmethod
    def from_row(cls, row: dict) -> "ChatMessage | None":
        if row is None or not (row.get("content") or "").strip():
            return None
        role = Role.from_string(row.get("role"))
        content = row["content"]
        if role == Role.ASSISTANT:
            # 1:1 翻译 Java CitationMarkup.strip：剥离引用标记后再进模型上下文
            content = _strip_citation(content)
        return cls(
            role=role,
            content=content,
            thinking_content=row.get("thinking_content"),
            thinking_duration=row.get("thinking_duration"),
            sources=row.get("sources"),
            retrieved_chunks=row.get("retrieved_chunks"),
            reply_to_message_id=row.get("reply_to_message_id"),
            message_status=row.get("message_status"),
        )

    def to_row(self, conversation_id: str, user_id: str) -> dict:
        return {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "role": self.role.value,
            "content": self.content,
            "thinking_content": self.thinking_content,
            "thinking_duration": self.thinking_duration,
            "sources": self.sources,
            "retrieved_chunks": self.retrieved_chunks,
            "reply_to_message_id": self.reply_to_message_id,
            "message_status": self.message_status,
        }


def _strip_citation(text: str) -> str:
    """1:1 翻译 ragent CitationMarkup.strip：去除 [n](cite:...) 引用标注。"""
    import re

    return re.sub(r"\[(\d+)\]\(cite:[^)]*\)", "", text or "")
