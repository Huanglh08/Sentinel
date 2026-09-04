"""检索层：多通道并行 + RRF 融合 + 去重 + 可插拔 Rerank。

默认生产栈（严格对齐 ragent infra-ai.embedding 家族）：
  - 向量嵌入：RoutingEmbeddingService（Qwen3-Embedding-8B @ 硅基流动，1536 维）
  - 向量索引：faiss-cpu IndexFlatIP
  - 关键词通道：rank-bm25.BM25Okapi（jieba 分词，缺失则字符 bigram 退路）
  - 精排：BAAI/bge-reranker-base（见 rerank.py）

依赖缺失抛 ImportError 并给出 pip 安装命令——不静默退化。
"""
from core.retrieval.types import (
    ChannelType, RetrievedChunk, SearchChannelResult, RetrievalBudget,
)
from core.retrieval.retriever import (
    VectorBackend, KeywordBackend,
    FaissVectorBackend, BM25Backend, HybridRetriever,
)
from core.retrieval.engine import (
    MultiChannelRetrievalEngine, RetrievalConfig, ChannelConfig,
)
from core.retrieval.rerank import Reranker, SiliconFlowReranker, CrossEncoderReranker, LLMReranker, RoutingReranker
from core.retrieval.types import BY_SCORE_DESC  # noqa: F401  静态比较器导出

__all__ = [
    "ChannelType", "RetrievedChunk", "SearchChannelResult", "RetrievalBudget", "BY_SCORE_DESC",
    "VectorBackend", "KeywordBackend",
    "FaissVectorBackend", "BM25Backend", "HybridRetriever",
    "MultiChannelRetrievalEngine", "RetrievalConfig", "ChannelConfig",
    "Reranker", "CrossEncoderReranker", "LLMReranker", "RoutingReranker",
]
