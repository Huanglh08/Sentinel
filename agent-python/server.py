from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
import uuid

# 必须先于任何 os.getenv / from_env()：pydantic-settings 读 .env 不注入 os.environ，
# 裸读 os.environ 的配置（如 RAG_SEARCH_CHANNELS_KEYWORD_ENABLED、RAG_KEYWORD_CHANNEL_ENABLED）会静默失效
from dotenv import load_dotenv

load_dotenv()
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from core.agent import Agent
from core.agent_profile.service import ClientException
from core.case import CaseInvestigationAgent
from core.case.notify import notify_approval_decided
from core.case.patrol import CasePatroller
from core.case.responder import CaseResponder
from core.case.rule_miner import RuleMiner
from core.case.store import CaseStore, get_risk_policy, save_risk_policy
from core.case.types import (
    AUDIT_OPERATION_ACTION_EXECUTE,
    AUDIT_OPERATION_APPROVE,
    AUDIT_OPERATION_REJECT,
    AUDIT_OPERATION_RULE_APPROVED,
    AUDIT_OPERATION_RULE_REJECTED,
)
from core.chunk.chunker import chunk_file
from core.config import AppSettings, RAGConfigProperties, SearchChannelProperties
from core.embedding import EmbeddingConfig, ModelHealthStore as EmbeddingHealthStore, ModelRoutingExecutor as EmbeddingRoutingExecutor, RoutingEmbeddingService
from core.embedding.health import ModelHealthStore as LLMHealthStore
from core.embedding.clients.aihubmix import AIHubMixEmbeddingClient
from core.embedding.clients.ollama import OllamaEmbeddingClient
from core.embedding.clients.siliconflow import SiliconFlowEmbeddingClient
from core.llm import (
    AIModelProperties,
    AsyncRoutingLLMService,
    LLMClient,
    ModelRoutingExecutor,
    ModelSelector,
    RoutingLLMService,
)
from core.llm.providers.aihubmix import AIHubMixLLMService
from core.llm.providers.bailian import BaiLianLLMService
from core.llm.providers.ollama import OllamaLLMService
from core.llm.providers.siliconflow import SiliconFlowLLMService
from core.intent.cache import IntentTreeCacheManager
from core.intent.loader import InMemoryIntentLoader, RedisBackedIntentLoader
from core.intent.service import InMemoryIntentTreeService
from core.intent import IntentResolver
from core.retrieval.engine import ChannelConfig, HeadChunkDemotionPostProcessor, MultiChannelRetrievalEngine, RetrievalConfig
from core.retrieval.diversity import doc_diversity_backup
from core.retrieval.external_channels import LightRagBackend, LightRagConfig, YouComWebSearchBackend, WebSearchConfig
from core.retrieval.rerank import SiliconFlowReranker, LLMReranker, RoutingReranker
from core.retrieval.rerank_gate import GateResult, get_default_gate
from core.retrieval.scope_resolver import RetrievalScope
from core.retrieval.types import ChannelType, RetrievedChunk, RetrievalBudget
from core.security import SecurityService, get_current_user, get_security_service, get_settings
from core.tracing import Tracer
from core.admin import (
    build_sample_question_router, build_query_term_mapping_router, build_eval_router,
    build_knowledge_base_router, build_knowledge_document_router, build_knowledge_chunk_router)
from core.demo_mode import DemoModeMiddleware, DemoModeProperties
from core.ratelimit import ChatQueueLimiter, FairDistributedRateLimiter, RAGRateLimitProperties
from core.case.ids import next_message_id
from core.session_memory import SessionMemory

_logger = logging.getLogger("sentinel.api")
_settings = AppSettings()
_tracer = Tracer(enabled=True)
_rate_buckets: dict[str, deque[float]] = defaultdict(deque)


@dataclass
class StoredChunk:
    collection: str
    doc_name: str
    chunk: dict


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20000)
    conversationId: str | None = Field(default=None, max_length=128)
    taskId: str | None = Field(default=None, max_length=128)
    knowledgeBaseIds: list[str] | None = None
    history: list[dict] | None = Field(default=None, description="会话历史，[{role, content}]，由 Java 侧从会话存储读出传入")


class TitleRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20000)


class CaseInvestigateRequest(BaseModel):
    """案件调查请求：告警/线索文本 -> 自主调查 -> 结构化报告。"""

    clue: str = Field(min_length=5, max_length=20000, description="告警/线索原文")
    source: str = Field(default="MANUAL", max_length=32, description="线索来源: MANUAL/WEBHOOK/SCAN")
    alert_id: str = Field(default="", max_length=64, description="告警编号；传入后取证工具只返回该告警关联的记录（案件隔离）")


class RecommendRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20000)
    answer: str = Field(default="", max_length=60000)
    chunks: list[dict] | None = Field(default=None, description="[{doc_name, text}]")


class TextIngestRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10_000_000)
    filename: str = Field(default="input.txt", min_length=1, max_length=255)
    collection: str = Field(default="default", min_length=1, max_length=128)


class IngestRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=10_000_000)
    collection: str = Field(default="default", min_length=1, max_length=128)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=20000)
    top_k: int = Field(default=6, ge=1, le=100)
    collections: list[str] = Field(default_factory=list)


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.settings = AppSettings()
    application.state.security = SecurityService(application.state.settings)
    application.state.documents = _load_documents()
    application.state.embedding_config = EmbeddingConfig()
    application.state.embedding_health = EmbeddingHealthStore()
    application.state.embedding_executor = EmbeddingRoutingExecutor(
        application.state.embedding_config,
        application.state.embedding_health,
        {
            "ollama": OllamaEmbeddingClient(),
            "siliconflow": SiliconFlowEmbeddingClient(),
            "aihubmix": AIHubMixEmbeddingClient(),
        },
    )
    application.state.embedding_service = RoutingEmbeddingService(
        application.state.embedding_config,
        application.state.embedding_health,
        application.state.embedding_executor,
    )
    application.state.rag_config = RAGConfigProperties.from_env()
    application.state.search_channel_config = SearchChannelProperties.from_env()
    application.state.search_channel_config.validate()
    application.state.retrieval_engine = None
    application.state.agent = None
    application.state.intent_resolver = None
    application.state.intent_cache_manager = None
    application.state.intent_redis = None
    application.state.task_registry = {}
    # 1:1 ragent ChatQueueLimiter/FairDistributedRateLimiter bean init
    rate_limit_properties = RAGRateLimitProperties.from_env()
    fair_limiter = FairDistributedRateLimiter(
        name="sentinel:rag:chat",
        redis_url=getattr(_settings, "redis_url", None),
        max_permits=rate_limit_properties.global_max_concurrent,
        lease_seconds=rate_limit_properties.global_lease_seconds,
        poll_interval_ms=rate_limit_properties.global_poll_interval_ms,
    )
    application.state.queue_limiter = ChatQueueLimiter(fair_limiter)
    application.state.rate_limit_properties = rate_limit_properties
    # 1:1 ragent @PostConstruct: start limiter lifecycle inside the event loop
    try:
        await fair_limiter.start()
    except Exception as exc:  # noqa: BLE001
        # Redis unreachable: skip queue limiting instead of failing startup
        _logger.warning("queue limiter disabled (redis unavailable): %s", exc)
        application.state.queue_limiter = None
    # 会话状态卡（agent 自主记忆）：建表 + 挂 app.state；失败不阻塞启动（chat 内再降级）
    try:
        _session_memory = SessionMemory()
        _session_memory.ensure_table()
        application.state.session_memory = _session_memory
    except Exception as exc:  # noqa: BLE001
        _logger.warning("session memory 初始化失败（状态卡将不可用）: %s", exc)
        application.state.session_memory = None
    refresh_runtime_components(application)
    # Phase 4: 定时巡检批处理（确定性调度与 Agent 分离；默认关闭 CASE_PATROL_INTERVAL<=0）
    try:
        _patrol_interval = float(os.getenv("CASE_PATROL_INTERVAL", "0").strip() or "0")
    except ValueError:
        _patrol_interval = 0.0
    application.state.case_patroller = None
    if _patrol_interval > 0:
        patroller = CasePatroller(
            # Agent 懒加载工厂：首个请求后才可用，巡检轮次运行时解析
            case_agent=lambda: getattr(application.state, "case_agent", None),
            store=CaseStore(),
        )
        if patroller.start():
            application.state.case_patroller = patroller
    yield
    _patroller = getattr(application.state, "case_patroller", None)
    if _patroller is not None:
        _patroller.shutdown()
    redis_client = getattr(application.state, "intent_redis", None)
    if redis_client is not None:
        try:
            redis_client.close()
        except Exception:  # noqa: BLE001
            pass


