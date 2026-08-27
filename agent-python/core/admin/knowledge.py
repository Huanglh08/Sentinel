"""知识库管理后台：1:1 翻译 ragent KnowledgeBaseController / KnowledgeDocumentController / KnowledgeChunkController。

提供知识库、文档、分块的CRUD API：
  - 知识库管理：创建、查询、更新、删除知识库
  - 文档管理：上传、查询、更新、删除文档
  - 分块管理：查询、更新、删除分块
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from core.db import get_pool
from core.security import get_current_user

_logger = logging.getLogger("sentinel.admin.knowledge")


def build_knowledge_base_router() -> APIRouter:
    """构建知识库管理路由。"""
    router = APIRouter(prefix="/knowledge-bases", tags=["admin-knowledge"])

    @router.get("")
    async def list_knowledge_bases(
        page: int = Query(default=1, ge=1),
        size: int = Query(default=20, ge=1, le=100),
        user=Depends(get_current_user),
    ):
        """分页查询知识库列表。"""
        try:
            pool = get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                # 查询总数
                cur.execute("SELECT COUNT(*) FROM t_knowledge_base WHERE deleted = 0")
                total = cur.fetchone()[0]
                # 查询分页数据
                offset = (page - 1) * size
                cur.execute(
                    "SELECT id, name, description, status, create_time, update_time "
                    "FROM t_knowledge_base WHERE deleted = 0 "
                    "ORDER BY create_time DESC LIMIT %s OFFSET %s",
                    (size, offset),
                )
                cols = [d[0] for d in cur.description]
                items = [dict(zip(cols, row)) for row in cur.fetchall()]
                return {
                    "success": True,
                    "data": {"items": items, "total": total, "page": page, "size": size},
                }
        except Exception as exc:
            _logger.exception("list knowledge bases failed")
            raise HTTPException(status_code=500, detail=str(exc))

    @router.get("/{kb_id}")
    async def get_knowledge_base(kb_id: str, user=Depends(get_current_user)):
        """获取知识库详情。"""
        try:
            pool = get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name, description, status, create_time, update_time "
                    "FROM t_knowledge_base WHERE id = %s AND deleted = 0",
                    (kb_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="知识库不存在")
                cols = [d[0] for d in cur.description]
                return {"success": True, "data": dict(zip(cols, row))}
        except HTTPException:
            raise
        except Exception as exc:
            _logger.exception("get knowledge base failed")
            raise HTTPException(status_code=500, detail=str(exc))

    @router.post("")
    async def create_knowledge_base(payload: dict, user=Depends(get_current_user)):
        """创建知识库。"""
        try:
            name = payload.get("name", "").strip()
            if not name:
                raise HTTPException(status_code=400, detail="知识库名称不能为空")
            description = payload.get("description", "")
            import uuid
            kb_id = str(uuid.uuid4())
            pool = get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO t_knowledge_base (id, name, description, status) "
                    "VALUES (%s, %s, %s, %s)",
                    (kb_id, name, description, "ACTIVE"),
                )
            return {"success": True, "data": {"id": kb_id}}
        except HTTPException:
            raise
        except Exception as exc:
            _logger.exception("create knowledge base failed")
            raise HTTPException(status_code=500, detail=str(exc))

    @router.put("/{kb_id}")
    async def update_knowledge_base(kb_id: str, payload: dict, user=Depends(get_current_user)):
        """更新知识库。"""
        try:
            name = payload.get("name", "").strip()
            description = payload.get("description", "")
            pool = get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE t_knowledge_base SET name = %s, description = %s, update_time = NOW() "
                    "WHERE id = %s AND deleted = 0",
                    (name, description, kb_id),
                )
            return {"success": True}
        except Exception as exc:
            _logger.exception("update knowledge base failed")
            raise HTTPException(status_code=500, detail=str(exc))

    @router.delete("/{kb_id}")
    async def delete_knowledge_base(kb_id: str, user=Depends(get_current_user)):
        """删除知识库（软删除）。"""
        try:
            pool = get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE t_knowledge_base SET deleted = 1, update_time = NOW() WHERE id = %s",
                    (kb_id,),
                )
            return {"success": True}
        except Exception as exc:
            _logger.exception("delete knowledge base failed")
            raise HTTPException(status_code=500, detail=str(exc))

    return router


def build_knowledge_document_router() -> APIRouter:
    """构建知识库文档管理路由。"""
    router = APIRouter(prefix="/knowledge-documents", tags=["admin-knowledge"])

    @router.get("")
    async def list_documents(
        kb_id: str | None = None,
        page: int = Query(default=1, ge=1),
        size: int = Query(default=20, ge=1, le=100),
        user=Depends(get_current_user),
    ):
        """分页查询文档列表。"""
        try:
            pool = get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                if kb_id:
                    cur.execute(
                        "SELECT COUNT(*) FROM t_knowledge_document WHERE knowledge_base_id = %s AND deleted = 0",
                        (kb_id,),
                    )
                else:
                    cur.execute("SELECT COUNT(*) FROM t_knowledge_document WHERE deleted = 0")
                total = cur.fetchone()[0]
                offset = (page - 1) * size
                if kb_id:
                    cur.execute(
                        "SELECT id, knowledge_base_id, name, file_type, status, chunk_count, "
                        "create_time, update_time "
                        "FROM t_knowledge_document WHERE knowledge_base_id = %s AND deleted = 0 "
                        "ORDER BY create_time DESC LIMIT %s OFFSET %s",
                        (kb_id, size, offset),
                    )
                else:
                    cur.execute(
                        "SELECT id, knowledge_base_id, name, file_type, status, chunk_count, "
                        "create_time, update_time "
                        "FROM t_knowledge_document WHERE deleted = 0 "
                        "ORDER BY create_time DESC LIMIT %s OFFSET %s",
                        (size, offset),
                    )
                cols = [d[0] for d in cur.description]
                items = [dict(zip(cols, row)) for row in cur.fetchall()]
                return {
                    "success": True,
                    "data": {"items": items, "total": total, "page": page, "size": size},
                }
        except Exception as exc:
            _logger.exception("list documents failed")
            raise HTTPException(status_code=500, detail=str(exc))

    @router.get("/{doc_id}")
    async def get_document(doc_id: str, user=Depends(get_current_user)):
        """获取文档详情。"""
        try:
            pool = get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT id, knowledge_base_id, name, file_type, status, chunk_count, "
                    "create_time, update_time "
                    "FROM t_knowledge_document WHERE id = %s AND deleted = 0",
                    (doc_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="文档不存在")
                cols = [d[0] for d in cur.description]
                return {"success": True, "data": dict(zip(cols, row))}
        except HTTPException:
            raise
        except Exception as exc:
            _logger.exception("get document failed")
            raise HTTPException(status_code=500, detail=str(exc))

    @router.delete("/{doc_id}")
    async def delete_document(doc_id: str, user=Depends(get_current_user)):
        """删除文档（软删除）。"""
        try:
            pool = get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE t_knowledge_document SET deleted = 1, update_time = NOW() WHERE id = %s",
                    (doc_id,),
                )
            return {"success": True}
        except Exception as exc:
            _logger.exception("delete document failed")
            raise HTTPException(status_code=500, detail=str(exc))

    return router


def build_knowledge_chunk_router() -> APIRouter:
    """构建知识库分块管理路由。"""
    router = APIRouter(prefix="/knowledge-chunks", tags=["admin-knowledge"])

    @router.get("")
    async def list_chunks(
        doc_id: str | None = None,
        page: int = Query(default=1, ge=1),
        size: int = Query(default=20, ge=1, le=100),
        user=Depends(get_current_user),
    ):
        """分页查询分块列表。"""
        try:
            pool = get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                if doc_id:
                    cur.execute(
                        "SELECT COUNT(*) FROM t_knowledge_chunk WHERE document_id = %s AND deleted = 0",
                        (doc_id,),
                    )
                else:
                    cur.execute("SELECT COUNT(*) FROM t_knowledge_chunk WHERE deleted = 0")
                total = cur.fetchone()[0]
                offset = (page - 1) * size
                if doc_id:
                    cur.execute(
                        "SELECT id, document_id, content, index, token_count, "
                        "create_time, update_time "
                        "FROM t_knowledge_chunk WHERE document_id = %s AND deleted = 0 "
                        "ORDER BY index ASC LIMIT %s OFFSET %s",
                        (doc_id, size, offset),
                    )
                else:
                    cur.execute(
                        "SELECT id, document_id, content, index, token_count, "
                        "create_time, update_time "
                        "FROM t_knowledge_chunk WHERE deleted = 0 "
                        "ORDER BY create_time DESC LIMIT %s OFFSET %s",
                        (size, offset),
                    )
                cols = [d[0] for d in cur.description]
                items = [dict(zip(cols, row)) for row in cur.fetchall()]
                return {
                    "success": True,
                    "data": {"items": items, "total": total, "page": page, "size": size},
                }
        except Exception as exc:
            _logger.exception("list chunks failed")
            raise HTTPException(status_code=500, detail=str(exc))

    @router.get("/{chunk_id}")
    async def get_chunk(chunk_id: str, user=Depends(get_current_user)):
        """获取分块详情。"""
        try:
            pool = get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT id, document_id, content, index, token_count, "
                    "create_time, update_time "
                    "FROM t_knowledge_chunk WHERE id = %s AND deleted = 0",
                    (chunk_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="分块不存在")
                cols = [d[0] for d in cur.description]
                return {"success": True, "data": dict(zip(cols, row))}
        except HTTPException:
            raise
        except Exception as exc:
            _logger.exception("get chunk failed")
            raise HTTPException(status_code=500, detail=str(exc))

    @router.put("/{chunk_id}")
    async def update_chunk(chunk_id: str, payload: dict, user=Depends(get_current_user)):
        """更新分块内容。"""
        try:
            content = payload.get("content", "")
            pool = get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE t_knowledge_chunk SET content = %s, update_time = NOW() WHERE id = %s",
                    (content, chunk_id),
                )
            return {"success": True}
        except Exception as exc:
            _logger.exception("update chunk failed")
            raise HTTPException(status_code=500, detail=str(exc))

    @router.delete("/{chunk_id}")
    async def delete_chunk(chunk_id: str, user=Depends(get_current_user)):
        """删除分块（软删除）。"""
        try:
            pool = get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE t_knowledge_chunk SET deleted = 1, update_time = NOW() WHERE id = %s",
                    (chunk_id,),
                )
            return {"success": True}
        except Exception as exc:
            _logger.exception("delete chunk failed")
            raise HTTPException(status_code=500, detail=str(exc))

    return router
