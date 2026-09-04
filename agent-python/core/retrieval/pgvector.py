"""PostgreSQL/pgvector 检索后端：直接读取 Java 端入库写入的向量库，与 ragent 原行为一致。

严格对齐 ragent-main PgVectorStoreService 的 schema：
  - 表 t_knowledge_vector(id text, collection_name text, content text, metadata jsonb, embedding vector)
  - 余弦距离用 pgvector 原生 `<#>` 算子（1 - (embedding <=> query) 即余弦相似度）
  - collection_name 字段过滤（对应 Java 知识库隔离）
  - metadata jsonb 含 doc_id / chunk_index / doc_name

这样 Python 检索与 Java 入库共用同一份 pgvector 数据，向量维度 1536 与 Java rag.default.dimension 一致，
embedding 由 RoutingEmbeddingService（Qwen3-Embedding-8B）生成，保证向量可比。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
from typing import Any

from core.embedding import EmbeddingConfig, NoAvailableModelException, RoutingEmbeddingService
from core.retrieval.retriever import VectorBackend
from core.retrieval.types import ChannelType, RetrievedChunk

_logger = logging.getLogger("sentinel.retrieval.pgvector")

# ---------- 查询向量缓存（GPTCache 思想的最小落地：zilliztech/GPTCache） ----------
# 查询 embedding 高度重复（用户/评测反复问同类问题），文档嵌入走 embed_batch 不缓存。
# Redis 不可用时静默直通，不影响主链路。
_QUERY_EMB_CACHE_TTL_ENV = "RAG_QUERY_EMBED_CACHE_TTL"  # 秒；0 = 禁用；默认 3600
_EMBED_RETRY_BACKOFF_S = 0.5  # 链全失败后的单次重试退避（修瞬时网络毛刺；熔断 OPEN 时立即再抛）
_query_emb_cache_client = None
_query_emb_cache_checked = False


def _query_emb_cache():
    global _query_emb_cache_client, _query_emb_cache_checked
    if _query_emb_cache_checked:
        return _query_emb_cache_client
    _query_emb_cache_checked = True
    # ponytail: 配置来源统一走 core.config Settings（pydantic-settings 读 .env），
    # 裸进程启动时 os.environ 没有 REDIS_URL（docker compose --env-file 才注入），裸读会静默禁用缓存
    from core.config.settings import AppSettings

    url = (os.environ.get("REDIS_URL") or AppSettings().redis_url or "").strip()
    if url:
        try:
            import redis as _redis_mod

            _query_emb_cache_client = _redis_mod.Redis.from_url(
                url, socket_timeout=1.0, socket_connect_timeout=1.0
            )
            _query_emb_cache_client.ping()
        except Exception as e:  # noqa: BLE001
            _logger.warning("查询向量缓存不可用，直通: %s", e)
            _query_emb_cache_client = None
    return _query_emb_cache_client


def _cache_ttl() -> int:
    try:
        return int(os.environ.get(_QUERY_EMB_CACHE_TTL_ENV, "3600"))
    except ValueError:
        return 3600


# Qwen3-Embedding 官方 instruction-aware 用法：仅查询侧加前缀，文档入库 embed_batch 不加。
# 探针实测（_probe_crosslingual.py：442 生产 chunk × 100 正例 seed，纯向量 top5）：
# recall@5 75.67% → 86.83%，超过换 bge-m3 重嵌的 84.67% 且零迁移。降级链其他模型带此前缀无害。
_QUERY_INSTRUCTION = (
    "Instruct: Given a user question about AI security risk control, "
    "retrieve the most relevant security documentation\nQuery: "
)


def _embed_with_cache(routing: RoutingEmbeddingService, text: str) -> list[float]:
    ttl = _cache_ttl()
    client = _query_emb_cache() if ttl > 0 else None
    # ponytail: key 不含模型版本维度——更换 embedding 模型须手动清缓存（FLUSH sentinel:qemb:*）。
    # 升级路径：值带 model_id 读时校验，或键加模型前缀
    key = "sentinel:qemb:" + hashlib.md5(text.encode("utf-8")).hexdigest() if client else None
    if client and key:
        try:
            raw = client.get(key)
            if raw:
                return json.loads(raw)
        except Exception:  # noqa: BLE001
            client = None  # 缓存故障后本次直通
    # 降级产出（非默认模型）不入缓存：异构向量空间与库内主模型向量不可比，
    # 写入会让该查询在 TTL 内持续召回正交垃圾（实测毒害 recall 至 0）
    result = routing.embed_batch([text])
    vec = result.vectors[0]
    if client and key and result.model_id == routing.default_model_id():
        try:
            client.setex(key, ttl, json.dumps(vec))
        except Exception:  # noqa: BLE001
            pass
    return vec

# 关键词通道抽词：英文小 token + 停用词过滤；中文连续段取 bigram（pg simple 配置不分词的兜底）
_EN_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_.-]+")
_ZH_SEG_RE = re.compile(r"[\u4e00-\u9fff]+")
_EN_STOPWORDS = {
    "the", "and", "or", "of", "to", "in", "for", "with", "is", "are", "was",
    "were", "a", "an", "on", "at", "by", "as", "it", "its", "this", "that",
    "what", "which", "how", "why", "when", "who", "does", "do", "did", "be",
    "been", "has", "have", "had", "not", "no", "yes", "can", "should", "will",
}

_ZH_STOP_BIGRAMS = {
    "什么", "哪些", "如何", "为什么", "怎么", "是否", "我们", "他们", "然后",
    "如果", "但是", "以及", "进行", "需要", "应该", "可能", "已经", "还是",
}

# 关键词通道准入：命中词元数下限（中文长句的有效词元本就稀疏，取 1，改由 BM25 分数兜底去噪）
_MIN_HIT_TERMS = 1
# 无区分度词元阈值：DF 占库内 chunk 比例超过此值即视为通用词，剔除出打分
_GENERIC_DF_RATIO = 0.5
# BM25 参数（标准取值）
_BM25_K1 = 1.5
_BM25_B = 0.75
# BM25 绝对分数下限：低于此值的候选视为噪声，不返回
_BM25_MIN_SCORE = 1.0

# 关键词通道总开关（默认关闭，见 retrieve_keyword 文档字符串的实测结论）
_KEYWORD_CHANNEL_ENABLED_ENV = "RAG_KEYWORD_CHANNEL_ENABLED"
_KEYWORD_CHANNEL_ENABLED_VALUES = {"1", "true", "yes", "on"}

_log = math.log


def _keyword_channel_enabled() -> bool:
    """关键词通道是否启用。

    默认关闭：本实现基于「中文 bigram + ILIKE」，无中文分词器时词元碎片化严重
    （实测单条查询 46 个 bigram 里仅 10 个在库内有 DF），在中英混合风控语料上
    30 条种子 A/B 显示 Recall 无稳定提升而 MRR 下降 0.12~0.22，属净负收益。
    接入中文分词（如 pg_jieba / 分词后入库）后再置 RAG_KEYWORD_CHANNEL_ENABLED=1 启用。
    """
    return os.environ.get(_KEYWORD_CHANNEL_ENABLED_ENV, "").strip().lower() in _KEYWORD_CHANNEL_ENABLED_VALUES


def _bm25(tf: int, dl: int, avgdl: float, idf: float) -> float:
    """单项 BM25 贡献：IDF × 词频饱和 × 文档长度归一化。

    长度归一化是关键：不做归一化时，长 chunk 仅凭体量就能命中更多词元而系统性
    排在前（实测 C13 类长文档压过只命中单个高区分度词的正确文档）。
    """
    if avgdl <= 0:
        avgdl = 1.0
    norm = 1.0 - _BM25_B + _BM25_B * (dl / avgdl)
    return idf * (tf * (_BM25_K1 + 1.0)) / (tf + _BM25_K1 * norm)


def _extract_keyword_terms(query: str) -> tuple[list[str], list[str]]:
    """从查询抽取关键词词元：(英文词, 中文 bigram)。

    英文：长度 >=2 的字母 token，lower，去停用词，去重保序。
    中文：连续汉字段生成相邻 bigram，去停用 bigram 与重复，截前 16 个。
    """
    en_terms: list[str] = []
    for m in _EN_TOKEN_RE.finditer(query):
        t = m.group(0).lower().strip(".-_")
        if len(t) >= 2 and t not in _EN_STOPWORDS and t not in en_terms:
            en_terms.append(t)
    zh_terms: list[str] = []
    for seg in _ZH_SEG_RE.finditer(query):
        s = seg.group(0)
        grams = [s[i : i + 2] for i in range(len(s) - 1)] if len(s) >= 2 else []
        for g in grams:
            if g not in _ZH_STOP_BIGRAMS and g not in zh_terms:
                zh_terms.append(g)
    # 上限够大以覆盖长句后段的实义词（短截会把"校验"这类后段关键词整段丢掉）
    return en_terms[:24], zh_terms[:48]


class PgVectorBackend(VectorBackend):
    """pgvector 向量后端（对应 ragent PgVectorRetrieverService）。

    查询 SQL（Cosine，pgvector <=>）：
        SELECT id, content, metadata, 1 - (embedding <=> $1) AS score
        FROM t_knowledge_vector
        WHERE collection_name = ANY($2)
        ORDER BY embedding <=> $1
        LIMIT $3
    """

    def __init__(
        self,
        routing: RoutingEmbeddingService,
        dsn: str | None = None,
        table: str = "t_knowledge_vector",
        config: EmbeddingConfig | None = None,
    ):
        if routing is None:
            raise ValueError("PgVectorBackend 必须传入 routing（RoutingEmbeddingService）")
        self.routing = routing
        self.config = config or EmbeddingConfig()
        # DSN 统一走 core.db（与 Java application.yaml datasource 对齐：sentinel 库）
        from core.db import get_dsn

        self.dsn = dsn or get_dsn()
        self.table = table
        self._pool = None  # 延迟创建，避免无 pg 依赖时启动崩溃

    def _get_pool(self):
        if self._pool is None:
            try:
                import psycopg_pool
            except ImportError as e:
                raise ImportError(
                    "PgVectorBackend 需要 psycopg_pool，请执行：\n  pip install psycopg_pool"
                ) from e
            self._pool = psycopg_pool.ConnectionPool(
                self.dsn, open=True, max_size=4, kwargs={"autocommit": True}
            )
        return self._pool

    def get_doc(self, doc_id: str | None) -> dict[str, str]:
        if not doc_id:
            return {}
        sql = (
            "SELECT doc_name, source_type, file_type, source_location "
            "FROM t_knowledge_document "
            "WHERE id = %s AND deleted = 0 "
            "LIMIT 1"
        )
        try:
            pool = self._get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(sql, (str(doc_id),))
                row = cur.fetchone()
        except Exception as exc:
            _logger.warning("document metadata lookup failed doc_id=%s: %s", doc_id, exc)
            return {}
        if not row:
            return {}
        doc_name, source_type, file_type, source_location = row
        return {
            "doc_name": doc_name or "",
            "source_type": source_type or "",
            "file_type": file_type or "",
            "url": source_location or "",
        }

    async def embed(self, text: str) -> list[float]:
        # instruction 拼在最前 → 缓存 key 随之改变，旧的无 instruction 缓存自然失效
        qtext = _QUERY_INSTRUCTION + text
        try:
            return await asyncio.to_thread(_embed_with_cache, self.routing, qtext)
        except NoAvailableModelException:
            # 链全失败（embedding 短暂不可用窗口）退避后重试 1 次，避免空检索直接拒答
            _logger.warning("embed 链全失败，%.1fs 后重试 1 次", _EMBED_RETRY_BACKOFF_S)
            await asyncio.sleep(_EMBED_RETRY_BACKOFF_S)
            return await asyncio.to_thread(_embed_with_cache, self.routing, qtext)

    def _row_to_chunk(
        self,
        rid: Any,
        collection_name: Any,
        content: Any,
        metadata: Any,
        score: Any,
        channel: ChannelType,
    ) -> RetrievedChunk:
        meta = metadata or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        doc_id = meta.get("doc_id") or rid
        return RetrievedChunk(
            id=str(rid),
            text=content or "",
            score=float(score or 0.0),
            collection_name=collection_name or meta.get("collection_name"),
            doc_id=str(doc_id),
            chunk_index=meta.get("chunk_index", 0),
            doc_name=meta.get("doc_name") or meta.get("document_name") or str(doc_id),
            channel=channel,
            source_type=meta.get("source_type") or meta.get("sourceType"),
            file_type=meta.get("file_type") or meta.get("fileType"),
            url=meta.get("url"),
        )

    def list_collections(self) -> list[str]:
        """全库 distinct collection 名（诊断端点用：未指定范围时取全库）。"""
        try:
            pool = self._get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    f"SELECT DISTINCT collection_name FROM {self.table} WHERE collection_name IS NOT NULL"
                )
                return sorted(r[0] for r in cur.fetchall() if r[0])
        except Exception as exc:
            _logger.warning("list_collections failed: %s", exc)
            return []

    def list_doc_names(self) -> list[str]:
        """全库 doc_name 列表（QueryRewriter 文档域感知用；失败返回空=不注入）。"""
        try:
            pool = self._get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT doc_name FROM t_knowledge_document WHERE doc_name IS NOT NULL"
                )
                return sorted(r[0] for r in cur.fetchall() if r[0])
        except Exception as exc:
            _logger.warning("list_doc_names failed: %s", exc)
            return []

    async def retrieve(
        self, query: str, collections: list[str], top_k: int
    ) -> list[RetrievedChunk]:
        if not collections:
            return []
        qv = await self.embed(query)
        # pgvector 文本字面量：[v1,v2,...]
        vec_literal = "[" + ",".join(f"{x:.8g}" for x in qv) + "]"

        sql = (
            f"SELECT id, collection_name, content, metadata, 1 - (embedding <=> %s::vector) AS score "
            f"FROM {self.table} "
            f"WHERE collection_name = ANY(%s) "
            f"ORDER BY embedding <=> %s::vector "
            f"LIMIT %s"
        )
        params = (vec_literal, list(collections), vec_literal, int(top_k))

        def _query() -> list[RetrievedChunk]:
            pool = self._get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
            return [
                self._row_to_chunk(rid, collection_name, content, metadata, score, ChannelType.VECTOR)
                for rid, collection_name, content, metadata, score in rows
            ]

        try:
            return await asyncio.to_thread(_query)
        except Exception as exc:
            _logger.warning("pgvector 检索失败: %s", exc)
            return []

    def _term_document_frequencies(
        self, terms: list[str], collections: list[str]
    ) -> tuple[int, float, dict[str, int]]:
        """统计库内 chunk 总数、平均长度与每个词元的文档频率 DF（一次批量查询）。

        DF 用于 IDF：中文 bigram 里大量跨词边界碎片（如"当安""全测"）DF=0，
        既无召回价值又稀释打分，必须先剔除。平均长度供 BM25 长度归一化使用。
        """
        if not terms:
            return 0, 0.0, {}
        sql = (
            f"SELECT (SELECT COUNT(*) FROM {self.table} WHERE collection_name = ANY(%s)) AS n, "
            f"       (SELECT COALESCE(AVG(LENGTH(content)), 0) FROM {self.table} "
            f"        WHERE collection_name = ANY(%s)) AS avgdl, "
            f"       w.word, COUNT(v.id)::int "
            f"FROM unnest(%s::text[]) AS w(word) "
            f"LEFT JOIN {self.table} v "
            f"  ON v.collection_name = ANY(%s) AND v.content ILIKE '%%' || w.word || '%%' "
            f"GROUP BY w.word"
        )
        pool = self._get_pool()
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (list(collections), list(collections), list(terms), list(collections)))
            rows = cur.fetchall()
        if not rows:
            return 0, 0.0, {}
        total = int(rows[0][0] or 0)
        avgdl = float(rows[0][1] or 0.0)
        return total, avgdl, {r[2]: int(r[3] or 0) for r in rows}

    async def retrieve_keyword(
        self, query: str, collections: list[str], top_k: int
    ) -> list[RetrievedChunk]:
        """关键词通道（稀疏检索）。

        原实现用 plainto_tsquery('simple', 中文长句)：其 AND 语义要求全部词元同现，
        且 simple 配置对中文不分词，导致中文查询恒返回 0 条——双通道架构里关键词
        通道实际从未生效。本实现修正为：
          - OR 语义拉候选（英文走 tsquery，中文走 ILIKE 兜底）
          - 剔除 DF=0 的无效词元，按 IDF 加权 + BM25 长度归一化打分
          - 准入阈值：低于 _BM25_MIN_SCORE 的候选视为噪声不返回

        默认关闭（_keyword_channel_enabled）：无中文分词器时该通道实测净负收益，
        详见该函数文档。启用方式：环境变量 RAG_KEYWORD_CHANNEL_ENABLED=1。
        """
        if not _keyword_channel_enabled():
            return []
        if not query.strip() or not collections:
            return []
        en_terms, zh_terms = _extract_keyword_terms(query)
        if not en_terms and not zh_terms:
            return []

        def _query() -> list[RetrievedChunk]:
            pool = self._get_pool()
            all_terms = en_terms + zh_terms
            total, avgdl, df = self._term_document_frequencies(all_terms, collections)
            if total <= 0:
                return []
            # IDF = log(N / (1 + DF))，DF=0 的词元不参与（无召回价值且稀释打分）
            idf = {t: _log(total / (1 + df.get(t, 0))) for t in all_terms if df.get(t, 0) > 0}
            if not idf:
                return []
            # 无区分度词元（出现在过半库内 chunk 中）剔除，防止通用词主导打分
            generic = {t for t, d in df.items() if d > total * _GENERIC_DF_RATIO}
            effective = {t: w for t, w in idf.items() if t not in generic}
            if not effective:
                return []

            # OR 语义拉候选；参数顺序严格对齐 SQL 中 %s 出现次序
            conds, where_params = [], []
            if any(t in effective for t in en_terms):
                conds.append("to_tsvector('simple', coalesce(content, '')) @@ to_tsquery('simple', %s)")
                where_params.append(" | ".join(t for t in en_terms if t in effective))
            zh_eff = [t for t in zh_terms if t in effective]
            if zh_eff:
                conds.append("content ILIKE ANY(%s)")
                where_params.append([f"%{w}%" for w in zh_eff])
            sql = (
                f"SELECT id, collection_name, content, metadata "
                f"FROM {self.table} "
                f"WHERE collection_name = ANY(%s) "
                f"AND ({' OR '.join(conds)}) "
                f"LIMIT %s"
            )
            params = (list(collections), *where_params, int(top_k) * 3)
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

            scored = []
            for rid, collection_name, content, metadata in rows:
                text = (content or "").lower()
                hit_terms = [t for t in effective if t in text]
                if len(hit_terms) < _MIN_HIT_TERMS:
                    continue
                dl = len(text)
                score = sum(_bm25(text.count(t), dl, avgdl, effective[t]) for t in hit_terms)
                if score < _BM25_MIN_SCORE:
                    continue
                scored.append((score, rid, collection_name, content, metadata))
            scored.sort(key=lambda x: -x[0])
            return [
                self._row_to_chunk(rid, collection_name, content, metadata, score, ChannelType.KEYWORD)
                for score, rid, collection_name, content, metadata in scored[: int(top_k)]
            ]

        try:
            return await asyncio.to_thread(_query)
        except Exception as exc:
            _logger.warning("pg keyword 检索失败: %s", exc)
            return []
