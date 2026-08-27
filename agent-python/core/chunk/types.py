"""分块数据模型：严格对应 ragent bootstrap core.chunk.model.*。

核心语义（严格对齐 ragent java 版）：
  - ChunkMetadata：3 字段（provenance/heading_path/extra）—— 对应 ragent ChunkMetadata record
  - Chunk：5 字段（id/index/content/embeddingText/metadata）—— 对应 ragent Chunk record
  - EmbeddedChunk：2 字段（chunk + float[] embedding）—— 对应 ragent EmbeddedChunk record
  - EmbeddedChunk 6 个委托属性（chunkId/index/content/embeddingText/metadata/dimension）→ 委托给 chunk
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ChunkBudget:
    max_tokens: int = 512
    overlap: int = 64
    whole_document: bool = False


@dataclass
class ChunkMetadata:
    """严格对应 ragent bootstrap core.chunk.model.ChunkMetadata record（3 字段）。"""

    provenance: str = ""
    heading_path: str | list[str] = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Chunk:
    """严格对应 ragent bootstrap core.chunk.model.Chunk record（5 字段）。

    字段：
      id:             片段唯一 id（java: chunkId）
      index:          在文档内的分块索引（java: index）
      content:        片段正文（java: content）
      embedding_text: 向量化输入（java: embeddingText，默认 = content）
      metadata:       片段元数据（java: ChunkMetadata）
    """
    id: str
    index: int
    content: str
    embedding_text: str = ""
    metadata: ChunkMetadata = field(default_factory=ChunkMetadata)

    def __post_init__(self) -> None:
        # 严格对齐 ragent：embeddingText 默认 = content（向量化时直接用 content 喂入）
        if not self.embedding_text:
            self.embedding_text = self.content

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "index": self.index,
            "content": self.content,
            "embedding_text": self.embedding_text,
            "metadata": {
                "provenance": self.metadata.provenance,
                "heading_path": self.metadata.heading_path,
                "extra": self.metadata.extra,
            },
        }


@dataclass
class EmbeddedChunk:
    """严格对应 ragent bootstrap core.chunk.model.EmbeddedChunk record（2 字段）。

    ragent 原生 record：
      public record EmbeddedChunk(Chunk chunk, float[] embedding)

    6 个委托属性对应 ragent EmbeddedChunk 6 个 getter：
      chunk_id, index, content, embedding_text, metadata, dimension
    """
    chunk: Chunk
    embedding: list[float]

    # ----- 委托属性（严格对齐 ragent EmbeddedChunk 的 6 个 getter） -----
    @property
    def chunk_id(self) -> str:
        return self.chunk.id

    @property
    def index(self) -> int:
        return self.chunk.index

    @property
    def content(self) -> str:
        return self.chunk.content

    @property
    def embedding_text(self) -> str:
        return self.chunk.embedding_text

    @property
    def metadata(self) -> ChunkMetadata:
        return self.chunk.metadata

    @property
    def dimension(self) -> int:
        return len(self.embedding)
