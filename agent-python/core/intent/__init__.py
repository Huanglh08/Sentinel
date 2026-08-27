"""意图识别包：严格对齐 ragent rag.core.intent.* + rag.enums.* + ingestion.service.*。

文件结构：
   - types.py：IntentKind / IntentLevel / IntentNode / NodeScore / IntentTree
   - cache.py：IntentTreeCacheManager（Redis 7 天 TTL）
   - factory.py：IntentTreeFactory（3 维树形）+ DEFAULT_INTENT_TREE
   - loader.py：IntentTreeLoader 接口 + InMemory / Redis 两种实现
   - service.py：IntentTreeService 接口 + InMemoryIntentTreeService 内存实现
   - resolver.py：IntentResolver（LLM 打分）
   - guidance.py：IntentGuidanceService（歧义引导）

向后兼容：导出旧 IntentResolver / IntentNode / NodeScore 别名。
"""
from core.intent.types import IntentKind, IntentLevel, IntentNode, NodeScore, IntentTree
from core.intent.factory import DEFAULT_INTENT_TREE, build_default_intent_tree
from core.intent.cache import IntentTreeCacheManager
from core.intent.loader import (
    IntentTreeLoader, InMemoryIntentLoader, RedisBackedIntentLoader,
)
from core.intent.service import IntentTreeService, InMemoryIntentTreeService
from core.intent.resolver import IntentResolver
from core.intent.sub_question_intent import SubQuestionIntent
from core.intent.guidance import IntentGuidanceService, GuidanceDecision
from core.intent.db_service import DatabaseIntentTreeService, IntentNodeTable  # noqa: F401

__all__ = [
    # 类型
    "IntentKind", "IntentLevel", "IntentNode", "NodeScore", "IntentTree", "SubQuestionIntent",
    # 工厂 + 默认树
    "DEFAULT_INTENT_TREE", "build_default_intent_tree",
    # 缓存
    "IntentTreeCacheManager",
    # 加载器
    "IntentTreeLoader", "InMemoryIntentLoader", "RedisBackedIntentLoader",
    # 服务
    "IntentTreeService", "InMemoryIntentTreeService", "DatabaseIntentTreeService", "IntentNodeTable",
    # 解析器（向后兼容）
    "IntentResolver",
    # 歧义引导
    "IntentGuidanceService", "GuidanceDecision",
]
