"""共享 PostgreSQL 连接池：1:1 对齐 ragent 的 DataSource / JdbcTemplate 访问层。

ragent 的对话记忆、摘要、Agent 画像、RAG 链路追踪、管理后台 CRUD 等全部读写同一份
`ragent` 库（与 Java 入库共用），因此这里提供统一的 psycopg_pool 连接池，供各移植模块复用，
避免每个模块各自建池。DSN 默认对齐 ragent application.yaml 的 datasource 配置。
"""
from __future__ import annotations

import logging
import os
import threading
import time

_logger = logging.getLogger("sentinel.db")

_DEFAULT_DSN = "postgresql://postgres:postgres@127.0.0.1:5432/sentinel?client_encoding=UTF8"

_lock = threading.Lock()
_pool = None


def get_dsn() -> str:
    """1:1 翻译 ragent spring.datasource.url 注入（环境变量优先）。"""
    return os.getenv("PG_DSN", _DEFAULT_DSN)


def get_pool(max_size: int = 8):
    """返回进程级共享的 psycopg_pool.ConnectionPool（懒加载、线程安全）。"""
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                try:
                    import psycopg_pool
                except ImportError as e:  # pragma: no cover
                    raise ImportError(
                        "共享 DB 池需要 psycopg_pool，请执行：\n  pip install psycopg_pool"
                    ) from e
                _pool = psycopg_pool.ConnectionPool(
                    get_dsn(), open=True, max_size=max_size, kwargs={"autocommit": True}
                )
                _logger.info("共享 DB 连接池已创建 - dsn=%s", _mask_dsn(get_dsn()))
    return _pool


def _mask_dsn(dsn: str) -> str:
    # 避免日志泄露密码
    try:
        from urllib.parse import urlparse

        p = urlparse(dsn)
        return f"{p.scheme}://{p.hostname}:{p.port}{p.path}"
    except Exception:  # pragma: no cover
        return "***"


def close_pool() -> None:
    """进程退出时关闭连接池（对应 ragent DataSource 销毁）。"""
    global _pool
    if _pool is not None:
        with _lock:
            if _pool is not None:
                _pool.close()
                _pool = None


_hidden_cache: tuple[float, list[str]] = (0.0, [])


def hidden_collections(ttl_s: int = 60) -> list[str]:
    """返回对普通用户隐藏（visible_to_user=0）的 collection 名列表。

    60s TTL 缓存；查询失败沿用旧缓存（首次失败返回空 = fail-open：
    ponytail — DB 不可用时 pgvector 检索本身也不可用，不再加独立降级分支）。
    """
    global _hidden_cache
    ts, cached = _hidden_cache
    now = time.monotonic()
    if now - ts < ttl_s:
        return cached
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT collection_name FROM t_knowledge_base WHERE visible_to_user = 0 AND deleted = 0"
            ).fetchall()
        result = [r[0] for r in rows]
        _hidden_cache = (now, result)
        return result
    except Exception as e:
        _logger.warning("hidden_collections 查询失败，沿用旧缓存: %s", e)
        return cached
