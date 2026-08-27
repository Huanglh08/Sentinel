"""示例问题管理：1:1 翻译 ragent SampleQuestionController / SampleQuestionServiceImpl / SampleQuestionDO。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from core.agent_profile.service import ClientException
from core.agent_profile.repository import next_id
from core.db import get_pool


@dataclass
class SampleQuestionDO:
    """1:1 翻译 ragent SampleQuestionDO（表 t_sample_question）。"""

    id: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    question: Optional[str] = None
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None
    deleted: int = 0


class SampleQuestionRepository:
    """1:1 翻译 ragent SampleQuestionMapper。"""

    def insert(self, record: SampleQuestionDO) -> str:
        pid = record.id or next_id()
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO t_sample_question (id, title, description, question) "
                "VALUES (%s,%s,%s,%s)",
                (pid, record.title, record.description, record.question),
            )
        return pid

    def update(self, record: SampleQuestionDO) -> None:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE t_sample_question SET title=%s, description=%s, question=%s "
                "WHERE id=%s AND deleted=0",
                (record.title, record.description, record.question, record.id),
            )

    def delete(self, sid: str) -> None:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE t_sample_question SET deleted=1 WHERE id=%s AND deleted=0", (sid,))

    def select_by_id(self, sid: str) -> SampleQuestionDO | None:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, description, question, create_time, update_time "
                "FROM t_sample_question WHERE id=%s AND deleted=0", (sid,))
            row = cur.fetchone()
            return _row(row) if row else None

    def select_page(self, keyword: str | None, limit: int, offset: int):
        clause = "WHERE deleted=0"
        params: list = []
        if keyword:
            clause = ("WHERE deleted=0 AND (title ILIKE %s OR description ILIKE %s "
                      "OR question ILIKE %s)")
            like = f"%{keyword}%"
            params = [like, like, like]
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT id, title, description, question, create_time, update_time "
                f"FROM t_sample_question {clause} ORDER BY update_time DESC "
                f"LIMIT %s OFFSET %s", params + [limit, offset])
            rows = cur.fetchall()
            cur.execute(f"SELECT count(*) FROM t_sample_question {clause}", params)
            total = cur.fetchone()[0]
        return total, [_row(r) for r in rows]

    def select_random(self, limit: int) -> list[SampleQuestionDO]:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, description, question, create_time, update_time "
                "FROM t_sample_question WHERE deleted=0 ORDER BY RANDOM() LIMIT %s", (limit,))
            return [_row(r) for r in cur.fetchall()]


def _row(row) -> SampleQuestionDO:
    return SampleQuestionDO(
        id=row["id"], title=row.get("title"), description=row.get("description"),
        question=row.get("question"), create_time=row.get("create_time"),
        update_time=row.get("update_time"), deleted=row.get("deleted") or 0)


class SampleQuestionService:
    """1:1 翻译 ragent SampleQuestionServiceImpl。"""

    DEFAULT_LIMIT = 3

    def __init__(self, repo: SampleQuestionRepository | None = None) -> None:
        self._repo = repo or SampleQuestionRepository()

    def create(self, question: str | None, title: str | None, description: str | None) -> str:
        question = (question or "").strip() or None
        if not question:
            raise ClientException("示例问题内容不能为空")
        return self._repo.insert(SampleQuestionDO(
            title=(title or "").strip() or None,
            description=(description or "").strip() or None,
            question=question,
        ))

    def update(self, sid: str, question: str | None, title: str | None, description: str | None) -> None:
        record = self._load(sid)
        if question is not None:
            q = (question or "").strip() or None
            if not q:
                raise ClientException("示例问题内容不能为空")
            record.question = q
        if title is not None:
            record.title = (title or "").strip() or None
        if description is not None:
            record.description = (description or "").strip() or None
        self._repo.update(record)

    def delete(self, sid: str) -> None:
        self._load(sid)
        self._repo.delete(sid)

    def query_by_id(self, sid: str) -> dict:
        return self._to_vo(self._load(sid))

    def page_query(self, keyword: str | None, current: int, size: int) -> dict:
        total, rows = self._repo.select_page(
            (keyword or "").strip() or None, size, (current - 1) * size)
        return {
            "current": current, "size": size, "total": total,
            "records": [self._to_vo(r) for r in rows],
        }

    def list_random_questions(self) -> list[dict]:
        rows = self._repo.select_random(self.DEFAULT_LIMIT)
        return [self._to_vo(r) for r in rows]

    def _load(self, sid: str) -> SampleQuestionDO:
        record = self._repo.select_by_id(sid)
        if record is None:
            raise ClientException("示例问题不存在")
        return record

    @staticmethod
    def _to_vo(record: SampleQuestionDO) -> dict:
        return {
            "id": record.id, "title": record.title, "description": record.description,
            "question": record.question,
            "createTime": record.create_time.isoformat() if record.create_time else None,
            "updateTime": record.update_time.isoformat() if record.update_time else None,
        }


def build_router() -> "APIRouter":
    """1:1 翻译 ragent SampleQuestionController。"""
    from fastapi import APIRouter

    router = APIRouter()
    svc = SampleQuestionService()

    @router.get("")
    def page_query(keyword: str | None = None, current: int = 1, size: int = 10) -> dict:
        return svc.page_query(keyword, current, size)

    @router.get("/random")
    def random_questions() -> list:
        return svc.list_random_questions()

    @router.get("/{sid}")
    def query_by_id(sid: str) -> dict:
        return svc.query_by_id(sid)

    @router.post("")
    def create(question: str, title: str | None = None, description: str | None = None) -> dict:
        return {"id": svc.create(question, title, description)}

    @router.put("/{sid}")
    def update(sid: str, question: str | None = None, title: str | None = None,
               description: str | None = None) -> dict:
        svc.update(sid, question, title, description)
        return {"success": True}

    @router.delete("/{sid}")
    def delete(sid: str) -> dict:
        svc.delete(sid)
        return {"success": True}

    return router
