"""1:1 翻译 ragent rag.core.retrieval.channel.KbCollectionProvider。

接口语义：
  - listActiveCollections() → List[str]：未删除知识库的 collection 名称（去重、去空）
  - 全局检索（向量 / 关键词）的唯一「全库范围」来源：只返回未删除知识库的 collection
  - 两路全局检索共用此处，保证「全局」语义一致

差异：ragent 走 MyBatis-Plus KnowledgeBaseMapper selectList(... eq deleted=0)
      agent-python 没有持久层 → 接受可注入的 provider 函数（默认从环境变量 KB_ACTIVE_COLLECTIONS
      或 RAGConfigProperties.activeCollections 读），行为等价
"""
from __future__ import annotations
import logging

import os
from typing import Callable

_logger = logging.getLogger(__name__)


_KbCollectionSource = Callable[[], list[str]]


def _default_source() -> list[str]:
    """1:1 翻译 ragent KbCollectionProvider.listActiveCollections 默认实现：
    1) 环境变量 KB_ACTIVE_COLLECTIONS（逗号分隔）
    2) RAGConfigProperties.activeCollections（如果已实例化）
    3) 空 list
    """
    env_value = os.environ.get("KB_ACTIVE_COLLECTIONS", "").strip()
    if env_value:
        return [s.strip() for s in env_value.split(",") if s.strip()]
    try:
        from core.config.rag_config import RAGConfigProperties
        active = (RAGConfigProperties().active_collections or [])
        return [s for s in active if s and s.strip()]
    except Exception as e:  # noqa: BLE001
        _logger.debug("读取 active collections 失败，回退空列表: %s", e)
        return []


class KbCollectionProvider:
    """1:1 翻译 ragent KbCollectionProvider。

    source: 可注入的「数据源」函数，返回 list[str]。
    替换 source = lambda: ["kb_A", "kb_B"] 即可对接 Python 端持久化层（pymongo / SQLAlchemy / Milvus collection 列表）。
    """
    def __init__(self, source: _KbCollectionSource | None = None):
        self._source = source or _default_source

    def list_active_collections(self) -> list[str]:
        """1:1 翻译 ragent KbCollectionProvider.listActiveCollections：
        map + filter(StrUtil::isNotBlank) + distinct。
        """
        items = self._source() or []
        out: list[str] = []
        seen: set[str] = set()
        for s in items:
            if not s or not str(s).strip():
                continue
            v = str(s).strip()
            if v in seen:
                continue
            seen.add(v)
            out.append(v)
        return out
