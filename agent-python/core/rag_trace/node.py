"""RAG Trace 节点/链路装饰器与上下文管理器：1:1 翻译 ragent RagTraceNode 注解 + RagTraceAspect。

提供：
  - RagTraceNode：方法装饰器（同步/异步），自动记录节点 start/finish 并维护节点栈
  - trace_run / trace_node：异步上下文管理器，供 sentinel 的 chat 管线手动埋点
"""
from __future__ import annotations

import functools
import inspect
import time
import uuid
from datetime import datetime
from typing import Optional

from core.rag_trace.context import RagTraceContext
from core.rag_trace.do import RagTraceNodeDO, RagTraceRunDO
from core.rag_trace.service import RagTraceRecordService


def _new_id() -> str:
    return uuid.uuid4().hex


class RagTraceNode:
    """1:1 翻译 ragent @RagTraceNode（name / type）。"""

    def __init__(self, name: str = "", node_type: str = "METHOD") -> None:
        self.name = name
        self.node_type = node_type

    def __call__(self, func):
        is_async = inspect.iscoroutinefunction(func)

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            node_name = self.name or func.__name__
            with trace_node(node_name, self.node_type, func.__qualname__):
                return await func(*args, **kwargs)

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            node_name = self.name or func.__name__
            with trace_node(node_name, self.node_type, func.__qualname__):
                return func(*args, **kwargs)

        return async_wrapper if is_async else sync_wrapper


class _NodeSpan:
    def __init__(self, service: RagTraceRecordService, node_name: str,
                 node_type: str, method_name: str) -> None:
        self._service = service
        self._node_name = node_name
        self._node_type = node_type
        self._method_name = method_name
        self._node_id = _new_id()
        self._start = time.monotonic()
        self._started = False

    def __enter__(self):
        self._begin()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._end("ERROR" if exc_type else "SUCCESS",
                  str(exc) if exc else None)
        return False

    async def __aenter__(self):
        self._begin()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._end("ERROR" if exc_type else "SUCCESS",
                  str(exc) if exc else None)
        return False

    def _begin(self):
        trace_id = RagTraceContext.get_trace_id()
        if not trace_id:
            return
        RagTraceContext.push_node(self._node_id)
        parent = RagTraceContext.current_node_id()
        # 注意：push 后 current 已是自己，父节点需在 push 前取
        node = RagTraceNodeDO(
            trace_id=trace_id,
            node_id=self._node_id,
            parent_node_id=self._parent_id,
            depth=RagTraceContext.depth() - 1,
            node_type=self._node_type,
            node_name=self._node_name,
            method_name=self._method_name,
            status="RUNNING",
            start_time=datetime.now(),
        )
        self._service.start_node(node)
        self._started = True

    def _end(self, status: str, error_message: Optional[str]):
        if not self._started:
            return
        RagTraceContext.pop_node()
        self._service.finish_node(
            RagTraceContext.get_trace_id(), self._node_id, status,
            error_message, datetime.now(),
            int((time.monotonic() - self._start) * 1000),
        )

    @property
    def _parent_id(self):
        # 在 push 之前记录的父节点
        return getattr(self, "_saved_parent", None)


def trace_node(node_name: str, node_type: str = "METHOD",
               method_name: str = "", service: RagTraceRecordService | None = None):
    """异步/同步上下文管理器：记录单个 trace 节点。"""
    svc = service or RagTraceRecordService()
    span = _NodeSpan(svc, node_name, node_type, method_name)
    # 进入前保存父节点
    span._saved_parent = RagTraceContext.current_node_id()
    return span


class _RunSpan:
    def __init__(self, service: RagTraceRecordService, trace_name: str,
                 entry_method: str, conversation_id: str | None,
                 task_id: str | None, user_id: str | None) -> None:
        self._service = service
        self._trace_id = _new_id()
        self._start = time.monotonic()
        self._run = RagTraceRunDO(
            trace_id=self._trace_id, trace_name=trace_name,
            entry_method=entry_method, conversation_id=conversation_id,
            task_id=task_id, user_id=user_id, status="RUNNING",
            start_time=datetime.now(),
        )

    def __enter__(self):
        RagTraceContext.set_trace_id(self._trace_id)
        if self._run.task_id:
            RagTraceContext.set_task_id(self._run.task_id)
        self._service.start_run(self._run)
        return self

    def __exit__(self, exc_type, exc, tb):
        self._service.finish_run(
            self._trace_id, "ERROR" if exc_type else "SUCCESS",
            str(exc) if exc else None, datetime.now(),
            int((time.monotonic() - self._start) * 1000),
        )
        RagTraceContext.clear()
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, exc_type, exc, tb):
        return self.__exit__(exc_type, exc, tb)


def trace_run(trace_name: str, entry_method: str = "",
              conversation_id: str | None = None, task_id: str | None = None,
              user_id: str | None = None,
              service: RagTraceRecordService | None = None):
    """异步/同步上下文管理器：开启一条 RAG Trace 链路（run）。"""
    return _RunSpan(service or RagTraceRecordService(), trace_name,
                   entry_method, conversation_id, task_id, user_id)
