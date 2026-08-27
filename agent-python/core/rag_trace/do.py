"""RAG Trace 实体：1:1 翻译 ragent RagTraceRunDO / RagTraceNodeDO。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class RagTraceRunDO:
    """1:1 翻译 ragent RagTraceRunDO（表 t_rag_trace_run）。"""

    id: Optional[str] = None
    trace_id: Optional[str] = None
    trace_name: Optional[str] = None
    entry_method: Optional[str] = None
    conversation_id: Optional[str] = None
    task_id: Optional[str] = None
    user_id: Optional[str] = None
    status: Optional[str] = None
    error_message: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_ms: Optional[int] = None
    extra_data: Optional[str] = None
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None
    deleted: int = 0


@dataclass
class RagTraceNodeDO:
    """1:1 翻译 ragent RagTraceNodeDO（表 t_rag_trace_node）。"""

    id: Optional[str] = None
    trace_id: Optional[str] = None
    node_id: Optional[str] = None
    parent_node_id: Optional[str] = None
    depth: Optional[int] = None
    node_type: Optional[str] = None
    node_name: Optional[str] = None
    class_name: Optional[str] = None
    method_name: Optional[str] = None
    status: Optional[str] = None
    error_message: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_ms: Optional[int] = None
    extra_data: Optional[str] = None
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None
    deleted: int = 0
