"""智能体画像 / 提示词仓储：1:1 翻译 ragent AgentProfileMapper / AgentPromptMapper。

直接读写与 Java 共用的 ragent 库（t_agent_profile / t_agent_prompt），沿用 core.db 共享连接池。
"""
from __future__ import annotations

import logging
import threading
import time

from core.agent_profile.do import AgentPromptDO, AgentProfileDO
from core.db import get_pool
from psycopg.rows import dict_row

_logger = logging.getLogger("sentinel.agent_profile.repo")

# ---- 雪花 ID（与 ragent ASSIGN_ID 同量级，避免与 Java 主键冲突）----
_EPOCH = 1700000000000
_lock = threading.Lock()
_last_ts = 0
_seq = 0


def next_id() -> str:
    """1:1 对齐 ragent ASSIGN_ID 雪花算法（数值字符串）。"""
    global _last_ts, _seq
    with _lock:
        ts = int(time.time() * 1000) - _EPOCH
        if ts == _last_ts:
            _seq = (_seq + 1) & 0x3FF
            if _seq == 0:
                while ts <= _last_ts:
                    ts = int(time.time() * 1000) - _EPOCH
        else:
            _seq = 0
        _last_ts = ts
        return str((ts << 22) | (_seq))


def _row_to_profile(row) -> AgentProfileDO:
    return AgentProfileDO(
        id=row["id"], name=row["name"], description=row.get("description"),
        avatar=row.get("avatar"), builtin=row.get("builtin") or 0,
        active=row.get("active") or 0, create_by=row.get("create_by"),
        update_by=row.get("update_by"), create_time=row.get("create_time"),
        update_time=row.get("update_time"), deleted=row.get("deleted") or 0,
    )


def _row_to_prompt(row) -> AgentPromptDO:
    return AgentPromptDO(
        id=row["id"], agent_id=row.get("agent_id"), slot_key=row.get("slot_key"),
        content=row.get("content"), create_by=row.get("create_by"),
        update_by=row.get("update_by"), create_time=row.get("create_time"),
        update_time=row.get("update_time"), deleted=row.get("deleted") or 0,
    )


class AgentProfileRepository:
    """1:1 翻译 ragent AgentProfileMapper。"""

    def select_all(self) -> list[AgentProfileDO]:
        with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, name, description, avatar, builtin, active, "
                "create_by, update_by, create_time, update_time, deleted "
                "FROM t_agent_profile WHERE deleted = 0 ORDER BY create_time, id"
            )
            return [_row_to_profile(r) for r in cur.fetchall()]

    def select_by_id(self, agent_id: str) -> AgentProfileDO | None:
        with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, name, description, avatar, builtin, active, "
                "create_by, update_by, create_time, update_time, deleted "
                "FROM t_agent_profile WHERE id = %s AND deleted = 0", (agent_id,)
            )
            row = cur.fetchone()
            return _row_to_profile(row) if row else None

    def select_first_by_flag(self, flag: str) -> AgentProfileDO | None:
        with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT id, name, description, avatar, builtin, active, "
                f"create_by, update_by, create_time, update_time, deleted "
                f"FROM t_agent_profile WHERE deleted = 0 AND {flag} = 1 "
                f"ORDER BY create_time, id LIMIT 1"
            )
            row = cur.fetchone()
            return _row_to_profile(row) if row else None

    def exists_by_name(self, name: str, exclude_id: str | None) -> bool:
        with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            if exclude_id:
                cur.execute(
                    "SELECT 1 FROM t_agent_profile WHERE deleted = 0 AND name = %s "
                    "AND id <> %s LIMIT 1", (name, exclude_id)
                )
            else:
                cur.execute(
                    "SELECT 1 FROM t_agent_profile WHERE deleted = 0 AND name = %s "
                    "LIMIT 1", (name,)
                )
            return cur.fetchone() is not None

    def insert(self, profile: AgentProfileDO) -> str:
        pid = profile.id or next_id()
        with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "INSERT INTO t_agent_profile (id, name, description, avatar, "
                "builtin, active, create_by, update_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (pid, profile.name, profile.description, profile.avatar,
                 profile.builtin, profile.active, profile.create_by, profile.update_by),
            )
        return pid

    def update_fields(self, agent_id: str, **fields) -> None:
        cols = ", ".join(f"{k} = %s" for k in fields)
        params = list(fields.values()) + [agent_id]
        with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"UPDATE t_agent_profile SET {cols} WHERE id = %s AND deleted = 0",
                params,
            )

    def update_active_zero_where_active_one(self) -> int:
        with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE t_agent_profile SET active = 0 WHERE active = 1 AND deleted = 0"
            )
            return cur.rowcount

    def delete(self, agent_id: str) -> None:
        with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE t_agent_profile SET deleted = 1 WHERE id = %s AND deleted = 0",
                (agent_id,),
            )


class AgentPromptRepository:
    """1:1 翻译 ragent AgentPromptMapper。"""

    def select_by_agent(self, agent_id: str) -> list[AgentPromptDO]:
        with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, agent_id, slot_key, content, create_by, update_by, "
                "create_time, update_time, deleted FROM t_agent_prompt "
                "WHERE agent_id = %s AND deleted = 0", (agent_id,)
            )
            return [_row_to_prompt(r) for r in cur.fetchall()]

    def select_configured(self) -> list[AgentPromptDO]:
        """1:1 对齐 loadConfiguredSlots：只投影 agent_id / slot_key，且 content 非空。"""
        with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT agent_id, slot_key FROM t_agent_prompt "
                "WHERE deleted = 0 AND content IS NOT NULL AND content <> ''"
            )
            return [AgentPromptDO(agent_id=r["agent_id"], slot_key=r["slot_key"])
                    for r in cur.fetchall()]

    def exists(self, agent_id: str, slot_key: str) -> bool:
        with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT 1 FROM t_agent_prompt WHERE deleted = 0 AND agent_id = %s "
                "AND slot_key = %s LIMIT 1", (agent_id, slot_key)
            )
            return cur.fetchone() is not None

    def update_content(self, agent_id: str, slot_key: str, content) -> None:
        with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE t_agent_prompt SET content = %s WHERE deleted = 0 "
                "AND agent_id = %s AND slot_key = %s",
                (content, agent_id, slot_key),
            )

    def insert(self, agent_id: str, slot_key: str, content: str) -> None:
        with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "INSERT INTO t_agent_prompt (id, agent_id, slot_key, content) "
                "VALUES (%s, %s, %s, %s)",
                (next_id(), agent_id, slot_key, content),
            )

    def delete_by_agent(self, agent_id: str) -> None:
        with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE t_agent_prompt SET deleted = 1 WHERE agent_id = %s AND deleted = 0",
                (agent_id,),
            )
