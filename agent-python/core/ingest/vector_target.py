"""VectorTarget：严格对应 ragent bootstrap core.ingest.VectorTarget record。

记录"这一批向量归属于哪个 partition / 用哪个 embedding 模型 / 维度多少"——
与 EmbeddedChunk 协同，写入向量库时用于按模型/维度过滤。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VectorTarget:
    """严格对应 ragent VectorTarget record。

    字段：
      partition: 业务分区（"default" / "tenant-a" 等）
      embedding_model: 候选模型 id（如 "qwen-emb-8b"）
      dimension: 向量维度（与 candidate.dimension 一致；ragent 用此做列宽校验）
    """

    partition: str
    embedding_model: str
    dimension: int
