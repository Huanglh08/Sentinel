"""ingest 层：严格对应 ragent bootstrap core.ingest。

子模块：
  - embed: ChunkEmbeddingService（把 Chunk 列表打平成文本 → 走 RoutingEmbeddingService → EmbeddedChunk）
  - vector_target: VectorTarget 记录（partition + embeddingModel + dimension）
"""
from core.ingest.vector_target import VectorTarget

__all__ = ["VectorTarget"]
