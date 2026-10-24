"""AIModelProperties 配置：严格对齐 ragent infra-ai.infra.config.AIModelProperties。

完整复现 ragent 配置层级：
  - AIModelProperties 顶层：4 个 ModelGroup + Map<ProviderConfig> providers
  - ModelGroup：含 candidates + (defaultModel | defaultTier + deepThinkingTier + Map<TierConfig> tiers)
  - ModelCandidate：含 id/provider/model/enabled/priority/supportsThinking 等
  - ProviderConfig：含 baseUrl/apiKey
  - TierConfig：含 candidates (有序 list) + timeoutMs
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.llm.enums import ModelProvider


@dataclass
class ProviderConfig:
    """严格对应 ragent AIModelProperties.ProviderConfig。"""

    base_url: str = ""
    api_key: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelCandidate:
    """严格对应 ragent AIModelProperties.ModelCandidate。"""

    id: str
    provider: ModelProvider
    model: str = ""
    name: str = ""
    enabled: bool = True
    priority: int = 0
    supports_thinking: bool = False
    # 可选 provider 特定配置（覆盖 ProviderConfig）
    base_url: str | None = None
    api_key: str | None = None
    # 嵌入/重排特定
    dimension: int | None = None
    # 生成参数
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    # 追加到 chat/completions 请求体的候选级参数（如百炼 qwen3 系的 enable_thinking）
    extra_body: dict[str, Any] | None = None


@dataclass
class TierConfig:
    """严格对应 ragent AIModelProperties.TierConfig。"""

    candidates: list[str] = field(default_factory=list)
    timeout_ms: int | None = None


@dataclass
class ModelGroup:
    """严格对应 ragent AIModelProperties.ModelGroup。"""

    candidates: list[ModelCandidate] = field(default_factory=list)
    # embedding/rerank/vlm 用
    default_model: str = ""
    # chat 用
    default_tier: str = "standard"
    deep_thinking_tier: str = ""
    tiers: dict[str, TierConfig] = field(default_factory=dict)


@dataclass
class AIModelProperties:
    """严格对应 ragent AIModelProperties 顶层。"""

    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    chat: ModelGroup | None = None
    embedding: ModelGroup | None = None
    rerank: ModelGroup | None = None
    vlm: ModelGroup | None = None

    @classmethod
    def default_bailian(cls) -> "AIModelProperties":
        """ragent 默认配置，DashScope OpenAI 兼容接口 + 多候选降级链。

        候选顺序 = tiers["standard"].candidates 顺序（ModelSelector 按序展开，
        ModelRoutingExecutor 逐个尝试，失败换下一个，全部失败才抛 LLMError）：
          1. siliconflow Qwen3-30B-A3B-Instruct-2507 首选（MoE 激活 3B，改写质量实测最优）
          2. siliconflow Qwen2.5-72B-Instruct 质量兜底（贵，仅首选挂时使用）
          3. bailian qwen-plus    跨 provider 备胎（DashScope RPM 按 model 维度计）
          4. bailian qwen-turbo   末位备选（2026-10-14 起持续 429 限流，探针实锤，
             曾因占链首每 5min sticky 过期白撞一次 429；恢复后也仅作末位）
        """
        import os

        candidates = [
            ModelCandidate(
                id="qwen3-30b-a3b-sf",
                provider=ModelProvider.SILICON_FLOW,
                model="Qwen/Qwen3-30B-A3B-Instruct-2507",
                supports_thinking=False,
                priority=0,
            ),
            ModelCandidate(
                id="qwen25-72b-sf",
                provider=ModelProvider.SILICON_FLOW,
                model="Qwen/Qwen2.5-72B-Instruct",
                supports_thinking=False,
                priority=1,
            ),
            ModelCandidate(
                id="qwen-plus",
                provider=ModelProvider.BAI_LIAN,
                model="qwen-plus",
                supports_thinking=False,
                priority=2,
            ),
            ModelCandidate(
                id="qwen-turbo",
                provider=ModelProvider.BAI_LIAN,
                model="qwen-turbo",
                supports_thinking=False,
                priority=3,
            ),
        ]
        # B' 收敛：AGENT_CHAT_MODEL 设置时 chat 链收敛为显式候选序（逗号分隔，主在前）。
        # 模型切换视为部署变更（改 env + 重启），不做运行时静默扩池；
        # 主失败按既有粘性路由落备用，属显式声明的容灾序而非漂移。
        override_model = os.getenv("AGENT_CHAT_MODEL", "").strip()
        if override_model:
            override_models = [m.strip() for m in override_model.split(",") if m.strip()]
            candidates = [
                ModelCandidate(
                    id="chat-primary" if i == 0 else f"chat-standby-{i}",
                    provider=ModelProvider.BAI_LIAN,
                    model=m,
                    supports_thinking=False,
                    # 百炼 qwen3 系默认输出思考内容，会耗尽输出限额导致正文为空；
                    # 实测对 qwen-plus/flash 等非思考模型无害（参数被忽略）。
                    # seed 固定随机种子：百炼官方口径“尽力确定性采样，尽可能返回
                    # 相同结果”，非完全一致承诺（2026-10-22 文档调研）；随 80 题
                    # 评测轮间对比顺便验证其收敛效果，无效即撤
                    extra_body={"enable_thinking": False, "seed": 20261022},
                )
                for i, m in enumerate(override_models)
            ]
        return cls(
            providers={
                "bailian": ProviderConfig(
                    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    api_key=os.getenv("BAILIAN_API_KEY")
                    or os.getenv("DASHSCOPE_API_KEY")
                    or "",
                ),
                "siliconflow": ProviderConfig(
                    base_url="https://api.siliconflow.cn/v1",
                    api_key=os.getenv("SILICONFLOW_API_KEY") or "",
                ),
            },
            chat=ModelGroup(
                candidates=candidates,
                default_tier="standard",
                tiers={
                    "standard": TierConfig(
                        candidates=[c.id for c in candidates],
                        timeout_ms=60000,
                    ),
                },
            ),
        )

    @classmethod
    def default_ollama(cls) -> "AIModelProperties":
        """兼容旧开发配置；LLM 服务默认不再使用此配置。"""
        return cls(
            providers={"ollama": ProviderConfig(base_url="http://localhost:11434/v1", api_key="ollama")},
            chat=ModelGroup(
                candidates=[ModelCandidate(id="qwen2.5:7b", provider=ModelProvider.OLLAMA, model="qwen2.5:7b")],
                default_tier="standard",
                tiers={"standard": TierConfig(candidates=["qwen2.5:7b"], timeout_ms=60000)},
            ),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AIModelProperties":
        """从 dict 解析（对应 ragent @ConfigurationProperties 绑定）。"""
        from core.llm.enums import ModelProvider as MP

        def _pick(d: dict, *keys: str, default: Any = "") -> Any:
            for key in keys:
                if key in d:
                    return d[key]
            return default

        def _provider(d: dict) -> ProviderConfig:
            return ProviderConfig(
                base_url=_pick(d, "base-url", "baseUrl", default=""),
                api_key=_pick(d, "api-key", "apiKey", default=""),
                extra={k: v for k, v in d.items() if k not in ("base-url", "baseUrl", "api-key", "apiKey")},
            )

        def _candidate(d: dict) -> ModelCandidate:
            return ModelCandidate(
                id=d["id"],
                provider=MP(d["provider"]),
                model=d.get("model", ""),
                name=d.get("name", ""),
                enabled=_pick(d, "enabled", default=True),
                priority=_pick(d, "priority", default=0),
                supports_thinking=_pick(d, "supports-thinking", "supportsThinking", default=False),
                base_url=_pick(d, "base-url", "baseUrl", default=None),
                api_key=_pick(d, "api-key", "apiKey", default=None),
                dimension=_pick(d, "dimension", default=None),
                temperature=_pick(d, "temperature", default=None),
                top_p=_pick(d, "top-p", "topP", default=None),
                max_tokens=_pick(d, "max-tokens", "maxTokens", default=None),
            )

        def _tier(d: dict) -> TierConfig:
            return TierConfig(
                candidates=list(d.get("candidates", [])),
                timeout_ms=d.get("timeout-ms"),
            )

        def _group(d: dict) -> ModelGroup:
            return ModelGroup(
                candidates=[_candidate(c) for c in d.get("candidates", [])],
                default_model=d.get("default-model", ""),
                default_tier=d.get("default-tier", "standard"),
                deep_thinking_tier=d.get("deep-thinking-tier", ""),
                tiers={k: _tier(v) for k, v in (d.get("tiers") or {}).items()},
            )

        return cls(
            providers={k: _provider(v) for k, v in (data.get("providers") or {}).items()},
            chat=_group(data["chat"]) if data.get("chat") else None,
            embedding=_group(data["embedding"]) if data.get("embedding") else None,
            rerank=_group(data["rerank"]) if data.get("rerank") else None,
            vlm=_group(data["vlm"]) if data.get("vlm") else None,
        )
