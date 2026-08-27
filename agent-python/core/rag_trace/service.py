"""RAG Trace 记录服务：1:1 翻译 ragent RagTraceRecordService / RagTraceRecordServiceImpl。"""
from __future__ import annotations

import uuid
from datetime import datetime

from core.rag_trace.do import RagTraceNodeDO, RagTraceRunDO
from core.rag_trace.repository import RagTraceRepository


def _new_id() -> str:
    return uuid.uuid4().hex


class RagTraceRecordService:
    """1:1 翻译 ragent RagTraceRecordService。"""

    def __init__(self, repo: RagTraceRepository | None = None) -> None:
        self._repo = repo or RagTraceRepository()

    def start_run(self, run: RagTraceRunDO) -> None:
        if run.id is None:
            run.id = _new_id()
        self._repo.insert_run(run)

    def finish_run(self, trace_id: str, status: str, error_message: str | None,
                   end_time: datetime, duration_ms: int) -> None:
        self._repo.update_run(trace_id, status, error_message, end_time, duration_ms)

    def start_node(self, node: RagTraceNodeDO) -> None:
        if node.id is None:
            node.id = _new_id()
        self._repo.insert_node(node)

    def finish_node(self, trace_id: str, node_id: str, status: str,
                    error_message: str | None, end_time: datetime, duration_ms: int) -> None:
        self._repo.update_node(trace_id, node_id, status, error_message, end_time, duration_ms)
