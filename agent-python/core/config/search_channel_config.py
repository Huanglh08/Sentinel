"""1:1 翻译 ragent rag.config.SearchChannelProperties。

完整 1:1 翻译：default_top_k / recall_budget / scope / channels / fusion / channel_weights。
按 Java afterPropertiesSet() 实现严格单调不变式校验：
  - default_top_k > 0
  - recall_budget ≥ default_top_k（漏斗不变式：召回扇出 ≥ 最终条数）
  - candidate_limit ≥ default_top_k
  - scope.min_intent_score >= INTENT_MIN_SCORE
  - scope.confidence_threshold ∈ (min_intent_score, 1]
  - scope.supplement_ratio < 1
"""
from __future__ import annotations

from dataclasses import dataclass, field


_INTENT_MIN_SCORE = 0.3  # 1:1 翻译 ragent RAGConstant.INTENT_MIN_SCORE


@dataclass
class Scope:
    """1:1 翻译 ragent SearchChannelProperties.Scope。"""
    min_intent_score: float = 0.4
    confidence_threshold: float = 0.6
    supplement_ratio: float = 0.25


@dataclass
class VectorChannelConfig:
    """1:1 翻译 ragent SearchChannelProperties.Vector。"""
    enabled: bool = True


@dataclass
class KeywordChannelConfig:
    """1:1 翻译 ragent SearchChannelProperties.Keyword。"""
    enabled: bool = False


@dataclass
class GraphChannelConfig:
    """1:1 翻译 ragent SearchChannelProperties.Graph。"""
    enabled: bool = False


@dataclass
class WebSearchConfig:
    """1:1 翻译 ragent SearchChannelProperties.WebSearch。"""
    enabled: bool = False
    count: int = 5
    timeout_seconds: int = 10
    api_key: str = ""
    api_url: str = "https://ydc-index.io/v1/search"


@dataclass
class ChannelsConfig:
    """1:1 翻译 ragent SearchChannelProperties.Channels。"""
    timeout_ms: int = 15_000
    vector: VectorChannelConfig = field(default_factory=VectorChannelConfig)
    keyword: KeywordChannelConfig = field(default_factory=KeywordChannelConfig)
    graph: GraphChannelConfig = field(default_factory=GraphChannelConfig)
    web_search: WebSearchConfig = field(default_factory=WebSearchConfig)


@dataclass
class ChannelWeights:
    """1:1 翻译 ragent SearchChannelProperties.Fusion.ChannelWeights。"""
    vector: float = 1.0
    keyword: float = 1.0
    graph: float = 0.5
    web_search: float = 0.5
    default_weight: float = 1.0


@dataclass
class FusionConfig:
    """1:1 翻译 ragent SearchChannelProperties.Fusion。"""
    strategy: str = "rrf"
    rrf_k: int = 20
    rerank_candidate_limit: int = 40
    channel_weights: ChannelWeights = field(default_factory=ChannelWeights)


