"""关键词映射管理：1:1 翻译 ragent QueryTermMappingController / QueryTermMappingAdminServiceImpl /
QueryTermMappingDO / QueryTermMappingCacheManager。"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from core.agent_profile.service import ClientException
from core.agent_profile.repository import next_id
from core.db import get_pool

_logger = logging.getLogger("sentinel.admin.qtm")


@dataclass
class QueryTermMappingDO:
    """1:1 翻译 ragent QueryTermMappingDO（表 t_query_term_mapping）。"""

    id: Optional[str] = None
    domain: Optional[str] = None
    source_term: Optional[str] = None
    target_term: Optional[str] = None
    match_type: int = 1
    priority: int = 0
    enabled: int = 1
    remark: Optional[str] = None
    create_by: Optional[str] = None
    update_by: Optional[str] = None
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None


class QueryTermMappingCacheManager:
    """1:1 翻译 ragent QueryTermMappingCacheManager（Redis 优先，回落进程内）。"""

    CACHE_KEY = "ragent:query-term:mappings"
    CACHE_EXPIRE_SECONDS = 7 * 24 * 3600

    def __init__(self) -> None:
        self._local: dict = {}
        self._lock = threading.Lock()
        self._redis = None

    @property
    def redis(self):
        if self._redis is None:
            try:
                import redis  # type: ignore

                self._redis = redis.from_url(
                    os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"), decode_responses=True)
            except Exception as exc:  # noqa: BLE001
                _logger.warning("Redis 不可用，术语映射缓存回落进程内：%s", exc)
                self._redis = False
        return self._redis or None

    def get_mappings_from_cache(self):
        try:
            r = self.redis
            if r is not None:
                raw = r.get(self.CACHE_KEY)
                return json.loads(raw) if raw else None
        except Exception as exc:  # noqa: BLE001
            _logger.debug("读取术语映射缓存失败：%s", exc)
        with self._lock:
            return self._local.get(self.CACHE_KEY)

    def save_mappings_to_cache(self, mappings: list) -> None:
        try:
            r = self.redis
            if r is not None:
                r.set(self.CACHE_KEY, json.dumps(mappings), ex=self.CACHE_EXPIRE_SECONDS)
        except Exception as exc:  # noqa: BLE001
            _logger.debug("保存术语映射缓存失败：%s", exc)
        with self._lock:
            self._local[self.CACHE_KEY] = list(mappings)

    def clear_cache(self) -> None:
        try:
            r = self.redis
            if r is not None:
                r.delete(self.CACHE_KEY)
        except Exception as exc:  # noqa: BLE001
            _logger.debug("清除术语映射缓存失败：%s", exc)
        with self._lock:
            self._local.pop(self.CACHE_KEY, None)


class QueryTermMappingRepository:
    """1:1 翻译 ragent QueryTermMappingMapper。"""

    def insert(self, record: QueryTermMappingDO) -> str:
        pid = record.id or next_id()
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO t_query_term_mapping (id, domain, source_term, target_term, "
                "match_type, priority, enabled, remark) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (pid, record.domain, record.source_term, record.target_term,
                 record.match_type, record.priority, record.enabled, record.remark))
        return pid

    def update(self, record: QueryTermMappingDO) -> None:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE t_query_term_mapping SET domain=%s, source_term=%s, target_term=%s, "
                "match_type=%s, priority=%s, enabled=%s, remark=%s WHERE id=%s",
                (record.domain, record.source_term, record.target_term, record.match_type,
                 record.priority, record.enabled, record.remark, record.id))

    def delete(self, mid: str) -> None:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM t_query_term_mapping WHERE id=%s", (mid,))

    def select_by_id(self, mid: str) -> QueryTermMappingDO | None:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, domain, source_term, target_term, match_type, priority, "
                "enabled, remark, create_time, update_time FROM t_query_term_mapping WHERE id=%s",
                (mid,))
            row = cur.fetchone()
            return _row(row) if row else None

    def select_page(self, keyword: str | None, limit: int, offset: int):
        clause = ""
        params: list = []
        if keyword:
            like = f"%{keyword}%"
            clause = "WHERE source_term ILIKE %s OR target_term ILIKE %s"
            params = [like, like]
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT id, domain, source_term, target_term, match_type, priority, "
                f"enabled, remark, create_time, update_time FROM t_query_term_mapping "
                f"{clause} ORDER BY priority ASC, update_time DESC LIMIT %s OFFSET %s",
                params + [limit, offset])
            rows = cur.fetchall()
            cur.execute(f"SELECT count(*) FROM t_query_term_mapping {clause}", params)
            total = cur.fetchone()[0]
        return total, [_row(r) for r in rows]


def _row(row) -> QueryTermMappingDO:
    return QueryTermMappingDO(
        id=row["id"], domain=row.get("domain"), source_term=row.get("source_term"),
        target_term=row.get("target_term"), match_type=row.get("match_type") or 1,
        priority=row.get("priority") or 0, enabled=row.get("enabled") or 0,
        remark=row.get("remark"), create_time=row.get("create_time"),
        update_time=row.get("update_time"))


class QueryTermMappingAdminService:
    """1:1 翻译 ragent QueryTermMappingAdminServiceImpl。"""

    def __init__(self, repo: QueryTermMappingRepository | None = None,
                 cache: QueryTermMappingCacheManager | None = None) -> None:
        self._repo = repo or QueryTermMappingRepository()
        self._cache = cache or QueryTermMappingCacheManager()

    def create(self, source_term, target_term, match_type=None, priority=None,
               enabled=None, remark=None) -> str:
        source_term = (source_term or "").strip() or None
        target_term = (target_term or "").strip() or None
        if not source_term:
            raise ClientException("原始词不能为空")
        if not target_term:
            raise ClientException("目标词不能为空")
        record = QueryTermMappingDO(
            source_term=source_term, target_term=target_term,
            match_type=match_type if match_type is not None else 1,
            priority=priority if priority is not None else 0,
            enabled=1 if enabled is None or enabled else 0,
            remark=(remark or "").strip() or None)
        pid = self._repo.insert(record)
        self._cache.clear_cache()
        return pid

    def update(self, mid, source_term=None, target_term=None, match_type=None,
               priority=None, enabled=None, remark=None) -> None:
        record = self._load(mid)
        if source_term is not None:
            s = (source_term or "").strip() or None
            if not s:
                raise ClientException("原始词不能为空")
            record.source_term = s
        if target_term is not None:
            t = (target_term or "").strip() or None
            if not t:
                raise ClientException("目标词不能为空")
            record.target_term = t
        if match_type is not None:
            record.match_type = match_type
        if priority is not None:
            record.priority = priority
        if enabled is not None:
            record.enabled = 1 if enabled else 0
        if remark is not None:
            record.remark = (remark or "").strip() or None
        self._repo.update(record)
        self._cache.clear_cache()

    def delete(self, mid) -> None:
        self._load(mid)
        self._repo.delete(mid)
        self._cache.clear_cache()

    def query_by_id(self, mid) -> dict:
        return self._to_vo(self._load(mid))

    def page_query(self, keyword: str | None, current: int, size: int) -> dict:
        total, rows = self._repo.select_page(
            (keyword or "").strip() or None, size, (current - 1) * size)
        return {
            "current": current, "size": size, "total": total,
            "records": [self._to_vo(r) for r in rows],
        }

    def _load(self, mid) -> QueryTermMappingDO:
        record = self._repo.select_by_id(mid)
        if record is None:
            raise ClientException("映射规则不存在")
        return record

    @staticmethod
    def _to_vo(record: QueryTermMappingDO) -> dict:
        return {
            "id": record.id, "sourceTerm": record.source_term,
            "targetTerm": record.target_term, "matchType": record.match_type,
            "priority": record.priority, "enabled": record.enabled == 1,
            "remark": record.remark,
            "createTime": record.create_time.isoformat() if record.create_time else None,
            "updateTime": record.update_time.isoformat() if record.update_time else None,
        }


def build_router() -> "APIRouter":
    """1:1 翻译 ragent QueryTermMappingController。"""
    from fastapi import APIRouter

    router = APIRouter()
    svc = QueryTermMappingAdminService()

    @router.get("")
    def page_query(keyword: str | None = None, current: int = 1, size: int = 10) -> dict:
        return svc.page_query(keyword, current, size)

    @router.get("/{mid}")
    def query_by_id(mid: str) -> dict:
        return svc.query_by_id(mid)

    @router.post("")
    def create(source_term: str, target_term: str, match_type: int = 1,
               priority: int = 0, enabled: bool = True, remark: str | None = None) -> dict:
        return {"id": svc.create(source_term, target_term, match_type, priority, enabled, remark)}

    @router.put("/{mid}")
    def update(mid: str, source_term: str | None = None, target_term: str | None = None,
               match_type: int | None = None, priority: int | None = None,
               enabled: bool | None = None, remark: str | None = None) -> dict:
        svc.update(mid, source_term, target_term, match_type, priority, enabled, remark)
        return {"success": True}

    @router.delete("/{mid}")
    def delete(mid: str) -> dict:
        svc.delete(mid)
        return {"success": True}

    return router
