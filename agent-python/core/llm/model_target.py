"""ModelTarget record：严格对齐 ragent infra-ai.infra.model.ModelTarget。

java record：
  public record ModelTarget(
      String id,                                    // 模型唯一 ID
      AIModelProperties.ModelCandidate candidate,   // 候选配置
      AIModelProperties.ProviderConfig provider,    // 提供商配置
      Long timeoutMs                                 // 档位超时（ms）
  )
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.llm.config import ModelCandidate, ProviderConfig


@dataclass(frozen=True)
class ModelTarget:
    """严格对应 ragent ModelTarget record（4 字段）。"""

    id: str
    candidate: "ModelCandidate"
    provider: "ProviderConfig"
    timeout_ms: int | None = None
