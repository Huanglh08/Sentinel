"""嵌入配置：严格对应 ragent infra-ai.config.AIModelProperties + bootstrap application.yaml。

读取优先级：环境变量 > .env > 默认值（pydantic-settings 标准行为）。
默认候选链与 ragent application.yaml L253-270 完全一致：
  1) qwen-emb-8b (siliconflow, Qwen/Qwen3-Embedding-8B, 1536 维)
  2) qwen-emb-local (ollama, qwen3-embedding:8b-fp16, 1536 维)
  3) text-embedding-3-large (aihubmix, 1536 维)

维度与 ragent rag.default.dimension: 1536 完全一致。
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderConfig(BaseModel):
    """提供商配置——严格对应 ragent AIModelProperties.ProviderConfig。"""

    url: str
    api_key: str = ""
    endpoints: dict[str, str] = Field(default_factory=dict)


class ModelCandidate(BaseModel):
    """候选模型配置——严格对应 ragent AIModelProperties.ModelCandidate。"""

    id: str
    provider: str
    model: str
    dimension: int
    priority: int = 0
    extra: dict[str, Any] = Field(default_factory=dict)


_DEFAULT_CANDIDATES: list[dict[str, Any]] = [
    {"id": "qwen-emb-8b", "provider": "siliconflow", "model": "Qwen/Qwen3-Embedding-8B", "dimension": 1536, "priority": 1},
    {"id": "qwen-emb-local", "provider": "ollama", "model": "qwen3-embedding:8b-fp16", "dimension": 1536, "priority": 2},
    {"id": "text-embedding-3-large", "provider": "aihubmix", "model": "text-embedding-3-large", "dimension": 1536, "priority": 3},
]


class EmbeddingConfig(BaseSettings):
    """嵌入层总配置——pydantic-settings 读取 .env + 环境变量。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- providers ----------------
    siliconflow_url: str = "https://api.siliconflow.cn"
    siliconflow_api_key: str = ""
    siliconflow_endpoints: dict[str, str] = Field(
        default_factory=lambda: {"embedding": "/v1/embeddings", "chat": "/v1/chat/completions"}
    )

    ollama_url: str = "http://localhost:11434"
    ollama_endpoints: dict[str, str] = Field(default_factory=lambda: {"embedding": "/v1/embeddings"})

    aihubmix_url: str = "https://aihubmix.com/v1"
    aihubmix_api_key: str = ""
    aihubmix_endpoints: dict[str, str] = Field(default_factory=lambda: {"embedding": "/v1/embeddings"})

    # ---------------- embedding selection ----------------
    embedding_default_model: str = "qwen-emb-8b"
    embedding_dim: int = 1536
    embedding_candidates: list[ModelCandidate] = Field(
        default_factory=lambda: [ModelCandidate(**c) for c in _DEFAULT_CANDIDATES]
    )

    # ---------------- circuit breaker (对应 ai.selection) ----------------
    embedding_failure_threshold: int = 2
    embedding_open_duration_ms: int = 30_000

    # ---------------- validators ----------------

    @field_validator("embedding_candidates", mode="before")
    @classmethod
    def _parse_candidates(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [ModelCandidate(**item) for item in json.loads(v)] if v.strip() else []
        return v

    @field_validator("siliconflow_endpoints", "ollama_endpoints", "aihubmix_endpoints", mode="before")
    @classmethod
    def _parse_endpoints(cls, v: Any) -> Any:
        if isinstance(v, str):
            return json.loads(v) if v.strip() else {}
        return v

    # ---------------- helpers ----------------

    def get_provider(self, name: str) -> ProviderConfig:
        """按 provider id 取配置——对应 ragent AIModelProperties.getProvider()。"""
        p = name.lower()
        if p == "siliconflow":
            return ProviderConfig(
                url=self.siliconflow_url,
                api_key=self.siliconflow_api_key,
                endpoints=dict(self.siliconflow_endpoints),
            )
        if p == "ollama":
            return ProviderConfig(url=self.ollama_url, api_key="", endpoints=dict(self.ollama_endpoints))
        if p == "aihubmix":
            return ProviderConfig(
                url=self.aihubmix_url,
                api_key=self.aihubmix_api_key,
                endpoints=dict(self.aihubmix_endpoints),
            )
        raise ValueError(f"未知 provider: {name}")

    def candidates_sorted(self) -> list[ModelCandidate]:
        """按 priority 升序返回候选链——与 ragent executeWithFallback 排序约定一致。"""
        return sorted(self.embedding_candidates, key=lambda c: c.priority)

    def default_candidate(self) -> ModelCandidate:
        """根据 embedding_default_model 选候选——对应 ragent RoutingEmbeddingService 主入口。"""
        for c in self.embedding_candidates:
            if c.id == self.embedding_default_model:
                return c
        if not self.embedding_candidates:
            raise ValueError("未配置任何 embedding 候选")
        return self.embedding_candidates[0]
