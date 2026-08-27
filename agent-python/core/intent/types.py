"""意图树类型：严格对齐 ragent rag.core.intent.* + rag.enums.*。

- IntentKind：3 枚举（KB/MCP/SYSTEM）—— 严格对齐 ragent rag.enums.IntentKind
- IntentLevel：3 枚举（DOMAIN/CATEGORY/TOPIC）—— 严格对齐 ragent rag.enums.IntentLevel
- IntentNode：14 字段（含 prompt 模板 3 字段）—— 严格对齐 ragent IntentNodeDO + IntentNode
- NodeScore：节点打分（node + score + reason）—— 严格对齐 ragent NodeScore
- IntentTree：树形结构（roots + 子节点树）—— 严格对齐 ragent IntentTreeFactory.buildIntentTree()
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class IntentKind(str, Enum):
    """严格对应 ragent rag.enums.IntentKind。"""

    KB = "KB"
    MCP = "MCP"
    SYSTEM = "SYSTEM"


class IntentLevel(str, Enum):
    """严格对应 ragent rag.enums.IntentLevel。"""

    DOMAIN = "DOMAIN"
    CATEGORY = "CATEGORY"
    TOPIC = "TOPIC"


@dataclass
class IntentNode:
    """严格对应 ragent rag.core.intent.IntentNode（14 字段）。

    字段映射（java IntentNodeDO → python IntentNode）：
      intentCode        → id
      name              → name
      description       → description
      kind              → kind（IntentKind 枚举）
      level             → level（IntentLevel 枚举）
      parentCode        → parent_id
      kbId              → kb_id
      mcpToolId         → tool_id
      collectionNames   → collection_names
      examples          → examples（list[str]）
      topK              → top_k
      sortOrder         → sort_order
      enabled           → enabled
      paramPromptTemplate → param_prompt_template
      promptSnippet       → prompt_snippet
      promptTemplate      → prompt_template
    """
    id: str
    name: str
    description: str = ""
    kind: IntentKind = IntentKind.KB
    level: IntentLevel = IntentLevel.TOPIC
    parent_id: str | None = None
    kb_id: str | None = None
    tool_id: str | None = None
    collection_names: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    top_k: int | None = None
    sort_order: int = 0
    enabled: bool = True
    prompt_template: str | None = None
    param_prompt_template: str | None = None
    prompt_snippet: str | None = None
    children: list["IntentNode"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "kind": self.kind.value, "level": self.level.value,
            "parent_id": self.parent_id, "kb_id": self.kb_id, "tool_id": self.tool_id,
            "collection_names": self.collection_names, "examples": self.examples,
            "top_k": self.top_k, "sort_order": self.sort_order, "enabled": self.enabled,
        }


@dataclass
class NodeScore:
    """严格对应 ragent NodeScore。"""

    node: IntentNode
    score: float
    reason: str = ""


@dataclass
class IntentTree:
    """严格对应 ragent IntentTreeFactory.buildIntentTree() 返回的根节点列表。"""

    roots: list[IntentNode] = field(default_factory=list)

    def leaf_nodes(self) -> list[IntentNode]:
        """返回所有叶节点（无子节点的节点）—— 严格对齐 ragent IntentTreeCacheManager 取的"叶子"。"""
        all_nodes = self._all_nodes()
        parent_ids = {n.parent_id for n in all_nodes if n.parent_id}
        return [n for n in all_nodes if n.id not in parent_ids]

    def _all_nodes(self) -> list[IntentNode]:
        result: list[IntentNode] = []
        stack = list(self.roots)
        while stack:
            node = stack.pop()
            result.append(node)
            stack.extend(node.children)
        return result

    def _children_of(self, parent: IntentNode) -> list[IntentNode]:
        return list(parent.children)

    def _all_flat(self) -> list[IntentNode]:
        return self._all_nodes()