app = FastAPI(title="sentinel-agent", version=_settings.app_version, lifespan=lifespan)
app.state.settings = _settings
app.state.security = SecurityService(_settings)
app.state.documents = []
app.state.embedding_config = EmbeddingConfig()
app.state.embedding_health = EmbeddingHealthStore()
app.state.embedding_executor = EmbeddingRoutingExecutor(
    app.state.embedding_config,
    app.state.embedding_health,
    {
        "ollama": OllamaEmbeddingClient(),
        "siliconflow": SiliconFlowEmbeddingClient(),
        "aihubmix": AIHubMixEmbeddingClient(),
    },
)
app.state.embedding_service = RoutingEmbeddingService(
    app.state.embedding_config,
    app.state.embedding_health,
    app.state.embedding_executor,
)
app.state.rag_config = RAGConfigProperties.from_env()
app.state.search_channel_config = SearchChannelProperties.from_env()
app.state.search_channel_config.validate()
app.state.retrieval_engine = None
app.state.agent = None
app.state.task_registry = {}


def _documents_snapshot(application: FastAPI) -> list[dict[str, Any]]:
    snapshot: list[dict[str, Any]] = []
    for item in getattr(application.state, "documents", []):
        snapshot.append({
            "id": item.chunk["id"],
            "content": item.chunk["content"],
            "metadata": {
                "collection": item.collection,
                "filename": item.doc_name,
                "doc_name": item.doc_name,
                "chunk_index": item.chunk["index"],
            },
            "collection": item.collection,
            "filename": item.doc_name,
            "chunk_index": item.chunk["index"],
        })
    return snapshot


def _build_retrieval_engine(application: FastAPI) -> tuple[MultiChannelRetrievalEngine, Any] | None:
    """构造检索引擎。

    g2 分层：检索直接连 Java 端入库写入的 pgvector（t_knowledge_vector），
    与 ragent 原行为一致（同库、同维度 1536、同 embedding 模型）。
    无 pg 依赖时降级为 None（对话仍可用，仅不检索知识库）。
    """
    try:
        from core.retrieval.pgvector import PgVectorBackend

        pg = PgVectorBackend(
            routing=application.state.embedding_service,
            config=application.state.embedding_config,
        )
        application.state.pg_backend = pg
    except Exception as exc:
        _logger.warning("pgvector 检索后端初始化失败，检索降级为空: %s", exc)
        return None

    async def vector_runner(query: str, collections: list[str], top_k: int):
        return await pg.retrieve(query, collections, top_k)

    async def keyword_runner(query: str, collections: list[str], top_k: int):
        return await pg.retrieve_keyword(query, collections, top_k)

    search_config = application.state.search_channel_config
    rag_config = getattr(application.state, "rag_config", None) or RAGConfigProperties.from_env()
    channels = {
        ChannelType.VECTOR: vector_runner,
        ChannelType.KEYWORD: keyword_runner,
    }
    graph_config = LightRagConfig(
        base_url=os.getenv("RAG_GRAPH_BASE_URL", "http://127.0.0.1:9621"),
        api_key=os.getenv("RAG_GRAPH_API_KEY", ""),
        query_mode=os.getenv("RAG_GRAPH_QUERY_MODE", "hybrid"),
        timeout_ms=search_config.channels.timeout_ms,
    )
    web_config = search_config.channels.web_search
    graph_backend = LightRagBackend(graph_config)
    web_backend = YouComWebSearchBackend(WebSearchConfig(
        api_url=web_config.api_url,
        api_key=web_config.api_key,
        count=web_config.count,
        timeout_seconds=web_config.timeout_seconds,
    ))

    async def graph_runner(query: str, collections: list[str], top_k: int):
        return await graph_backend.retrieve(query, collections, top_k)

    async def web_search_runner(query: str, collections: list[str], top_k: int):
        return await web_backend.retrieve(query, collections, top_k)

    channels.update({
        ChannelType.GRAPH: graph_runner,
        ChannelType.WEB_SEARCH: web_search_runner,
    })

    weights = search_config.fusion.channel_weights
    timeout_ms = search_config.channels.timeout_ms
    retrieval_config = RetrievalConfig(
        rrf_k=search_config.fusion.rrf_k,
        channels={
            ChannelType.VECTOR: ChannelConfig(
                enabled=search_config.channels.vector.enabled,
                weight=weights.vector,
            ),
            ChannelType.KEYWORD: ChannelConfig(
                enabled=search_config.channels.keyword.enabled,
                weight=weights.keyword,
            ),
            ChannelType.GRAPH: ChannelConfig(
                enabled=search_config.channels.graph.enabled,
                weight=weights.graph,
            ),
            ChannelType.WEB_SEARCH: ChannelConfig(
                enabled=search_config.channels.web_search.enabled,
                weight=weights.web_search,
            ),
        },
        rerank_enabled=search_config.fusion.rerank_candidate_limit > 0,
        supplement_ratio=search_config.scope.supplement_ratio,
        channel_timeout_ms=timeout_ms,
        gate_enabled=rag_config.gate_enabled,
        gate_bge_threshold=rag_config.gate_bge_threshold,
        gate_qwen3_threshold=rag_config.gate_qwen3_threshold,
        gate_top_n=rag_config.gate_top_n,
        # 窗口组装层消融开关（分层冻结实验法：默认值全部保持 v3 行为）
        head_demote_enabled=os.getenv("RAG_HEAD_DEMOTE", "false").strip().lower() in ("1", "true", "yes"),
        diversity_enabled=os.getenv("RAG_DIVERSITY_ENABLED", "true").strip().lower() in ("1", "true", "yes"),
        diversity_max_per_doc=int(os.getenv("RAG_DIVERSITY_MAX_PER_DOC", "0").strip() or "0"),
        diversity_min_score=float(os.getenv("RAG_DIVERSITY_MIN_SCORE", "0").strip() or "0"),
    )
    reranker = RoutingReranker([
        SiliconFlowReranker(),
        LLMReranker(_build_llm_service()),
    ])
    relevance_gate = get_default_gate() if rag_config.gate_enabled else None
    return (
        MultiChannelRetrievalEngine(
            channels, retrieval_config, reranker=reranker, relevance_gate=relevance_gate
        ),
        pg,
    )


def _build_intent_resolver(llm: Any) -> tuple[Any, IntentTreeCacheManager, Any | None]:
    """构造意图树 Resolver：可选 Redis 缓存，失败回退内存。

    返回 (resolver, cache_manager, redis_client)。Redis client 用于 graceful 关闭。
    """

    settings = _settings
    cache_manager = IntentTreeCacheManager()
    redis_client: Any | None = None
    if settings.redis_url:
        try:
            import redis  # type: ignore

            redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
            redis_client.ping()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("intent redis init failed, fallback to in-memory: %s", exc)
            redis_client = None
    cache_manager.bind(redis_client)
    loader = InMemoryIntentLoader() if redis_client is None else RedisBackedIntentLoader(cache_manager)
    service = InMemoryIntentTreeService(loader=loader, cache_manager=cache_manager)
    tree = service.get_full_tree()
    resolver = IntentResolver(llm=llm, tree=tree)
    return resolver, cache_manager, redis_client


