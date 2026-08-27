"""RAG Trace 上下文：1:1 翻译 ragent framework.trace.RagTraceContext。

使用 contextvars 在 asyncio 任务间透传 traceId / taskId / 节点栈。
asyncio 的 Task 会自动拷贝上下文，因此 NODE_STACK 的「深拷贝」由运行时保证，无需手动 copy。
"""
from __future__ import annotations

import contextvars
from collections import deque

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("rag_trace_id", default=None)
_task_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("rag_trace_task_id", default=None)
_node_stack: contextvars.ContextVar[deque[str] | None] = contextvars.ContextVar("rag_trace_node_stack", default=None)


class RagTraceContext:
    """1:1 翻译 ragent RagTraceContext。"""

    @staticmethod
    def get_trace_id() -> str | None:
        return _trace_id.get()

    @staticmethod
    def set_trace_id(trace_id: str) -> None:
        _trace_id.set(trace_id)

    @staticmethod
    def get_task_id() -> str | None:
        return _task_id.get()

    @staticmethod
    def set_task_id(task_id: str) -> None:
        _task_id.set(task_id)

    @staticmethod
    def depth() -> int:
        stack = _node_stack.get()
        return len(stack) if stack else 0

    @staticmethod
    def current_node_id() -> str | None:
        stack = _node_stack.get()
        return stack[-1] if stack else None

    @staticmethod
    def push_node(node_id: str) -> None:
        stack = _node_stack.get()
        if stack is None:
            stack = deque()
            _node_stack.set(stack)
        stack.append(node_id)

    @staticmethod
    def pop_node() -> None:
        stack = _node_stack.get()
        if not stack:
            return
        stack.pop()
        if not stack:
            _node_stack.set(None)

    @staticmethod
    def clear() -> None:
        _trace_id.set(None)
        _task_id.set(None)
        _node_stack.set(None)
