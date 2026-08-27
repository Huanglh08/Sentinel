"""ChunkEmbeddingService：严格对应 ragent bootstrap core.ingest.embed.ChunkEmbeddingService。

照搬语义：
  - 入参：List[Chunk], model_id (候选 id), VectorTarget (partition + embeddingModel + dimension)
  - 空 chunks → []
  - 收集 chunk.content 为 texts；调 RoutingEmbeddingService.embed_batch(texts, model_id)
  - 校验返回 dimension 与 VectorTarget.dimension 一致
  - 用 EmbeddingResult.vectors[i] 与 chunks[i] 配对，构造 EmbeddedChunk 列表

ragent 的关键设计：模型归属通过 VectorTarget 传递，EmbeddedChunk 只持有向量。
"""
from __future__ import annotations

from core.chunk.types import Chunk, EmbeddedChunk
from core.embedding.config import EmbeddingConfig
from core.embedding.errors import ModelClientError, ModelClientErrorType
from core.embedding.routing import RoutingEmbeddingService
from core.ingest.vector_target import VectorTarget


class ChunkEmbeddingService:
    """严格对应 ragent ChunkEmbeddingService。

    用法：
        svc = ChunkEmbeddingService(routing, config)
        embedded = svc.embed(chunks, model_id="qwen-emb-8b", target=VectorTarget("default", "qwen-emb-8b", 1536))
    """

    def __init__(
        self,
        routing: RoutingEmbeddingService,
        config: EmbeddingConfig,
    ) -> None:
        self._routing = routing
        self._config = config

    def embed(
        self,
        chunks: list[Chunk],
        model_id: str | None,
        target: VectorTarget,
    ) -> list[EmbeddedChunk]:
        """对应 ragent ChunkEmbeddingService.embed()。"""
        if not chunks:
            return []

        texts = [c.content for c in chunks]
        result = self._routing.embed_batch(texts, model_id)

        if result.dimension != target.dimension:
            raise ModelClientError(
                f"嵌入返回维度 {result.dimension} 与 VectorTarget.dimension {target.dimension} 不一致",
                error_type=ModelClientErrorType.INVALID_RESPONSE,
            )

        out: list[EmbeddedChunk] = []
        for chunk, vec in zip(chunks, result.vectors):
            # 严格对齐 ragent EmbeddedChunk record（2 字段：chunk + embedding）
            out.append(EmbeddedChunk(chunk=chunk, embedding=list(vec)))
        return out