def _build_llm_service() -> AsyncRoutingLLMService:
    properties = AIModelProperties.default_bailian()
    health = LLMHealthStore()
    selector = ModelSelector(properties, health)
    clients: dict[str, Any] = {}
    if properties.chat and properties.chat.candidates:
        model = properties.chat.candidates[0].model
    else:
        model = None
    for provider_name, provider in properties.providers.items():
        if provider_name == "bailian":
            clients[provider_name] = BaiLianLLMService(
                api_key=provider.api_key or None,
                model=model,
            )
        elif provider_name == "siliconflow":
            clients[provider_name] = SiliconFlowLLMService(
                api_key=provider.api_key or None,
                model=properties.chat.candidates[0].model if properties.chat and properties.chat.candidates else None,
            )
        elif provider_name == "aihubmix":
            clients[provider_name] = AIHubMixLLMService(
                api_key=provider.api_key or None,
                model=properties.chat.candidates[0].model if properties.chat and properties.chat.candidates else None,
            )
        elif provider_name == "ollama":
            clients[provider_name] = OllamaLLMService(
                base_url=provider.base_url or None,
                model=properties.chat.candidates[0].model if properties.chat and properties.chat.candidates else None,
            )
    if "bailian" not in clients:
        clients["bailian"] = BaiLianLLMService(
            api_key=None,
            model=model,
        )
    executor = ModelRoutingExecutor(health)
    return AsyncRoutingLLMService(RoutingLLMService(selector, executor, clients))


def _documents_path() -> str:
    path = str(_settings.documents_path)
    return os.path.abspath(path)


def _load_documents() -> list[dict[str, Any]]:
    try:
        with open(_documents_path(), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, list):
            return []
        return [
            StoredChunk(item["collection"], item["doc_name"], item["chunk"])
            for item in payload
            if isinstance(item, dict)
            and "collection" in item
            and "doc_name" in item
            and "chunk" in item
        ]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _save_documents(documents: list[dict[str, Any]]) -> None:
    target = _documents_path()
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="documents-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(documents, handle, ensure_ascii=False)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _error_payload(request: Request, code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {
        "success": False,
        "code": code,
        "message": message,
        "request_id": getattr(request.state, "request_id", None),
        "details": details,
    }


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(request, f"HTTP_{exc.status_code}", detail.get("message", "request failed"), detail),
        headers=exc.headers,
    )


@app.exception_handler(ClientException)
async def client_exception_handler(request: Request, exc: ClientException) -> JSONResponse:
    # 业务校验异常（agent_profile/sample_question/query_term_mapping 等 16 处 raise）统一 400 + 具体原因，
    # 未注册时会掉进下方 500 兜底，前端只能看到 internal server error
    return JSONResponse(
        status_code=400,
        content=_error_payload(request, "CLIENT_ERROR", str(exc)),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_error_payload(request, "VALIDATION_ERROR", "request validation failed", exc.errors()),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    _logger.exception("unhandled request error request_id=%s", getattr(request.state, "request_id", None))
    return JSONResponse(
        status_code=500,
        content=_error_payload(request, "INTERNAL_ERROR", "internal server error"),
    )


def ensure_runtime_state(application: FastAPI) -> None:
    if not hasattr(application.state, "settings"):
        application.state.settings = AppSettings()
    if not hasattr(application.state, "security"):
        application.state.security = SecurityService(application.state.settings)
    if not hasattr(application.state, "documents"):
        application.state.documents = _load_documents()
    if not hasattr(application.state, "embedding_config"):
        application.state.embedding_config = EmbeddingConfig()
    if not hasattr(application.state, "embedding_health"):
        application.state.embedding_health = EmbeddingHealthStore()
    if not hasattr(application.state, "embedding_executor"):
        application.state.embedding_executor = EmbeddingRoutingExecutor(
            application.state.embedding_config,
            application.state.embedding_health,
            {
                "ollama": OllamaEmbeddingClient(),
                "siliconflow": SiliconFlowEmbeddingClient(),
                "aihubmix": AIHubMixEmbeddingClient(),
            },
        )
    if not hasattr(application.state, "embedding_service"):
        application.state.embedding_service = RoutingEmbeddingService(
            application.state.embedding_config,
            application.state.embedding_health,
            application.state.embedding_executor,
        )
    if not hasattr(application.state, "rag_config"):
        application.state.rag_config = RAGConfigProperties.from_env()
    if not hasattr(application.state, "search_channel_config"):
        application.state.search_channel_config = SearchChannelProperties.from_env()
        application.state.search_channel_config.validate()
    if not hasattr(application.state, "retrieval_engine"):
        application.state.retrieval_engine = None
    for attr in ("intent_resolver", "intent_cache_manager", "intent_redis"):
        if not hasattr(application.state, attr):
            setattr(application.state, attr, None)
    if not hasattr(application.state, "agent") or application.state.agent is None:
        application.state.agent = Agent(
            _build_llm_service(),
            rag_config=application.state.rag_config,
            search_channel_config=application.state.search_channel_config,
        )


def refresh_runtime_components(application: FastAPI) -> None:
    ensure_runtime_state(application)
    retrieval_bundle = _build_retrieval_engine(application)
    retrieval_engine = retrieval_bundle[0] if retrieval_bundle else None
    doc_meta_provider = retrieval_bundle[1] if retrieval_bundle else None
    application.state.retrieval_engine = retrieval_engine
    application.state.doc_meta_provider = doc_meta_provider
    if getattr(application.state, "intent_resolver", None) is None:
        llm_service = _build_llm_service()
        resolver, cache_manager, redis_client = _build_intent_resolver(llm_service)
        application.state.intent_resolver = resolver
        application.state.intent_cache_manager = cache_manager
        application.state.intent_redis = redis_client
    else:
        llm_service = _build_llm_service()
    if retrieval_engine is None:
        application.state.agent = Agent(
            llm_service,
            intent_resolver=application.state.intent_resolver,
            doc_meta_provider=doc_meta_provider,
            rag_config=application.state.rag_config,
            search_channel_config=application.state.search_channel_config,
        )
    else:
        from core.rewrite import QueryRewriter

        # 文档域感知 rewrite：口语句对齐知识库术语后才能过 RelevanceGate（gate 双低切空）
        pg = getattr(application.state, "pg_backend", None)
        rewriter = QueryRewriter(llm_service, doc_domains=pg.list_doc_names() if pg else None)
        application.state.agent = Agent(
            llm_service,
            retriever=retrieval_engine,
            rewriter=rewriter,
            intent_resolver=application.state.intent_resolver,
            doc_meta_provider=doc_meta_provider,
            rag_config=application.state.rag_config,
            search_channel_config=application.state.search_channel_config,
        )


app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origins,
    allow_credentials=_settings.cors_origins != ["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-Id"],
)


# 1:1 移植 ragent DemoModeInterceptor：体验环境只读模式（在 request_context 之前短路）
_demo_mode_guard = DemoModeMiddleware(None, DemoModeProperties())


@app.middleware("http")
async def demo_mode_guard(request: Request, call_next):
    return await _demo_mode_guard.dispatch(request, call_next)


# 1:1 移植 ragent 管理后台路由（示例问题 / 关键词映射 / 评测 / 知识库管理）
app.include_router(build_sample_question_router(), prefix="/rag/sample-questions", tags=["admin"])
app.include_router(build_sample_question_router(), prefix="/sample-questions", tags=["admin"])
app.include_router(build_query_term_mapping_router(), prefix="/mappings", tags=["admin"])
app.include_router(build_eval_router(), tags=["admin"])
# 1:1 翻译 ragent KnowledgeBaseController / KnowledgeDocumentController / KnowledgeChunkController
app.include_router(build_knowledge_base_router(), prefix="/rag/knowledge-bases", tags=["admin"])
app.include_router(build_knowledge_base_router(), prefix="/knowledge-bases", tags=["admin"])
app.include_router(build_knowledge_document_router(), prefix="/rag/knowledge-documents", tags=["admin"])
app.include_router(build_knowledge_document_router(), prefix="/knowledge-documents", tags=["admin"])
app.include_router(build_knowledge_chunk_router(), prefix="/rag/knowledge-chunks", tags=["admin"])
app.include_router(build_knowledge_chunk_router(), prefix="/knowledge-chunks", tags=["admin"])


