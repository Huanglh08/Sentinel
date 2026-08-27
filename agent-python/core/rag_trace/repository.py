"""RAG Trace 仓储：1:1 翻译 ragent RagTraceRunMapper / RagTraceNodeMapper。"""
from __future__ import annotations

from core.db import get_pool
from core.rag_trace.do import RagTraceNodeDO, RagTraceRunDO


class RagTraceRepository:
    """1:1 翻译 ragent RagTraceRunMapper + RagTraceNodeMapper。"""

    # ---- run ----
    def insert_run(self, run: RagTraceRunDO) -> None:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO t_rag_trace_run (id, trace_id, trace_name, entry_method, "
                "conversation_id, task_id, user_id, status, error_message, "
                "start_time, end_time, duration_ms, extra_data) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (run.id, run.trace_id, run.trace_name, run.entry_method,
                 run.conversation_id, run.task_id, run.user_id, run.status,
                 run.error_message, run.start_time, run.end_time,
                 run.duration_ms, run.extra_data),
            )

    def update_run(self, trace_id: str, status: str, error_message,
                   end_time, duration_ms) -> None:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE t_rag_trace_run SET status=%s, error_message=%s, "
                "end_time=%s, duration_ms=%s WHERE trace_id=%s AND deleted=0",
                (status, error_message, end_time, duration_ms, trace_id),
            )

    # ---- node ----
    def insert_node(self, node: RagTraceNodeDO) -> None:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO t_rag_trace_node (id, trace_id, node_id, parent_node_id, "
                "depth, node_type, node_name, class_name, method_name, status, "
                "error_message, start_time, end_time, duration_ms, extra_data) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (node.id, node.trace_id, node.node_id, node.parent_node_id,
                 node.depth, node.node_type, node.node_name, node.class_name,
                 node.method_name, node.status, node.error_message,
                 node.start_time, node.end_time, node.duration_ms, node.extra_data),
            )

    def update_node(self, trace_id: str, node_id: str, status: str,
                    error_message, end_time, duration_ms) -> None:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE t_rag_trace_node SET status=%s, error_message=%s, "
                "end_time=%s, duration_ms=%s WHERE trace_id=%s AND node_id=%s AND deleted=0",
                (status, error_message, end_time, duration_ms, trace_id, node_id),
            )
