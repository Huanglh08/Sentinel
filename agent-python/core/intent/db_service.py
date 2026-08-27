"""Database 持久化：严格对齐 ragent ingestion.service.impl.IntentTreeServiceImpl + rag.dao.*。

SQLAlchemy 实现：DatabaseIntentTreeService（仿 MyBatis-Plus BaseMapper 风格）。
IntentNodeTable 19 字段映射 ragent IntentNodeDO，外部 API 暴露 IntentNode dataclass。

行为（与 ragent IntentTreeServiceImpl 一致）：
  - getFullTree：先查 Redis 缓存，命中返缓存；未命中查 DB 并写回缓存
  - 增删改：操作 DB + 清 Redis 缓存（@Transactional + cacheManager.clearCache）
  - 批量操作：IN 查询 + 一次性 UPDATE
  - initFromFactory：从 IntentTreeFactory 全量初始化（已存在则跳过）
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import create_engine, Column, String, Integer, Text, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from core.intent.types import IntentNode, IntentKind, IntentLevel
from core.intent.service import IntentTreeService
from core.intent.cache import IntentTreeCacheManager

Base = declarative_base()


class IntentNodeTable(Base):
    """严格对应 ragent rag.dao.entity.IntentNodeDO（19 字段）。"""
    __tablename__ = "intent_node"

    id = Column(String(64), primary_key=True)
    kb_id = Column(String(64))
    intent_code = Column(String(128), index=True)
    name = Column(String(128))
    level = Column(Integer)  # 0=DOMAIN, 1=CATEGORY, 2=TOPIC
    parent_code = Column(String(128), index=True)
    description = Column(Text)
    examples = Column(Text)  # JSON 数组
    collection_name = Column(String(128))
    collection_names = Column(Text)  # JSON 数组
    mcp_tool_id = Column(String(128))
    top_k = Column(Integer)
    kind = Column(Integer)  # 0=KB, 1=SYSTEM, 2=MCP
    sort_order = Column(Integer, default=0)
    prompt_snippet = Column(Text)
    prompt_template = Column(Text)
    param_prompt_template = Column(Text)
    enabled = Column(Integer, default=1)
    create_by = Column(String(64))
    update_by = Column(String(64))
    create_time = Column(DateTime, default=datetime.now)
    update_time = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    deleted = Column(Integer, default=0)  # @TableLogic 逻辑删除


class DatabaseIntentTreeService(IntentTreeService):
    """严格对应 ragent ingestion.service.impl.IntentTreeServiceImpl。"""

    def __init__(self, db_url: str, cache_manager: IntentTreeCacheManager | None = None) -> None:
        self._engine = create_engine(db_url)
        self._Session = sessionmaker(bind=self._engine)
        Base.metadata.create_all(self._engine)
        self._cache = cache_manager

    def _session(self) -> Session:
        return self._Session()

    def _invalidate_cache(self) -> None:
        if self._cache is not None:
            self._cache.clear()

    def _to_node(self, row: Any) -> IntentNode:
        return IntentNode(
            id=row.intent_code or row.id,
            name=row.name or "",
            description=row.description or "",
            kind=IntentKind({0: "KB", 1: "SYSTEM", 2: "MCP"}.get(row.kind or 0, "KB")),
            level=IntentLevel({0: "DOMAIN", 1: "CATEGORY", 2: "TOPIC"}.get(row.level or 2, "TOPIC")),
            parent_id=row.parent_code,
            kb_id=row.kb_id,
            tool_id=row.mcp_tool_id,
            collection_names=json.loads(row.collection_names) if row.collection_names else [],
            examples=json.loads(row.examples) if row.examples else [],
            top_k=row.top_k,
            sort_order=row.sort_order or 0,
            enabled=bool(row.enabled),
            prompt_template=row.prompt_template,
            param_prompt_template=row.param_prompt_template,
            prompt_snippet=row.prompt_snippet,
        )

    def _to_row(self, node: IntentNode) -> dict[str, Any]:
        return {
            "intent_code": node.id,
            "name": node.name,
            "level": {"DOMAIN": 0, "CATEGORY": 1, "TOPIC": 2}.get(node.level.value, 2),
            "kind": {"KB": 0, "SYSTEM": 1, "MCP": 2}.get(node.kind.value, 0),
            "parent_code": node.parent_id,
            "kb_id": node.kb_id,
            "description": node.description,
            "examples": json.dumps(node.examples, ensure_ascii=False),
            "mcp_tool_id": node.tool_id,
            "collection_names": json.dumps(node.collection_names, ensure_ascii=False),
            "top_k": node.top_k,
            "sort_order": node.sort_order,
            "enabled": 1 if node.enabled else 0,
            "prompt_template": node.prompt_template,
            "param_prompt_template": node.param_prompt_template,
            "prompt_snippet": node.prompt_snippet,
            "deleted": 0,
        }

    def _dict_to_node(self, d: dict) -> IntentNode:
        return IntentNode(
            id=d["id"], name=d["name"],
            description=d.get("description", ""),
            kind=IntentKind(d.get("kind", "KB")),
            level=IntentLevel(d.get("level", "TOPIC")),
            parent_id=d.get("parent_id"),
            kb_id=d.get("kb_id"),
            tool_id=d.get("tool_id"),
            collection_names=d.get("collection_names", []),
            examples=d.get("examples", []),
            top_k=d.get("top_k"),
            sort_order=d.get("sort_order", 0),
            enabled=d.get("enabled", True),
        )

    def get_full_tree(self) -> list[IntentNode]:
        """严格对齐 ragent getFullTree：先 Redis 缓存，后 DB。"""
        if self._cache is not None:
            cached = self._cache.get()
            if cached is not None:
                return [self._dict_to_node(d) for d in cached]
        with self._session() as s:
            rows = s.query(IntentNodeTable).filter(IntentNodeTable.deleted == 0).all()
            nodes = [self._to_node(r) for r in rows]
        if self._cache is not None:
            self._cache.save([n.to_dict() for n in nodes])
        return nodes

    def create_node(self, node: IntentNode) -> str:
        with self._session() as s:
            row = IntentNodeTable(**self._to_row(node))
            s.add(row)
            s.commit()
        self._invalidate_cache()
        return node.id

    def update_node(self, id: str, node: IntentNode) -> None:
        with self._session() as s:
            row = s.query(IntentNodeTable).filter(IntentNodeTable.intent_code == id).first()
            if row is None:
                raise KeyError(f"节点不存在: {id}")
            for k, v in self._to_row(node).items():
                setattr(row, k, v)
            s.commit()
        self._invalidate_cache()

    def delete_node(self, id: str) -> None:
        with self._session() as s:
            row = s.query(IntentNodeTable).filter(IntentNodeTable.intent_code == id).first()
            if row is not None:
                row.deleted = 1
                s.commit()
        self._invalidate_cache()

    def batch_enable_nodes(self, ids: list[str]) -> None:
        with self._session() as s:
            s.query(IntentNodeTable).filter(
                IntentNodeTable.intent_code.in_(ids)
            ).update({IntentNodeTable.enabled: 1}, synchronize_session=False)
            s.commit()
        self._invalidate_cache()

    def batch_disable_nodes(self, ids: list[str]) -> None:
        with self._session() as s:
            s.query(IntentNodeTable).filter(
                IntentNodeTable.intent_code.in_(ids)
            ).update({IntentNodeTable.enabled: 0}, synchronize_session=False)
            s.commit()
        self._invalidate_cache()

    def batch_delete_nodes(self, ids: list[str]) -> None:
        with self._session() as s:
            s.query(IntentNodeTable).filter(
                IntentNodeTable.intent_code.in_(ids)
            ).update({IntentNodeTable.deleted: 1}, synchronize_session=False)
            s.commit()
        self._invalidate_cache()

    def init_from_factory(self) -> int:
        """严格对齐 ragent initFromFactory：从 IntentTreeFactory 全量初始化。"""
        from core.intent.factory import build_default_intent_tree
        defaults = build_default_intent_tree()
        count = 0
        with self._session() as s:
            existing = {r.intent_code for r in s.query(IntentNodeTable).all()}
            for n in defaults:
                if n.id not in existing:
                    s.add(IntentNodeTable(**self._to_row(n)))
                    count += 1
            s.commit()
        self._invalidate_cache()
        return count