@app.middleware("http")
async def request_context(request: Request, call_next):
    ensure_runtime_state(request.app)
    settings: AppSettings = getattr(request.app.state, "settings", _settings)
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
    request.state.request_id = request_id

    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    bucket = _rate_buckets[client_ip]
    while bucket and now - bucket[0] > 60:
        bucket.popleft()
    if len(bucket) >= settings.rate_limit_per_minute:
        return JSONResponse(
            status_code=429,
            content=_error_payload(request, "RATE_LIMITED", "Too Many Requests", {"request_id": request_id}),
            headers={"X-Request-Id": request_id},
        )
    bucket.append(now)

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > settings.max_upload_bytes:
        return JSONResponse(
            status_code=413,
            content=_error_payload(request, "PAYLOAD_TOO_LARGE", "Payload Too Large", {"max_bytes": settings.max_upload_bytes, "request_id": request_id}),
            headers={"X-Request-Id": request_id},
        )

    span = _tracer.start_span(request.url.path)
    span.set_attribute("request_id", request_id)
    span.set_attribute("path", request.url.path)
    span.set_attribute("method", request.method)
    try:
        response = await call_next(request)
    except Exception as exc:
        span.record_exception(exc)
        _logger.exception("request failed request_id=%s path=%s", request_id, request.url.path)
        response = JSONResponse(
            status_code=500,
            content=_error_payload(request, "INTERNAL_ERROR", "Internal Server Error", {"request_id": request_id}),
        )
    finally:
        span.end()
    response.headers["X-Request-Id"] = request_id
    return response


@app.get("/health")
async def health(settings: AppSettings = Depends(get_settings)):
    """活跃探针：进程在线就返回 UP，不做下游依赖探测。"""
    return {
        "status": "UP",
        "app": settings.app_name,
        "version": settings.app_version,
    }


@app.post("/retrieval/rerank-gate")
async def rerank_gate(payload: dict[str, Any]):
    """双模型 rerank 相关性门控打分（P1 负样本治理）。

    供 Java 网关（RerankPostProcessor 之后的 RelevanceGatePostProcessor）内部调用；
    进程内链路（Python agent 检索引擎）经 engine.py 直接使用 DualRerankGate。

    请求: {"question": str, "texts": [str], "bge_threshold"?: float,
           "qwen3_threshold"?: float, "top_n"?: int}
    响应: GateResult.to_dict()；模型缺失/推理失败时 triggered=false + error（fail-open）。
    """
    question = str(payload.get("question") or "").strip()
    texts = [str(t) for t in (payload.get("texts") or []) if t]
    if not question or not texts:
        return GateResult(triggered=False, error="question/texts required").to_dict()
    gate = get_default_gate()
    kwargs: dict[str, Any] = {}
    if payload.get("bge_threshold"):
        kwargs["bge_threshold"] = float(payload["bge_threshold"])
    if payload.get("qwen3_threshold"):
        kwargs["qwen3_threshold"] = float(payload["qwen3_threshold"])
    if payload.get("top_n"):
        kwargs["top_n"] = int(payload["top_n"])
    result = await asyncio.to_thread(gate.gate, question, texts, **kwargs)
    return result.to_dict()


def _probe_redis(client: Any) -> dict[str, Any]:
    if client is None:
        return {"status": "DOWN", "detail": "not configured"}
    try:
        client.ping()
        return {"status": "UP"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "DOWN", "detail": str(exc)[:200]}


def _probe_llm(agent: Agent) -> dict[str, Any]:
    if agent is None or getattr(agent, "llm", None) is None:
        return {"status": "DOWN", "detail": "llm not initialised"}
    return {"status": "UP", "model": getattr(agent.llm, "model", None)}


def _probe_retrieval(engine: Any) -> dict[str, Any]:
    if engine is None:
        return {"status": "DOWN", "detail": "retrieval engine not initialised"}
    return {"status": "UP"}


def _probe_embedding(embedding_config: Any) -> dict[str, Any]:
    if embedding_config is None:
        return {"status": "DOWN", "detail": "embedding config missing"}
    provider = getattr(embedding_config, "provider", "unknown")
    return {"status": "UP", "provider": provider}


@app.get("/ready")
async def ready(request: Request):
    """就绪探针：聚合各子系统状态，任一关键子系统 DOWN 时整体 503。"""
    state = request.app.state
    components = {
        "llm": _probe_llm(getattr(state, "agent", None)),
        "retrieval": _probe_retrieval(getattr(state, "retrieval_engine", None)),
        "embedding": _probe_embedding(getattr(state, "embedding_config", None)),
        "intent_redis": _probe_redis(getattr(state, "intent_redis", None)),
    }
    critical = ("llm", "retrieval", "embedding")
    overall = "UP" if all(components[k]["status"] == "UP" for k in critical) else "DOWN"
    body: dict[str, Any] = {
        "status": overall,
        "app": state.settings.app_name if hasattr(state, "settings") else "agent",
        "components": components,
    }
    if overall != "UP":
        return JSONResponse(status_code=503, content=body)
    return body


@app.get("/me")
async def me(user=Depends(get_current_user)):
    return user.model_dump()


# ---------------------------------------------------------------------------
# 案件调查（Phase 1）：/case/investigate SSE + 案件查询。纯增量，不影响 /chat。
# ---------------------------------------------------------------------------
_CASE_SENSITIVE_ROLES = ("ADMIN", "RISK")  # 审批/归档敏感操作的允许角色关键字


def _case_operator(user) -> str:
    """提取操作人标识（P3-2）：sub -> username -> id 兜底链。"""
    op = (
        getattr(user, "sub", None)
        or getattr(user, "username", None)
        or getattr(user, "id", None)
    )
    return str(op or "").strip()


def _require_case_operator(user) -> str:
    """裁决/归档前置校验（P3-2）：操作人必须可追溯；登录态携带角色时须命中 ADMIN/RISK。"""
    op = _case_operator(user)
    if not op:
        raise HTTPException(status_code=403, detail="无法确定操作人身份，拒绝敏感操作")
    roles = getattr(user, "roles", None) or []
    if roles and not any(
        any(k in str(r).upper() for k in _CASE_SENSITIVE_ROLES) for r in roles
    ):
        raise HTTPException(status_code=403, detail=f"角色 {roles} 无审批权限（需 ADMIN/RISK）")
    return op


def _get_case_agent(request: Request) -> CaseInvestigationAgent:
    """懒加载案件调查 Agent：复用主 Agent 的 llm/mcp 实例（共享路由与 MCP 降级底座）。"""
    case_agent = getattr(request.app.state, "case_agent", None)
    if case_agent is None:
        main_agent: Agent = request.app.state.agent
        # Phase 4: 注入 Responder（L2 自动处置的受控执行器；未注入时 L2 自动禁用）
        case_agent = CaseInvestigationAgent(main_agent.llm, main_agent.mcp, responder=_get_case_responder(request))
        request.app.state.case_agent = case_agent
    return case_agent


def _get_case_store(request: Request) -> CaseStore:
    """案件存储（复用调查 Agent 的 store 实例，保证内存降级模式一致性）。"""
    return _get_case_agent(request).store


def _get_rule_miner(request: Request) -> RuleMiner:
    """懒加载 RuleMiner（Phase 4）：复用调查 Agent 的 llm/GitOps 槽位。"""
    miner = getattr(request.app.state, "case_rule_miner", None)
    if miner is None:
        miner = RuleMiner(_get_case_agent(request), _get_case_store(request))
        request.app.state.case_rule_miner = miner
    return miner


def _get_case_patroller(request: Request) -> CasePatroller:
    """懒加载巡检器（Phase 4）：优先复用 lifespan 装配的调度实例。"""
    patroller = getattr(request.app.state, "case_patroller", None)
    if patroller is None:
        patroller = CasePatroller(_get_case_agent(request), _get_case_store(request))
        request.app.state.case_patroller = patroller
    return patroller


def _get_case_responder(request: Request) -> CaseResponder:
    """懒加载 Responder 执行器（P3）：与调查 Agent 共享 mcp 实例。"""
    responder = getattr(request.app.state, "case_responder", None)
    if responder is None:
        main_agent: Agent = request.app.state.agent
        responder = CaseResponder(main_agent.mcp)
        request.app.state.case_responder = responder
    return responder


# ---------------- Phase 4: Rule Miner 规则工厂（蒸馏 -> 回放验证 -> HITL 审核 -> 回注分诊） ----------------


class RuleMineRequest(BaseModel):
    limit: int = Field(default=10, ge=1, le=50, description="消费蒸馏池记录数上限")


class RuleReviewRequest(BaseModel):
    decision: str = Field(pattern="^(APPROVED|REJECTED)$", description="审核结论")
    opinion: str = Field(default="", max_length=512, description="审核意见")


class PatrolRunRequest(BaseModel):
    clues: list[str] | None = Field(default=None, max_length=10, description="手动线索（测试/演示用）")


