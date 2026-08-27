"""管理后台：1:1 翻译 ragent 示例问题 / 关键词映射 / 评测接口 / 知识库管理。"""
from core.admin.sample_question import (
    SampleQuestionDO, SampleQuestionRepository, SampleQuestionService, build_router as build_sample_question_router)
from core.admin.query_term_mapping import (
    QueryTermMappingDO, QueryTermMappingRepository, QueryTermMappingCacheManager,
    QueryTermMappingAdminService, build_router as build_query_term_mapping_router)
from core.admin.eval import EvalProperties, EvalResponse, build_eval_router
from core.admin.knowledge import (
    build_knowledge_base_router, build_knowledge_document_router, build_knowledge_chunk_router)

__all__ = [
    "SampleQuestionDO", "SampleQuestionRepository", "SampleQuestionService",
    "build_sample_question_router",
    "QueryTermMappingDO", "QueryTermMappingRepository", "QueryTermMappingCacheManager",
    "QueryTermMappingAdminService", "build_query_term_mapping_router",
    "EvalProperties", "EvalResponse", "build_eval_router",
    "build_knowledge_base_router", "build_knowledge_document_router", "build_knowledge_chunk_router",
]