@dataclass
class SearchChannelProperties:
    """1:1 翻译 ragent SearchChannelProperties。

    漏斗不变式（启动时校验）：
      - default_top_k > 0
      - recall_budget ≥ default_top_k
      - rerank_candidate_limit ≥ default_top_k（配置了的话）
      - scope.min_intent_score >= _INTENT_MIN_SCORE
      - scope.confidence_threshold ∈ (scope.min_intent_score, 1]
      - scope.supplement_ratio < 1
    """
    default_top_k: int = 10
    recall_budget: int = 20
    scope: Scope = field(default_factory=Scope)
    channels: ChannelsConfig = field(default_factory=ChannelsConfig)
    fusion: FusionConfig = field(default_factory=FusionConfig)

    # ---- 1:1 翻译 ragent SearchChannelProperties.resolveRecallBudget ----
    def resolve_recall_budget(self, context_top_k: int) -> int:
        return self.recall_budget if self.recall_budget > 0 else context_top_k

    # ---- 1:1 翻译 ragent SearchChannelProperties.afterPropertiesSet ----
    def validate(self) -> None:
        """启动校验：违反漏斗不变式即抛 IllegalStateException（与 Java 完全一致）。"""
        context_top_k = self.default_top_k
        if context_top_k <= 0:
            raise ValueError(f"rag.search.default-top-k 必须为正数，当前：{context_top_k}")
        resolved_recall = self.resolve_recall_budget(context_top_k)
        if resolved_recall < context_top_k:
            raise ValueError(
                f"检索预算漏斗不变式被破坏：recallBudget({resolved_recall}) < contextTopK({context_top_k})，"
                "召回扇出不得小于最终条数，请调大 rag.search.recall-budget 或调小 rag.search.default-top-k"
            )
        candidate_limit = self.fusion.rerank_candidate_limit
        if candidate_limit > 0 and candidate_limit < context_top_k:
            raise ValueError(
                f"检索预算漏斗不变式被破坏：candidateLimit({candidate_limit}) < contextTopK({context_top_k})，"
                "送入 Rerank 的候选池不得小于最终条数，请调大 rag.search.fusion.rerank-candidate-limit 或调小 rag.search.default-top-k"
            )
        if self.scope.min_intent_score < _INTENT_MIN_SCORE:
            raise ValueError(
                f"rag.search.scope.min-intent-score({self.scope.min_intent_score}) 低于上游意图过滤下限 "
                f"INTENT_MIN_SCORE({_INTENT_MIN_SCORE})，该配置不会产生任何效果，请调高此值，或先下调 INTENT_MIN_SCORE"
            )
        confidence_threshold = self.scope.confidence_threshold
        if confidence_threshold <= self.scope.min_intent_score or confidence_threshold > 1:
            raise ValueError(
                f"rag.search.scope.confidence-threshold({confidence_threshold}) 必须落在 "
                f"(min-intent-score({self.scope.min_intent_score}), 1] 内："
                "不高于最低分则「低置信退化为全局」永不触发，大于 1 则「高置信收窄到命中库」永不触发，"
                "两者都会让一整条作用域分支连同补充路一起变成死代码"
            )
        supplement_ratio = self.scope.supplement_ratio
        if supplement_ratio != supplement_ratio or supplement_ratio >= 1:  # NaN or >= 1
            raise ValueError(
                f"rag.search.scope.supplement-ratio({supplement_ratio}) 必须小于 1："
                "该比例是从主路划给补充路的份额，取到 1 等于把高置信命中库的名额清零，"
                "与「定向优先、补充兜底」相反；关闭补充路请填 0"
            )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "SearchChannelProperties":
        """1:1 翻译 ragent @ConfigurationProperties(prefix = "rag.search") 注入机制。

        支持的环境变量（与 ragent yml key 严格对应）：
          RAG_SEARCH_DEFAULT_TOP_K
          RAG_SEARCH_RECALL_BUDGET
          RAG_SEARCH_SCOPE_MIN_INTENT_SCORE
          RAG_SEARCH_SCOPE_CONFIDENCE_THRESHOLD
          RAG_SEARCH_SCOPE_SUPPLEMENT_RATIO
          RAG_SEARCH_CHANNELS_TIMEOUT_MS
          RAG_SEARCH_CHANNELS_VECTOR_ENABLED
          RAG_SEARCH_CHANNELS_KEYWORD_ENABLED
          RAG_SEARCH_CHANNELS_GRAPH_ENABLED
          RAG_SEARCH_CHANNELS_WEB_SEARCH_ENABLED
          RAG_SEARCH_CHANNELS_WEB_SEARCH_API_KEY
          RAG_SEARCH_CHANNELS_WEB_SEARCH_API_URL
          RAG_SEARCH_FUSION_STRATEGY
          RAG_SEARCH_FUSION_RRF_K
          RAG_SEARCH_FUSION_RERANK_CANDIDATE_LIMIT
          RAG_SEARCH_FUSION_CHANNEL_WEIGHTS_VECTOR
          RAG_SEARCH_FUSION_CHANNEL_WEIGHTS_KEYWORD
          RAG_SEARCH_FUSION_CHANNEL_WEIGHTS_GRAPH
          RAG_SEARCH_FUSION_CHANNEL_WEIGHTS_WEB_SEARCH
          RAG_SEARCH_FUSION_CHANNEL_WEIGHTS_DEFAULT
        """
        import os
        env = env if env is not None else dict(os.environ)
        def _bool(name: str, default: bool) -> bool:
            raw = env.get(name, "").strip().lower()
            if not raw:
                return default
            return raw in ("1", "true", "yes", "on")
        def _int(name: str, default: int) -> int:
            raw = env.get(name, "").strip()
            return int(raw) if raw else default
        def _float(name: str, default: float) -> float:
            raw = env.get(name, "").strip()
            return float(raw) if raw else default
        return cls(
            default_top_k=_int("RAG_SEARCH_DEFAULT_TOP_K", 10),
            recall_budget=_int("RAG_SEARCH_RECALL_BUDGET", 20),
            scope=Scope(
                min_intent_score=_float("RAG_SEARCH_SCOPE_MIN_INTENT_SCORE", 0.4),
                confidence_threshold=_float("RAG_SEARCH_SCOPE_CONFIDENCE_THRESHOLD", 0.6),
                supplement_ratio=_float("RAG_SEARCH_SCOPE_SUPPLEMENT_RATIO", 0.25),
            ),
            channels=ChannelsConfig(
                timeout_ms=_int("RAG_SEARCH_CHANNELS_TIMEOUT_MS", 15_000),
                vector=VectorChannelConfig(enabled=_bool("RAG_SEARCH_CHANNELS_VECTOR_ENABLED", True)),
                keyword=KeywordChannelConfig(enabled=_bool("RAG_SEARCH_CHANNELS_KEYWORD_ENABLED", False)),
                graph=GraphChannelConfig(enabled=_bool("RAG_SEARCH_CHANNELS_GRAPH_ENABLED", False)),
                web_search=WebSearchConfig(
                    enabled=_bool("RAG_SEARCH_CHANNELS_WEB_SEARCH_ENABLED", False),
                    api_key=env.get("RAG_SEARCH_CHANNELS_WEB_SEARCH_API_KEY", "") or env.get("YDC_API_KEY", ""),
                    api_url=env.get("RAG_SEARCH_CHANNELS_WEB_SEARCH_API_URL", "https://ydc-index.io/v1/search"),
                ),
            ),
            fusion=FusionConfig(
                strategy=env.get("RAG_SEARCH_FUSION_STRATEGY", "rrf"),
                rrf_k=_int("RAG_SEARCH_FUSION_RRF_K", 20),
                rerank_candidate_limit=_int("RAG_SEARCH_FUSION_RERANK_CANDIDATE_LIMIT", 40),
                channel_weights=ChannelWeights(
                    vector=_float("RAG_SEARCH_FUSION_CHANNEL_WEIGHTS_VECTOR", 1.0),
                    keyword=_float("RAG_SEARCH_FUSION_CHANNEL_WEIGHTS_KEYWORD", 1.0),
                    graph=_float("RAG_SEARCH_FUSION_CHANNEL_WEIGHTS_GRAPH", 0.5),
                    web_search=_float("RAG_SEARCH_FUSION_CHANNEL_WEIGHTS_WEB_SEARCH", 0.5),
                    default_weight=_float("RAG_SEARCH_FUSION_CHANNEL_WEIGHTS_DEFAULT", 1.0),
                ),
            ),
        )