@app.get("/case/rules/candidates")
async def case_rule_candidates(request: Request, status: str = "", limit: int = 100, user=Depends(get_current_user)):
    """候选规则列表（PENDING/APPROVED/REJECTED，管理端审核视角）。"""
    store = _get_case_store(request)
    items = store.list_rule_candidates(status=status.strip().upper(), limit=max(1, min(limit, 200)))
    return {"success": True, "items": items, "total": len(items)}


@app.post("/case/rules/candidates/mine")
async def case_rule_mine(payload: RuleMineRequest, request: Request, user=Depends(get_current_user)):
    """手动触发 Rule Miner：消费蒸馏池 -> LLM 蒸馏 -> 候选规则落库（PENDING）。

    护栏：condition_hash 去重、高危动作拦截、批次审计（RULE_MINED）。
    """
    operator = str(getattr(user, "username", None) or "rule_miner")
    result = await _get_rule_miner(request).mine(limit=payload.limit, operator=operator)
    return {"success": True, **result}


@app.post("/case/rules/candidates/{rule_id}/review")
async def case_rule_review(rule_id: str, payload: RuleReviewRequest, request: Request, user=Depends(get_current_user)):
    """候选规则 HITL 审核（first_time 幂等）：APPROVED 后规则回注分诊（__RULE_HINTS__）。"""
    store = _get_case_store(request)
    operator = str(getattr(user, "username", None) or "unknown")
    item = store.review_rule_candidate(
        rule_id, status=payload.decision, operator=operator, opinion=payload.opinion
    )
    if item is None:
        raise HTTPException(status_code=404, detail="规则候选不存在")
    first_time = bool(item.pop("first_time", False))
    if first_time:
        operation = AUDIT_OPERATION_RULE_APPROVED if payload.decision == "APPROVED" else AUDIT_OPERATION_RULE_REJECTED
        store.record_audit(
            case_id="",
            operation=operation,
            target=rule_id,
            operator=operator,
            detail=f"规则[{item.get('rule_name', '')}]审核 {payload.decision}: {payload.opinion}"[:512],
        )
    return {"success": True, "first_time": first_time, "rule": item}


@app.post("/case/rules/candidates/{rule_id}/validate")
async def case_rule_validate(rule_id: str, request: Request, sample: int = 50, user=Depends(get_current_user)):
    """历史案件回放验证（检测工程 Agent「合成遥测验证」的 Sentinel 对应物）。"""
    result = await _get_rule_miner(request).validate(rule_id, sample_size=sample)
    return {"success": True, **result}


@app.post("/case/patrol/run")
async def case_patrol_run(payload: PatrolRunRequest, request: Request, user=Depends(get_current_user)):
    """手动触发一轮巡检（调度器之外的管理入口；指纹幂等 + 单批上限护栏与调度模式一致）。"""
    result = await _get_case_patroller(request).patrol_once(clues_override=payload.clues)
    return {"success": bool(result.get("started")), **result}


