"""1:1 翻译 ragent rag.config.RAGConfigProperties。

字段（严格按 Java @Value 注解 + 默认值）：
  - query_rewrite_enabled: bool, default True
  - rerank_enabled: bool, default True
  - context_enrich_enabled: bool, default True
  - citation_enabled: bool, default False
  - active_collections: list[str], default []

加载机制（替代 ragent Spring @ConfigurationProperties）：
  - __init__(env=os.environ, yml=...) 接受环境变量 + dict 两路注入
  - 环境变量优先（与 ragent @Value 行为一致）
  - 同时支持 yml 风格 dict（便于配置文件接入）

差异：ragent 走 Spring 容器 + @ConfigurationProperties；agent-python 走纯数据类。
**接口 1:1**，加载机制 pydantic-settings 后置（D3 进一步工作）
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class RAGConfigProperties:
    """1:1 翻译 ragent rag.config.RAGConfigProperties。"""
    query_rewrite_enabled: bool = True
    rerank_enabled: bool = True
    context_enrich_enabled: bool = True
    citation_enabled: bool = False
    active_collections: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "RAGConfigProperties":
        """1:1 翻译 ragent @Value("${rag.query-rewrite.enabled:true}") 注入机制。

        支持的环境变量（与 ragent key 严格对应）：
          RAG_QUERY_REWRITE_ENABLED
          RAG_RERANK_ENABLED
          RAG_CONTEXT_ENRICH_ENABLED
          RAG_CITATION_ENABLED
          RAG_ACTIVE_COLLECTIONS（逗号分隔）
        """
        env = env if env is not None else dict(os.environ)
        def _bool(name: str, default: bool) -> bool:
            raw = env.get(name, "").strip().lower()
            if not raw:
                return default
            return raw in ("1", "true", "yes", "on")
        active = env.get("RAG_ACTIVE_COLLECTIONS", "").strip()
        active_list = [s.strip() for s in active.split(",") if s.strip()] if active else []
        return cls(
            query_rewrite_enabled=_bool("RAG_QUERY_REWRITE_ENABLED", True),
            rerank_enabled=_bool("RAG_RERANK_ENABLED", True),
            context_enrich_enabled=_bool("RAG_CONTEXT_ENRICH_ENABLED", True),
            citation_enabled=_bool("RAG_CITATION_ENABLED", False),
            active_collections=active_list,
        )

    @classmethod
    def merge(cls, env: dict[str, str] | None = None, yml: dict | None = None) -> "RAGConfigProperties":
        """两路合并：env 优先（与 Spring @Value 行为一致），缺省时回退 yml。

        规则：
          1) env 显式设置（key 存在且非空）→ 用 env 值
          2) 否则 yml 显式提供 → 用 yml 值
          3) 否则用 default
        """
        from_env = cls.from_env(env)
        if yml is None:
            return from_env
        y = yml.get("rag") or {}
        q = y.get("query-rewrite") or {}
        r = y.get("rerank") or {}
        c = y.get("context") or {}
        cit = y.get("citation") or {}
        active = y.get("active-collections")

        def _env_or(key: str, yml_value, env_value):
            if env and key in env and (env.get(key) or "").strip():
                return env_value
            return yml_value if yml_value is not None else env_value

        return cls(
            query_rewrite_enabled=_env_or("RAG_QUERY_REWRITE_ENABLED", q.get("enabled"), from_env.query_rewrite_enabled),
            rerank_enabled=_env_or("RAG_RERANK_ENABLED", r.get("enabled"), from_env.rerank_enabled),
            context_enrich_enabled=_env_or("RAG_CONTEXT_ENRICH_ENABLED", c.get("enrich", {}).get("enabled"), from_env.context_enrich_enabled),
            citation_enabled=_env_or("RAG_CITATION_ENABLED", cit.get("enabled"), from_env.citation_enabled),
            active_collections=list(active) if isinstance(active, list) else from_env.active_collections,
        )
