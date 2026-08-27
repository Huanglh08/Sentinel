"""LLM 请求/响应模型：严格对应 ragent infra-ai.model.BaseChatCompletionRequest / BaseChatCompletionResponse。

- ChatRequest：6 字段（model/messages/temperature/topP/maxTokens/stream）—— 对应 ragent BaseChatCompletionRequest
- ChatResponse：3 字段（content/model/usage）—— 对应 ragent BaseChatCompletionResponse
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChatRequest:
    """严格对应 ragent BaseChatCompletionRequest（6 字段）。"""
    model: str
    messages: list[dict[str, str]] = field(default_factory=list)
    temperature: float = 0.7
    top_p: float = 0.9
    max_tokens: int = 2048
    stream: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": self.messages,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "stream": self.stream,
        }


@dataclass
class ChatResponse:
    """严格对应 ragent BaseChatCompletionResponse（3 字段）。"""
    content: str
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
