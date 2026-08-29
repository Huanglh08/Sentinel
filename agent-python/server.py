from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from core.agent import Agent
from core.case import CaseInvestigationAgent
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
from core.retrieval.engine import ChannelConfig, MultiChannelRetrievalEngine, RetrievalConfig
from core.retrieval.external_channels import LightRagBackend, LightRagConfig, YouComWebSearchBackend, WebSearchConfig
from core.retrieval.rerank import CrossEncoderReranker, LLMReranker, RoutingReranker
from core.retrieval.types import ChannelType, RetrievedChunk, RetrievalBudget
from core.security import SecurityService, get_current_user, get_security_service, get_settings
from core.tracing import Tracer
from core.admin import (
    build_sample_question_router, build_query_term_mapping_router, build_eval_router,
    build_knowledge_base_router, build_knowledge_document_router, build_knowledge_chunk_router)
from core.demo_mode import DemoModeMiddleware, DemoModeProperties
from core.ratelimit import ChatQueueLimiter, FairDistributedRateLimiter, RAGRateLimitProperties
from core.memory.store import JdbcConversationMemoryStore, DefaultConversationMemoryService, ConversationRepository, next_message_id

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


class RecommendRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20000)
    answer: str = Field(default="", max_length=60000)
    chunks: list[dict] | None = Field(default=None, description="[{doc_name, text}]")


class SummarizeRequest(BaseModel):
    messages: list[dict] = Field(default_factory=list, description="[{role, content}]")
    existing_summary: str = Field(default="", max_length=20000)


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
    # 1:1 翻译 ragent ConversationMemoryService 初始化
    memory_repo = ConversationRepository()
    memory_store = JdbcConversationMemoryStore(repo=memory_repo)
    application.state.memory_service = DefaultConversationMemoryService(store=memory_store)
    refresh_runtime_components(application)
    yield
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
    except Exception as exc:
        _logger.warning("pgvector 检索后端初始化失败，检索降级为空: %s", exc)
        return None

    async def vector_runner(query: str, collections: list[str], top_k: int):
        return await pg.retrieve(query, collections, top_k)

    async def keyword_runner(query: str, collections: list[str], top_k: int):
        return await pg.retrieve_keyword(query, collections, top_k)

    search_config = application.state.search_channel_config
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
    )
    reranker = RoutingReranker([
        CrossEncoderReranker(),
        LLMReranker(_build_llm_service()),
    ])
    return MultiChannelRetrievalEngine(channels, retrieval_config, reranker=reranker), pg


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
        application.state.agent = Agent(
            llm_service,
            retriever=retrieval_engine,
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
def _get_case_agent(request: Request) -> CaseInvestigationAgent:
    """懒加载案件调查 Agent：复用主 Agent 的 llm/mcp 实例（共享路由与 MCP 降级底座）。"""
    case_agent = getattr(request.app.state, "case_agent", None)
    if case_agent is None:
        main_agent: Agent = request.app.state.agent
        case_agent = CaseInvestigationAgent(main_agent.llm, main_agent.mcp)
        request.app.state.case_agent = case_agent
    return case_agent


@app.post("/case/investigate")
async def case_investigate(payload: CaseInvestigateRequest, request: Request, user=Depends(get_current_user)):
    """线索 -> 分诊 -> 受控多步调查 -> 结构化报告（SSE 逐步推送证据链）。"""
    case_agent = _get_case_agent(request)
    user_id = getattr(user, "username", None) or getattr(user, "id", None) or "anonymous"

    async def events() -> AsyncIterator[str]:
        try:
            async for event in case_agent.investigate(payload.clue, user_id=str(user_id), source=payload.source):
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
        )
    }


@app.post("/case/approvals/{approval_id}/decide")
async def approval_decide(
    approval_id: str, payload: ApprovalDecideRequest, request: Request, user=Depends(get_current_user)
):
    """审批裁决：APPROVED / REJECTED。重复裁决幂等返回当前状态。"""
    case_agent = _get_case_agent(request)
    operator = getattr(user, "username", None) or getattr(user, "id", None) or "anonymous"
    result = case_agent.store.decide_approval(
        approval_id, status=payload.decision, operator=str(operator), opinion=payload.opinion
    )
    if result is None:
        raise HTTPException(status_code=404, detail="approval not found")
    return result


@app.get("/case/{case_id}")
async def case_detail(case_id: str, request: Request, user=Depends(get_current_user)):
    """查询单个案件（含证据链全量）。"""
    case_agent = _get_case_agent(request)
    case = case_agent.store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    return case


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
    memory_service = getattr(request.app.state, "memory_service", None)
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

        # 等待限流结果
        wait_tasks = [acquired.wait(), rejected.wait()]
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
        try:
            # g2 layered mode: persistence (user/assistant messages + conversation
            # record) belongs to the Java gateway (PythonRAGChatServiceImpl, the
            # counterpart of ragent RAGChatServiceImpl). This endpoint only loads
            # history for generation context; persisting here would duplicate rows.
            if memory_service is not None:
                try:
                    history_messages = memory_service.load(payload.conversationId, str(user_id))
                    if history_messages and not payload.history:
                        scope["history"] = [
                            {"role": m.role.value, "content": m.content}
                            for m in history_messages
                        ]
                except Exception as e:
                    _logger.warning("load memory failed: %s", e)

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
                yield f"event: complete\ndata: {json.dumps({'full_text': full_text, 'sources': sources, 'grounding_chunks': grounding_chunks}, ensure_ascii=False)}\n\n"
                yield f"event: end\ndata: {json.dumps({'status': 'done', 'request_id': request_id}, ensure_ascii=False)}\n\n"
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
                except Exception:
                    pass

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


@app.post("/summarize")
async def summarize(payload: SummarizeRequest, request: Request, user=Depends(get_current_user)):
    """生成会话记忆摘要（替代 Java 侧本地 LLM 摘要）"""
    agent: Agent = request.app.state.agent
    summary = await agent.summarize(payload.messages, payload.existing_summary)
    return {"summary": summary}


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


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...), user=Depends(get_current_user)):
    return await _ingest_bytes(await file.read(), file.filename or "upload.txt", "default", request)


@app.get("/")
async def root():
    return {"message": "sentinel-agent is running"}