@app.post("/case/investigate")
async def case_investigate(payload: CaseInvestigateRequest, request: Request, user=Depends(get_current_user)):
    """线索 -> 分诊 -> 受控多步调查 -> 结构化报告（SSE 逐步推送证据链）。"""
    case_agent = _get_case_agent(request)
    user_id = getattr(user, "username", None) or getattr(user, "id", None) or "anonymous"

    async def events() -> AsyncIterator[str]:
        try:
            async for event in case_agent.investigate(
                payload.clue, user_id=str(user_id), source=payload.source, alert_id=payload.alert_id
            ):
                if await request.is_disconnected():
                    raise asyncio.CancelledError()
                yield f"event: message\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield "event: end\ndata: {\"status\": \"done\"}\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger.exception("case investigate failed")
            yield f"event: error\ndata: {json.dumps({'success': False, 'code': 'CASE_ERROR', 'message': str(exc)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class ApprovalDecideRequest(BaseModel):
    """审批裁决请求。"""

    decision: str = Field(pattern="^(APPROVED|REJECTED)$")
    operator: str = Field(default="", max_length=64)
    opinion: str = Field(default="", max_length=1024)


class RiskPolicyRequest(BaseModel):
    """P2 风险策略更新请求（L2 自动处置 + 双人复核分级共用）。"""

    l2_autopilot_enabled: bool
    l2_confidence: float = Field(ge=0, le=1)
    l2_action_whitelist: list[str] = Field(min_length=1)
    l2_action_denylist: list[str] = Field(min_length=1)


@app.get("/case/risk-policy")
async def get_case_risk_policy(request: Request, user=Depends(get_current_user)):
    """读取 L2 风险策略（登录即可读，供前端策略卡片展示）。"""
    _get_case_agent(request)
    operator = _case_operator(user)
    if not operator:
        raise HTTPException(status_code=403, detail="无法确定操作人身份")
    return get_risk_policy()


@app.put("/case/risk-policy")
async def put_case_risk_policy(payload: RiskPolicyRequest, request: Request, user=Depends(get_current_user)):
    """更新 L2 风险策略（P2 配置化）：仅管理员；白/黑名单非空校验（fail-closed）。"""
    _get_case_agent(request)
    operator = _case_operator(user)
    if not operator:
        raise HTTPException(status_code=403, detail="无法确定操作人身份，拒绝敏感操作")
    roles = getattr(user, "roles", None) or []
    if roles and str(roles[0]).lower() != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可修改风险策略")
    whitelist = [w.strip() for w in payload.l2_action_whitelist if w.strip()]
    denylist = [w.strip() for w in payload.l2_action_denylist if w.strip()]
    if not whitelist or not denylist:
        raise HTTPException(status_code=400, detail="白/黑名单不能为空")
    return save_risk_policy(
        enabled=payload.l2_autopilot_enabled,
        confidence=payload.l2_confidence,
        whitelist=whitelist,
        denylist=denylist,
        operator=operator,
    )


@app.get("/case/approvals")
async def approval_list(
    request: Request,
    user=Depends(get_current_user),
    case_id: str = "",
    status: str = "",
    limit: int = 100,
):
    """审批待办列表（管理端视角；case_id/status 可选过滤）。

    注意：必须定义在 GET /case/{case_id} 之前，否则路径参数会把
    /case/approvals 吞掉（FastAPI 按注册顺序匹配）。
    """
    case_agent = _get_case_agent(request)
    return {
        "approvals": case_agent.store.list_approvals(
            case_id=case_id, status=status, limit=min(max(limit, 1), 200)
        ),
        "stats": case_agent.store.approval_stats(case_id=case_id),
    }


@app.post("/case/approvals/{approval_id}/decide")
async def approval_decide(
    approval_id: str, payload: ApprovalDecideRequest, request: Request, user=Depends(get_current_user)
):
    """审批裁决：APPROVED / REJECTED。

    - 重复裁决幂等返回当前状态（不重复落处置/审计/通知）
    - 高危动作（命中 L2_ACTION_DENYLIST，如冻结）权限分层复核（C10 R4/R5）：
      第一次 APPROVED 仅记一级确认（pending_second=True，状态保持 PENDING，任意角色），
      由管理员（role=admin）终审 APPROVED 才触发执行；REJECTED 一票即否决
    - 终态裁决（P3-1/P3-2/P3-4）：写审计日志；APPROVED 自动落处置动作记录；
      执行成功（EXECUTED）推进案件 RESOLVED；推送 IM 裁决结果
    """
    case_agent = _get_case_agent(request)
    operator = _case_operator(user)
    if not operator:
        raise HTTPException(status_code=403, detail="无法确定操作人身份，拒绝敏感操作")
    roles = getattr(user, "roles", None) or []
    operator_role = str(roles[0]) if roles else ""
    try:
        result = case_agent.store.decide_approval(
            approval_id,
            status=payload.decision,
            operator=operator,
            opinion=payload.opinion,
            operator_role=operator_role,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result is None:
        raise HTTPException(status_code=404, detail="approval not found")
    first_time = bool(result.pop("first_time", False))
    pending_second = bool(result.pop("pending_second", False))
    if first_time:
        operation = (
            AUDIT_OPERATION_APPROVE if payload.decision == "APPROVED" else AUDIT_OPERATION_REJECT
        )
        case_agent.store.record_audit(
            case_id=result.get("case_id", ""),
            approval_id=approval_id,
            operation=operation,
            target=approval_id,
            operator=operator,
            detail=f"{payload.decision} {result.get('action', '')}｜意见: {payload.opinion[:100]}",
        )
        if payload.decision == "APPROVED":
            # P3 Responder：审批通过后受控执行（online 真调用 / offline 留痕）
            case_rec = case_agent.store.get_case(result.get("case_id", "")) or {}
            item = {
                "action": result.get("action", ""),
                "reason": result.get("reason", ""),
                "approval_id": approval_id,
                "operator": operator,
                "subject_id": str(case_rec.get("subject_id", "")),
            }
            outcome = await _get_case_responder(request).execute(item, result)
            case_agent.store.create_actions(
                result.get("case_id", ""),
                result.get("case_no", ""),
                approval_id,
                [
                    {
                        "action": item["action"],
                        "detail": f"{item['reason']}｜{outcome['detail']}",
                        "status": outcome.get("status", "EXECUTED"),
                    }
                ],
                operator=operator,
            )
            case_agent.store.record_audit(
                case_id=result.get("case_id", ""),
                approval_id=approval_id,
                operation=AUDIT_OPERATION_ACTION_EXECUTE,
                target=approval_id,
                operator=operator,
                detail=f"{outcome.get('mode', '')}｜{outcome.get('status', '')}｜{outcome.get('detail', '')[:300]}",
            )
            if outcome.get("status") == "EXECUTED":
                case_agent.store.mark_case_resolved(result.get("case_id", ""))
        notify_approval_decided(result, payload.decision, operator, payload.opinion)
    elif pending_second:
        case_agent.store.record_audit(
            case_id=result.get("case_id", ""),
            approval_id=approval_id,
            operation=AUDIT_OPERATION_APPROVE,
            target=approval_id,
            operator=operator,
            detail=f"一级通过（待二级复核）{result.get('action', '')}｜意见: {payload.opinion[:100]}",
        )
    return result


@app.get("/case/kb/distillations")
async def kb_distillation_list(
    request: Request,
    user=Depends(get_current_user),
    conclusion: str = "",
    limit: int = 100,
):
    """知识库飞轮候选池列表（P3）：案件报告经验沉淀，供 Rule Miner/知识入库消费。

    注意：必须定义在 GET /case/{case_id} 之前（FastAPI 按注册顺序匹配）。
    """
    case_agent = _get_case_agent(request)
    return {
        "distillations": case_agent.store.list_kb_distillations(
            conclusion=conclusion, limit=min(max(limit, 1), 200)
        )
    }


@app.get("/case/{case_id}")
async def case_detail(case_id: str, request: Request, user=Depends(get_current_user)):
    """查询单个案件（含证据链 + 审批待办 + 处置记录 + 审计日志）。"""
    case_agent = _get_case_agent(request)
    case = case_agent.store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    case["approvals"] = case_agent.store.list_approvals(case_id=case_id)
    case["actions"] = case_agent.store.list_actions(case_id)
    case["audits"] = case_agent.store.list_audit(case_id)
    return case


@app.post("/case/{case_id}/close")
async def case_close(case_id: str, request: Request, user=Depends(get_current_user)):
    """归档关闭案件（P3-3）：REPORTED/INVESTIGATING -> CLOSED，重复关闭幂等。

    归档成功时触发知识库飞轮：报告 knowledge_distillation 回写候选池（幂等）。
    """
    case_agent = _get_case_agent(request)
    operator = _require_case_operator(user)
    result = case_agent.store.close_case(case_id, operator=operator)
    if result is None:
        raise HTTPException(status_code=404, detail="case not found")
    if result.get("closed"):
        result["kb_distillation"] = case_agent.store.distill_knowledge(case_id, operator=operator)
    return result


@app.get("/case/{case_id}/audit")
async def case_audit(case_id: str, request: Request, user=Depends(get_current_user), limit: int = 100):
    """案件审计日志（P3-2）：裁决/归档全量留痕查询。"""
    case_agent = _get_case_agent(request)
    if case_agent.store.get_case(case_id) is None:
        raise HTTPException(status_code=404, detail="case not found")
    return {"audits": case_agent.store.list_audit(case_id, limit=min(max(limit, 1), 200))}


def _jsonish(value) -> dict:
    """report/triage 字段兼容：DB 里可能是 JSON 字符串或已是 dict。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _render_case_report_md(case: dict, store) -> str:
    """把案件完整数据渲染为 Markdown 调查报告（P3-3 导出）。"""
    triage = _jsonish(case.get("triage"))
    report = _jsonish(case.get("report"))
    lines: list[str] = [
        f"# 案件调查报告 · {case.get('case_no', case.get('id', ''))}",
        "",
        f"- **标题**: {case.get('title', '')}",
        f"- **状态**: {case.get('status', '')}｜**优先级**: {case.get('priority', '')}"
        f"｜**置信度**: {case.get('confidence', '')}",
        f"- **调查对象**: {case.get('subject_type', '')} `{case.get('subject_id', '')}`",
        f"- **创建时间**: {case.get('create_time', '')}"
        f"｜**TTA**: {case.get('tta_ms', '-')}ms｜**TTR**: {case.get('ttr_ms', '-')}ms",
        "",
        "## 1. 线索摘要",
        "",
        str(case.get("clue_summary") or case.get("raw_input") or "-"),
        "",
        "## 2. 分诊结论",
        "",
        "```json",
        json.dumps(triage, ensure_ascii=False, indent=2),
        "```",
        "",
        "## 3. 调查结论",
        "",
    ]
    if report:
        for key in ("summary", "conclusion", "risk_level", "confidence"):
            if report.get(key) is not None:
                lines.append(f"- **{key}**: {report.get(key)}")
        findings = report.get("key_findings") or []
        if findings:
            lines += ["", "### 关键发现", ""]
            lines += [f"{i}. {f}" for i, f in enumerate(findings, 1)]
        recs = report.get("recommendations") or []
        if recs:
            lines += ["", "### 处置建议", ""]
            for r in recs:
                mark = "需人工审批" if isinstance(r, dict) and r.get("requires_human_approval") else "可自动执行"
                action = r.get("action", "") if isinstance(r, dict) else str(r)
                lines.append(f"- [{mark}] {action}")
        for key, val in report.items():
            if key not in ("summary", "conclusion", "risk_level", "confidence",
                           "key_findings", "recommendations", "raw") and val:
                lines += ["", f"### {key}", "", "```json", json.dumps(val, ensure_ascii=False, indent=2), "```"]
    else:
        lines.append("（无结构化报告）")
    lines += ["", "## 4. 证据链", "", "| # | 阶段 | 工具 | 思考 | 结果摘要 | 耗时(ms) |", "|---|------|------|------|----------|----------|"]
    for ev in case.get("evidence", []):
        obs = str(ev.get("observation", "")).replace("\n", " ")[:120]
        thought = str(ev.get("thought", "")).replace("\n", " ").replace("|", "\\|")[:80]
        lines.append(
            f"| {ev.get('step_no', '')} | {ev.get('phase', '')} | {ev.get('tool_id', '')} "
            f"| {thought} | {obs.replace('|', '\\|')} | {ev.get('elapsed_ms', '')} |"
        )
    lines += ["", "## 5. 审批与处置", "", "| 待办 | 动作 | 审批状态 | 操作人 | 意见 |", "|------|------|----------|--------|------|"]
    for ap in case.get("approvals", []):
        lines.append(
            f"| {ap.get('id', '')[:8]} | {ap.get('action', '')} | {ap.get('status', '')} "
            f"| {ap.get('operator', '') or '-'} | {str(ap.get('opinion', '') or '-').replace('|', '\\|')[:60]} |"
        )
    lines += ["", "| 处置动作 | 详情 | 状态 | 操作人 | 时间 |", "|----------|------|------|--------|------|"]
    for ac in case.get("actions", []):
        lines.append(
            f"| {ac.get('action', '')} | {str(ac.get('detail', '')).replace('|', '\\|')[:80]} "
            f"| {ac.get('status', '')} | {ac.get('operator', '')} | {ac.get('create_time', '')} |"
        )
    lines += ["", "## 6. 审计日志", "", "| 操作 | 对象 | 操作人 | 详情 | 时间 |", "|------|------|--------|------|------|"]
    for au in case.get("audits", []):
        lines.append(
            f"| {au.get('operation', '')} | {au.get('target', '')} | {au.get('operator', '')} "
            f"| {str(au.get('detail', '')).replace('|', '\\|')[:80]} | {au.get('create_time', '')} |"
        )
    lines += ["", "---", f"*导出时间: {datetime.now().isoformat(timespec='seconds')}*"]
    return "\n".join(lines)


@app.get("/case/{case_id}/report/export")
async def case_report_export(case_id: str, request: Request, user=Depends(get_current_user)):
    """调查报告 Markdown 导出（P3-3）：下载附件。"""
    case_agent = _get_case_agent(request)
    case = case_agent.store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    case["approvals"] = case_agent.store.list_approvals(case_id=case_id)
    case["actions"] = case_agent.store.list_actions(case_id)
    case["audits"] = case_agent.store.list_audit(case_id)
    md = _render_case_report_md(case, case_agent.store)
    case_no = str(case.get("case_no") or case_id)
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{case_no}-report.md"'},
    )


@app.get("/cases")
async def case_list(request: Request, user=Depends(get_current_user), limit: int = 20):
    """案件列表（最近优先）。"""
    case_agent = _get_case_agent(request)
    return {"cases": case_agent.store.list_cases(limit=min(max(limit, 1), 100))}


@app.post("/chat")
async def chat(payload: ChatRequest, request: Request, user=Depends(get_current_user)):
    """1:1 翻译 ragent RAGChatServiceImpl.streamChat：集成 ChatQueueLimiter。

    队列限流逻辑：
      - 抢到许可 -> 正常执行聊天流程
      - 等待超时 -> 返回拒绝消息（系统繁忙）
    """
    agent: Agent = request.app.state.agent
    queue_limiter: ChatQueueLimiter = getattr(request.app.state, "queue_limiter", None)
    request_id = getattr(request.state, "request_id", None)
    task_id = payload.taskId or payload.conversationId or request_id or str(id(asyncio.current_task()))
    user_id = getattr(user, "username", None) or getattr(user, "id", None) or "anonymous"

    # 1:1 ragent RAGChatServiceImpl#streamChat: generate snowflake id when blank,
    # so first-turn messages persist and clients can read the id from meta frame
    if not payload.conversationId:
        payload.conversationId = next_message_id()

    # 1:1 翻译 ragent ChatQueueLimiter.enqueue：队列限流入口
    # ticket_holder must exist unconditionally: events().finally references it
    # even when the limiter is disabled
    ticket_holder: list = []
    rate_limit_props = getattr(request.app.state, "rate_limit_properties", None)
    if queue_limiter is not None and (rate_limit_props is None or rate_limit_props.global_enabled):
        loop = asyncio.get_event_loop()
        acquired = asyncio.Event()
        rejected = asyncio.Event()

        def _on_acquire(ticket):
            ticket_holder.append(ticket)
            acquired.set()

        def _on_reject():
            rejected.set()

        await loop.run_in_executor(
            None,
            lambda: queue_limiter.enqueue_or_reject(
                request_id=task_id,
                # 1:1 ragent ChatQueueLimiter: TimeUnit.SECONDS.toMillis(getGlobalMaxWaitSeconds())
                max_wait_ms=rate_limit_props.global_max_wait_seconds * 1000 if rate_limit_props else 30000,
                on_acquired=_on_acquire,
                on_rejected=_on_reject,
            ),
        )

        # 等待限流结果（Python 3.11+ asyncio.wait 禁止传裸 coroutine，必须包 Task）
        wait_tasks = [
            asyncio.ensure_future(acquired.wait()),
            asyncio.ensure_future(rejected.wait()),
        ]
        done, pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()

        if rejected.is_set():
            # 1:1 翻译 ragent handleReject：返回独立的拒绝消息 SSE 流（chat 本身保持普通函数）
            async def reject_events() -> AsyncIterator[str]:
                reject_msg = "系统繁忙，请稍后再试"
                yield f"event: reject\ndata: {json.dumps({'type': 'response', 'delta': reject_msg}, ensure_ascii=False)}\n\n"
                yield f"event: end\ndata: {json.dumps({'status': 'rejected', 'request_id': request_id}, ensure_ascii=False)}\n\n"
            return StreamingResponse(reject_events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def events() -> AsyncIterator[str]:
        cancelled = threading.Event()
        task_registry: dict[str, threading.Event] = getattr(request.app.state, "task_registry", {})
        task_registry[task_id] = cancelled
        scope = {
            "conversationId": payload.conversationId,
            "knowledgeBaseIds": payload.knowledgeBaseIds,
            "user": user_id,
            "history": payload.history,
            "task_id": task_id,
            "_cancelled": cancelled,
        }
        full_text_parts: list[str] = []
        # 会话状态卡：本轮开始前加载（注入 system 供 agent 保持一致），
        # 本轮结束后用 agent 自主输出的 memory_delta 合并回写（note-taking）
        session_memory: SessionMemory | None = getattr(request.app.state, "session_memory", None)
        session_id = payload.conversationId
        if session_memory is not None and session_id:
            try:
                scope["session_state"] = session_memory.load(session_id)
            except Exception as e:
                _logger.warning("load session state failed: %s", e)
        try:
            async for token in agent.answer(payload.question, scope=scope):
                if await request.is_disconnected():
                    raise asyncio.CancelledError()
                if _is_task_cancelled(task_id) or cancelled.is_set():
                    _logger.info("chat cancelled by server stop task_id=%s", task_id)
                    cancelled.set()  # 触发 agent 内部协作式取消
                    raise asyncio.CancelledError()
                # token 为 dict：{"type": "response"|"think", "delta": str}
                msg_type = token.get("type", "response")
                delta = token.get("delta", "")
                if msg_type == "think":
                    yield f"event: think\ndata: {json.dumps({'type': 'think', 'delta': delta}, ensure_ascii=False)}\n\n"
                else:
                    full_text_parts.append(delta)
                    yield f"event: message\ndata: {json.dumps({'type': 'response', 'delta': delta}, ensure_ascii=False)}\n\n"
            if not await request.is_disconnected() and not _is_task_cancelled(task_id):
                full_text = "".join(full_text_parts)
                # 引用 [N](#cite-N) 已由 core.source.CitationContextEnricher 注入，
                # 此处不再做后置替换（避免双重替换破坏 [N] 数字括号以外的场景）
                sources = scope.get("_sources") or []
                # grounding_chunks 与 sources 同构（Java 侧落库用）
                grounding_chunks = [
                    {"doc_name": s.get("doc_name"), "text": s.get("excerpt")}
                    for s in sources
                ]
                yield f"event: sources\ndata: {json.dumps({'sources': sources, 'grounding_chunks': grounding_chunks}, ensure_ascii=False)}\n\n"
                yield f"event: complete\ndata: {json.dumps({'full_text': full_text, 'sources': sources, 'grounding_chunks': grounding_chunks, 'ttft_breakdown': scope.get('_ttft_breakdown')}, ensure_ascii=False)}\n\n"
                yield f"event: end\ndata: {json.dumps({'status': 'done', 'request_id': request_id}, ensure_ascii=False)}\n\n"
                # 状态卡回写：agent 自主输出的 memory_delta 合并入库（解析失败静默丢弃）
                if session_memory is not None and session_id:
                    try:
                        session_memory.apply_delta(session_id, str(scope.get("_memory_delta") or ""))
                    except Exception as e:
                        _logger.warning("apply memory_delta failed: %s", e)
                # Assistant persistence is handled by the Java gateway; no double write here.
        except asyncio.CancelledError:
            _logger.info("chat cancelled request_id=%s task_id=%s", request_id, task_id)
            raise
        except Exception as exc:
            _logger.exception("chat failed request_id=%s", request_id)
            if not await request.is_disconnected():
                payload_err = {
                    "success": False,
                    "code": "STREAM_ERROR",
                    "message": str(exc) or "stream failed",
                    "request_id": request_id,
                    "details": None,
                }
                yield f"event: error\ndata: {json.dumps(payload_err, ensure_ascii=False)}\n\n"
        finally:
            task_registry.pop(task_id, None)
            # 释放队列许可
            if ticket_holder:
                try:
                    ticket_holder[0].release()
                except Exception as e:  # noqa: BLE001
                    _logger.debug("释放并发许可失败: %s", e)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/generate-title")
async def generate_title(payload: TitleRequest, request: Request, user=Depends(get_current_user)):
    """为会话生成标题（替代 Java 侧本地 LLM 标题生成）"""
    agent: Agent = request.app.state.agent
    title = await agent.generate_title(payload.question)
    return {"title": title}


@app.post("/recommend")
async def recommend(payload: RecommendRequest, request: Request, user=Depends(get_current_user)):
    """生成推荐追问（替代 Java 侧本地 LLM 推荐）"""
    agent: Agent = request.app.state.agent
    questions = await agent.recommend_questions(payload.question, payload.answer, payload.chunks or [])
    return {"questions": questions}


# ---- 服务端任务取消（对齐 ragent stopTask）----
_cancelled_tasks: set[str] = set()


def _is_task_cancelled(task_id: str) -> bool:
    return task_id in _cancelled_tasks


@app.post("/stop/{task_id}")
async def stop_task(task_id: str, request: Request, user=Depends(get_current_user)):
    """接收 Java 壳的停止请求，标记任务取消。"""
    _cancelled_tasks.add(task_id)
    task_registry: dict[str, threading.Event] = getattr(request.app.state, "task_registry", {})
    running = task_registry.get(task_id)
    if running is not None:
        running.set()
    # 清理：30 秒后自动移除，防止集合无限增长
    async def _cleanup():
        await asyncio.sleep(30)
        _cancelled_tasks.discard(task_id)
    asyncio.create_task(_cleanup())
    return {"success": True, "task_id": task_id}


async def _ingest_bytes(data: bytes, filename: str, collection: str, request: Request):
    settings: AppSettings = request.app.state.settings
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="Payload Too Large")
    try:
        chunks = chunk_file(data, filename)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    stored = request.app.state.documents
    for item in chunks:
        stored.append(StoredChunk(collection, filename, item))
    _save_documents([item.model_dump() for item in stored])
    refresh_runtime_components(request.app)
    return {"filename": filename, "collection": collection, "chunks": chunks, "count": len(chunks)}


@app.post("/ingest")
async def ingest(
    request: Request,
    file: UploadFile | None = File(default=None),
    collection: str = Form(default="default"),
    user=Depends(get_current_user),
):
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload = IngestRequest.model_validate(await request.json())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid ingest JSON") from exc
        return await _ingest_bytes(payload.content.encode("utf-8"), payload.filename, payload.collection, request)
    if file is None:
        raise HTTPException(status_code=400, detail="multipart field 'file' or JSON content is required")
    return await _ingest_bytes(await file.read(), file.filename or "upload.txt", collection, request)


@app.post("/ingest/text")
async def ingest_text(payload: TextIngestRequest, request: Request, user=Depends(get_current_user)):
    return await _ingest_bytes(payload.text.encode("utf-8"), payload.filename, payload.collection, request)


@app.post("/search")
async def search(payload: SearchRequest, request: Request, user=Depends(get_current_user)):
    ensure_runtime_state(request.app)
    if request.app.state.retrieval_engine is None:
        refresh_runtime_components(request.app)
    if request.app.state.retrieval_engine is None:
        return {"query": payload.query, "results": []}
    budget = RetrievalBudget(
        recall_budget=max(payload.top_k, 10),
        candidate_limit=max(payload.top_k * 3, payload.top_k),
        context_top_k=payload.top_k,
    )
    results = await request.app.state.retrieval_engine.retrieve(
        payload.query,
        collections=payload.collections,
        budget=budget,
    )
    return {"query": payload.query, "results": [result.to_dict() for result in results]}


@app.post("/debug/retrieval")
@app.get("/debug/retrieval")
async def debug_retrieval(request: Request, question: str = "", payload: SearchRequest | None = None, rewrite: bool = False):
    # ponytail: 内网诊断专用端点（无鉴权），仅供 eval/诊断脚本直连，勿暴露公网。
    ensure_runtime_state(request.app)
    if request.app.state.retrieval_engine is None:
        refresh_runtime_components(request.app)
    engine = request.app.state.retrieval_engine
    q = question or (payload.query if payload else "")
    if engine is None or not q.strip():
        return {"query": q, "rewritten_query": q, "channels": [], "post_processors": [], "final": [], "retrievedDocIds": []}
    # 对齐生产链路口径：口语场景句直接检索会被 RelevanceGate 误切空，生产先过 QueryRewriter；
    # 且生产保留原句为第一路（agent.py 双路融合消改写方差）、各子查询独立检索后拼接，此处同口径。
    queries = [q]
    if rewrite:
        rr = await request.app.state.agent.rewriter.rewrite(q)
        queries += [x for x in rr.queries if x and x != q]
    rewritten = queries[1] if len(queries) > 1 else q
    budget = RetrievalBudget(recall_budget=10, candidate_limit=20, context_top_k=8)
    # pgvector 语义：空 collections = 零召回；诊断未指定范围时取全库 distinct collection
    pg_backend = getattr(request.app.state, "pg_backend", None)
    collections = pg_backend.list_collections() if pg_backend else []
    scope = RetrievalScope(mode="GLOBAL", collections=collections)
    started = time.perf_counter()
    channel_results = []
    seen: dict[str, object] = {}
    per_query_processors = []
    for query in queries:
        context = engine._build_search_context(query, budget, scope, None, None)
        cr, chs = await engine.retrieve_debug(context)
        channel_results.extend(cr)
        per_query_processors.append({"query": query, "processors": context.metadata.get("post_processors", [])})
        for c in chs:
            prev = seen.get(c.id)
            if prev is None or c.score > prev.score:
                seen[c.id] = c
    # 诊断口径对齐生产：final 窗口 = budget.context_top_k（生产生成上下文条数），
    # 不得再截小，否则 judge 证据窄于生成上下文会造出伪"未支撑"判定。
    chunks = sorted(seen.values(), key=lambda c: -c.score)[: budget.context_top_k]
    # 对齐生产链 DocDiversity（order=12）：为缺席文档做保底补位，口径同 core/retrieval/diversity.py
    chunks = doc_diversity_backup(chunks, list(seen.values()), budget.context_top_k, log_tag="DocDiversityDebug")
    total_ms = (time.perf_counter() - started) * 1000.0
    # metadata jsonb 缺 doc_name 时 pgvector 召回回退 str(doc_id)；此处回表补真实文档名
    # ponytail: 逐条查询 O(n)，诊断端点 n=6 可接受；生产链路应由入库时写全 metadata 解决
    for c in chunks:
        if (not c.doc_name or c.doc_name == c.doc_id) and pg_backend:
            real = pg_backend.get_doc(c.doc_id).get("doc_name")
            if real:
                c.doc_name = real
    # 诊断字段：本次查询实际使用的 embedding 模型（降级链排查用；retrieve 已 embed，此处走缓存）
    embed_info = "n/a"
    if pg_backend:
        er = pg_backend.routing.embed_batch([rewritten])
        embed_info = f"{er.provider}:{er.model_id}"
    return {
        "query": q,
        "rewritten_query": rewritten,
        "embed_model": embed_info,
        "channels": [
            {
                "channel_type": r.channel_type.value,
                "channel_name": r.channel_name,
                "latency_ms": round(r.latency_ms, 1),
                "n_chunks": len(r.chunks),
                "chunks": [
                    {
                        "doc_id": c.doc_id,
                        "doc_name": c.doc_name,
                        "chunk_id": c.id,
                        "score": round(c.score, 4),
                        "preview": (c.text or "")[:80],
                    }
                    for c in r.chunks[:10]
                ],
            }
            for r in channel_results
        ],
        "post_processors": per_query_processors,
        "final": [c.to_dict() for c in chunks],
        # 对齐 Java EvalController 语义：doc_name 剥文件后缀作为业务 docId，与 seed expected_doc_ids 同名
        "retrievedDocIds": sorted(
            {
                (n.rsplit(".", 1)[0] if n.rfind(".") > 0 else n)
                for c in chunks
                if (n := c.doc_name)
            }
        ),
        "total_ms": round(total_ms, 1),
    }


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...), user=Depends(get_current_user)):
    return await _ingest_bytes(await file.read(), file.filename or "upload.txt", "default", request)


@app.get("/")
async def root():
    return {"message": "sentinel-agent is running"}
