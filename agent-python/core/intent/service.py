"""意图树服务接口：严格对齐 ragent ingestion.service.IntentTreeService。

接口方法（严格对齐 ragent IntentTreeService 8 方法）：
  - getFullTree()                — 查询整棵意图树
  - createNode(req)              — 单节点创建
  - updateNode(id, req)          — 单节点更新
  - deleteNode(id)               — 单节点删除
  - batchEnableNodes(ids)        — 批量启用
  - batchDisableNodes(ids)       — 批量禁用
  - batchDeleteNodes(ids)        — 批量删除
  - initFromFactory()            — 从 IntentTreeFactory 初始化全量到 DB

实现：InMemoryIntentTreeService（内存版，用于测试/无 DB 场景）。
DatabaseIntentTreeService（SQLAlchemy 版，接口预留，TODO）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from core.intent.types import IntentNode
from core.intent.loader import InMemoryIntentLoader


class IntentTreeService(ABC):
    """严格对应 ragent ingestion.service.IntentTreeService（8 方法）。"""

    @abstractmethod
    def get_full_tree(self) -> list[IntentNode]:
        """严格对应 ragent IntentTreeService.getFullTree()。"""
        ...

    @abstractmethod
    def create_node(self, node: IntentNode) -> str:
        """严格对应 ragent IntentTreeService.createNode(req)。返回节点 id。"""
        ...

    @abstractmethod
    def update_node(self, id: str, node: IntentNode) -> None:
        """严格对应 ragent IntentTreeService.updateNode(id, req)。"""
        ...

    @abstractmethod
    def delete_node(self, id: str) -> None:
        """严格对应 ragent IntentTreeService.deleteNode(id)。"""
        ...

    @abstractmethod
    def batch_enable_nodes(self, ids: list[str]) -> None:
        """严格对应 ragent IntentTreeService.batchEnableNodes(ids)。"""
        ...

    @abstractmethod
    def batch_disable_nodes(self, ids: list[str]) -> None:
        """严格对应 ragent IntentTreeService.batchDisableNodes(ids)。"""
        ...

    @abstractmethod
    def batch_delete_nodes(self, ids: list[str]) -> None:
        """严格对应 ragent IntentTreeService.batchDeleteNodes(ids)。"""
        ...

    @abstractmethod
    def init_from_factory(self) -> int:
        """严格对应 ragent IntentTreeService.initFromFactory()。返回新增节点数。"""
        ...


class InMemoryIntentTreeService(IntentTreeService):
    """内存版 IntentTreeService——用于测试/无 DB 场景。"""

    def __init__(self, loader: InMemoryIntentLoader | None = None, cache_manager=None) -> None:
        self._loader = loader or InMemoryIntentLoader()
        self._cache = cache_manager
        self._nodes: list[IntentNode] = self._loader.load()

    def get_full_tree(self) -> list[IntentNode]:
        return self._nodes

    def create_node(self, node: IntentNode) -> str:
        if any(n.id == node.id for n in self._nodes):
            raise ValueError(f"节点已存在: {node.id}")
        self._nodes.append(node)
        self._invalidate_cache()
        return node.id

    def update_node(self, id: str, node: IntentNode) -> None:
        for i, n in enumerate(self._nodes):
            if n.id == id:
                self._nodes[i] = node
                self._invalidate_cache()
                return
        raise KeyError(f"节点不存在: {id}")

    def delete_node(self, id: str) -> None:
        self._nodes = [n for n in self._nodes if n.id != id]
        self._invalidate_cache()

    def batch_enable_nodes(self, ids: list[str]) -> None:
        for n in self._nodes:
            if n.id in ids:
                n.enabled = True
        self._invalidate_cache()

    def batch_disable_nodes(self, ids: list[str]) -> None:
        for n in self._nodes:
            if n.id in ids:
                n.enabled = False
        self._invalidate_cache()

    def batch_delete_nodes(self, ids: list[str]) -> None:
        self._nodes = [n for n in self._nodes if n.id not in ids]
        self._invalidate_cache()

    def init_from_factory(self) -> int:
        from core.intent.factory import build_default_intent_tree
        defaults = build_default_intent_tree()
        count = 0
        for n in defaults:
            if not any(existing.id == n.id for existing in self._nodes):
                self._nodes.append(n)
                count += 1
        self._invalidate_cache()
        return count

    def _invalidate_cache(self) -> None:
        if self._cache is not None:
            self._cache.clear()
