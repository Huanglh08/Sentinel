"""对话记忆持久化：1:1 翻译 ragent JdbcConversationMemoryStore / ConversationMemoryService /
DefaultConversationMemoryService，并内置 ConversationRepository（对应 Java 的
ConversationMessageService + ConversationService + ConversationGroupService 数据访问）。

读写与 Java 同一份 `ragent` 库，表结构严格对齐：
  - t_message(id, conversation_id, user_id, role, content, thinking_content,
             thinking_duration, sources jsonb, retrieved_chunks jsonb,
             recommended_questions jsonb, reply_to_message_id, message_status,
             create_time, update_time, deleted)
  - t_conversation(id, conversation_id, user_id, title, last_time, create_time, update_time, deleted)

消息主键采用雪花算法（数值型），与 Java ASSIGN_ID 一致，保证摘要模块 Long.parseLong 比较语义可用。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Protocol

from core.db import get_pool
from core.memory.config import MemoryProperties
from core.memory.types import ChatMessage, Role

_logger = logging.getLogger("sentinel.memory.store")


# ---------------------------------------------------------------------------
# 雪花 ID 生成（1:1 翻译 ragent MyBatis ASSIGN_ID 雪花算法，数值型主键）
# ---------------------------------------------------------------------------
class _Snowflake:
    def __init__(self, node: int = 1) -> None:
        self._node = node & 0x3FF
        self._lock = threading.Lock()
        self._last_ts = -1
        self._seq = 0

    def next_id(self) -> int:
        with self._lock:
            ts = (int(time.time() * 1000) - 1288834974657) & 0x1FFFFFFFFFF
            if ts == self._last_ts:
                self._seq = (self._seq + 1) & 0xFFF
                if self._seq == 0:
                    while ts <= self._last_ts:
                        ts = (int(time.time() * 1000) - 1288834974657) & 0x1FFFFFFFFFF
            else:
                self._seq = 0
            self._last_ts = ts
            return (ts << 22) | (self._node << 12) | self._seq


_sf = _Snowflake()


def next_message_id() -> str:
    """生成数值型雪花 ID（字符串形式，与 Java 一致可 Long.parseLong）。"""
    return str(_sf.next_id())


# ---------------------------------------------------------------------------
# 仓储：对应 Java ConversationMessageService + ConversationService + ConversationGroupService
# ---------------------------------------------------------------------------
class ConversationRepository:
    """1:1 翻译 ragent 对话相关数据访问（MyBatis Mapper 等价物）。"""

    def __init__(self, pool=None) -> None:
        self._pool = pool

    def _pool(self):  # noqa: D401 - 兼容属性/方法两种取池方式
        return self._pool_inst or get_pool()

    @property
    def _pool_inst(self):
        return self.__dict__.get("_pool")

    # ---- 消息 ----
    def list_messages(
        self, conversation_id: str, user_id: str, limit: int, desc: bool = True
    ) -> list[dict]:
        order = "DESC" if desc else "ASC"
        sql = (
            "SELECT id, conversation_id, user_id, role, content, thinking_content, "
            "thinking_duration, sources, retrieved_chunks, reply_to_message_id, message_status "
            f"FROM t_message WHERE conversation_id = %s AND user_id = %s AND deleted = 0 "
            f"ORDER BY create_time {order} LIMIT %s"
        )
        return self._query(sql, (conversation_id, user_id, limit))

    def add_message(self, row: dict) -> str:
        mid = row.get("id") or next_message_id()
        sql = (
            "INSERT INTO t_message "
            "(id, conversation_id, user_id, role, content, thinking_content, "
            "thinking_duration, sources, retrieved_chunks, reply_to_message_id, message_status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s) "
            "RETURNING id"
        )
        params = (
            mid,
            row["conversation_id"],
            row["user_id"],
            row["role"],
            row.get("content"),
            row.get("thinking_content"),
            row.get("thinking_duration"),
            _jsonb(row.get("sources")),
            _jsonb(row.get("retrieved_chunks")),
            row.get("reply_to_message_id"),
            row.get("message_status"),
        )
        return self._execute_returning(sql, params, mid)

    def count_user_messages(self, conversation_id: str, user_id: str) -> int:
        sql = (
            "SELECT COUNT(*) FROM t_message "
            "WHERE conversation_id = %s AND user_id = %s AND deleted = 0"
        )
        rows = self._query(sql, (conversation_id, user_id))
        return int(rows[0]["count"]) if rows else 0

    def list_latest_user_only_messages(
        self, conversation_id: str, user_id: str, max_turns: int
    ) -> list[dict]:
        sql = (
            "SELECT id FROM t_message "
            "WHERE conversation_id = %s AND user_id = %s AND deleted = 0 AND role = 'user' "
            "ORDER BY create_time DESC LIMIT %s"
        )
        return self._query(sql, (conversation_id, user_id, max_turns))

    def list_messages_between_ids(
        self, conversation_id: str, user_id: str, after_id: str, before_id: str
    ) -> list[dict]:
        sql = (
            "SELECT id, role, content, create_time FROM t_message "
            "WHERE conversation_id = %s AND user_id = %s AND deleted = 0 "
            "AND id > %s AND id <= %s ORDER BY create_time ASC"
        )
        return self._query(sql, (conversation_id, user_id, after_id, before_id))

    def find_max_message_id_at_or_before(
        self, conversation_id: str, user_id: str, at_time
    ) -> str | None:
        sql = (
            "SELECT id FROM t_message "
            "WHERE conversation_id = %s AND user_id = %s AND deleted = 0 "
            "AND create_time <= %s ORDER BY create_time DESC LIMIT 1"
        )
        rows = self._query(sql, (conversation_id, user_id, at_time))
        return str(rows[0]["id"]) if rows else None

    # ---- 会话 ----
    def create_or_update_conversation(
        self, conversation_id: str, user_id: str, question: str, last_time
    ) -> None:
        sql = (
            "INSERT INTO t_conversation (id, conversation_id, user_id, title, last_time) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (conversation_id) DO UPDATE SET last_time = EXCLUDED.last_time"
        )
        self._execute(
            sql, (next_message_id(), conversation_id, user_id, question[:200], last_time)
        )

    # ---- 摘要 ----
    def find_latest_summary(
        self, conversation_id: str, user_id: str
    ) -> dict | None:
        sql = (
            "SELECT id, conversation_id, user_id, content, last_message_id, "
            "create_time, update_time FROM t_conversation_summary "
            "WHERE conversation_id = %s AND user_id = %s AND deleted = 0 "
            "ORDER BY create_time DESC LIMIT 1"
        )
        rows = self._query(sql, (conversation_id, user_id))
        return rows[0] if rows else None

    def add_message_summary(self, row: dict) -> None:
        sql = (
            "INSERT INTO t_conversation_summary "
            "(id, conversation_id, user_id, content, last_message_id) "
            "VALUES (%s, %s, %s, %s, %s)"
        )
        self._execute(
            sql,
            (
                next_message_id(),
                row["conversation_id"],
                row["user_id"],
                row["content"],
                row.get("last_message_id"),
            ),
        )

    # ---- 底层执行 ----
    def _query(self, sql: str, params: tuple) -> list[dict]:
        try:
            pool = self._pool_inst or get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
        except Exception as exc:  # pragma: no cover - 与 Java 降级一致
            _logger.warning("ConversationRepository 查询失败: %s", exc)
            return []

    def _execute(self, sql: str, params: tuple) -> None:
        pool = self._pool_inst or get_pool()
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)

    def _execute_returning(self, sql: str, params: tuple, fallback: str) -> str:
        try:
            pool = self._pool_inst or get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
                return str(row[0]) if row else fallback
        except Exception as exc:  # pragma: no cover
            _logger.warning("ConversationRepository 插入失败: %s", exc)
            return fallback


def _jsonb(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 记忆存储接口与 JDBC 实现
# ---------------------------------------------------------------------------
class ConversationMemoryStore(Protocol):
    """1:1 翻译 ragent ConversationMemoryStore。"""

    def load_history(self, conversation_id: str, user_id: str) -> list[ChatMessage]:
        ...

    def append(self, conversation_id: str, user_id: str, message: ChatMessage) -> str:
        ...

    def refresh_cache(self, conversation_id: str, user_id: str) -> None:
        ...


class JdbcConversationMemoryStore:
    """1:1 翻译 ragent JdbcConversationMemoryStore。"""

    def __init__(
        self,
        repo: ConversationRepository | None = None,
        memory_properties: MemoryProperties | None = None,
    ) -> None:
        self.repo = repo or ConversationRepository()
        self.memory_properties = memory_properties or MemoryProperties()

    def load_history(self, conversation_id: str, user_id: str) -> list[ChatMessage]:
        max_messages = self.memory_properties.history_keep_turns * 2
        rows = self.repo.list_messages(conversation_id, user_id, max_messages, desc=True)
        if not rows:
            return []
        messages = [
            m for m in (ChatMessage.from_row(r) for r in rows) if self._is_history(m)
        ]
        return self._normalize(messages)

    def append(self, conversation_id: str, user_id: str, message: ChatMessage) -> str:
        row = message.to_row(conversation_id, user_id)
        mid = self.repo.add_message(row)
        if message.role == Role.USER:
            self.repo.create_or_update_conversation(
                conversation_id, user_id, message.content, _now()
            )
        return mid

    def refresh_cache(self, conversation_id: str, user_id: str) -> None:
        # JDBC 直读模式，无需刷新缓存（与 Java 一致）
        return None

    @staticmethod
    def _is_history(message: ChatMessage | None) -> bool:
        if message is None:
            return False
        return message.role in (Role.USER, Role.ASSISTANT) and bool(
            (message.content or "").strip()
        )

    @staticmethod
    def _normalize(messages: list[ChatMessage]) -> list[ChatMessage]:
        if not messages:
            return []
        start = 0
        while start < len(messages) and messages[start].role == Role.ASSISTANT:
            start += 1
        if start >= len(messages):
            return []
        return messages[start:]


# ---------------------------------------------------------------------------
# 记忆服务接口与默认实现
# ---------------------------------------------------------------------------
class ConversationMemoryService(Protocol):
    """1:1 翻译 ragent ConversationMemoryService。"""

    def load(self, conversation_id: str, user_id: str) -> list[ChatMessage]:
        ...

    def append(self, conversation_id: str, user_id: str, message: ChatMessage) -> str:
        ...


class DefaultConversationMemoryService:
    """1:1 翻译 ragent DefaultConversationMemoryService：组合 store + summary。"""

    def __init__(
        self,
        store: JdbcConversationMemoryStore,
        summary_service: Any | None = None,
    ) -> None:
        self.store = store
        self.summary_service = summary_service

    def load(self, conversation_id: str, user_id: str) -> list[ChatMessage]:
        history = self.store.load_history(conversation_id, user_id)
        if self.summary_service is not None:
            summary = self.summary_service.load_latest_summary(conversation_id, user_id)
            if summary is not None:
                return [summary] + history
        return history

    def append(self, conversation_id: str, user_id: str, message: ChatMessage) -> str:
        mid = self.store.append(conversation_id, user_id, message)
        if self.summary_service is not None:
            self.summary_service.compress_if_needed(conversation_id, user_id, message)
        return mid


def _now():
    from datetime import datetime

    return datetime.now()
