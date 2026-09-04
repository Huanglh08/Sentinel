"""Judge 模型配置。

选型结论（2026-08-31 定稿，依据见 evaluation/README.md「Judge 选型」章节）：

    主 judge  = deepseek-v4-flash（阿里云百炼 OpenAI 兼容端点）
    仲裁 judge = deepseek-v4-pro（同渠道，仅用于校准分歧/低置信样本二次裁定）

选型依据（准确 + 性价比）：
1. 非 qwen 族：被测 agent 走通义千问（qwen-turbo/qwen-plus），judge 必须不同家族，
   否则"自恋偏差"（self-enhancement bias）会系统性抬高 Faithfulness 分数。
   README 明写"judge 模型须与被测模型不同家族"。
2. 成本极低：deepseek-v4-flash 原价 ¥1/¥2 每百万 token（输入/输出），
   上下文硬盘缓存命中后输入降至 ¥0.02/百万（降幅 98%）。
   judge 的 system rubric 是固定前缀 → 命中率极高（见 client.py 的缓存友好设计）。
3. 单条 judge 成本实测估算：输入 ~2K token + 输出 ~400 token ≈ ¥0.003（不到 1 分钱），
   1000 次全量 judge ≈ 3 元量级。
4. 能力够用：DeepSeek V4 系列中文强、指令遵循好，官方支持结构化输出（JSON）
   与 temperature 设置，judge 场景不需要 1M 上下文与旗舰推理。
5. 零新增账号：百炼已上架 DeepSeek V4 全系，复用现有 BAILIAN_API_KEY / DASHSCOPE_API_KEY，
   与项目现有 provider（bailian）同一渠道，账单统一。

关键参数：
- temperature = 0：judge 必须确定性输出（README 硬要求）。
- enable_thinking = false：V4 系列默认开思考模式，思维链按输出 token 计费且拖慢延迟，
  judge 是短判定任务，关掉思考 = 省钱 + 提速 + 降低评分漂移。
- response_format = json_object：官方结构化输出，杜绝解析失败。

风险与应对：
- DeepSeek 官方已预告"整体上调定价，预计涨幅较大"：成本模型不依赖单一渠道，
  config 支持一行切换 JUDGE_MODEL / JUDGE_BASE_URL；若涨价超预期，
  可切 GLM-4.7-Flash（智谱长期免费档）或 deepseek-v4-flash 其他渠道。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class JudgeConfig:
    """Judge 模型连接参数（全部可用环境变量覆盖）。"""

    # 模型
    model: str = "deepseek-v4-flash"
    # 百炼 OpenAI 兼容端点（可换 workspace 形态：https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1）
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str = ""

    # 判定参数（judge 必须确定性）
    temperature: float = 0.0
    max_tokens: int = 2048
    # 关闭思考模式：省输出 token + 降延迟 + 减少评分漂移
    enable_thinking: bool = False

    # 可靠性
    timeout: float = 90.0
    max_retries: int = 3
    retry_backoff: float = 1.5

    # 观测
    trace: bool = True

    @classmethod
    def from_env(cls) -> "JudgeConfig":
        key = (
            os.getenv("JUDGE_API_KEY")
            or os.getenv("BAILIAN_API_KEY")
            or os.getenv("DASHSCOPE_API_KEY")
            or ""
        )
        return cls(
            model=os.getenv("JUDGE_MODEL", "deepseek-v4-flash"),
            base_url=os.getenv(
                "JUDGE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
            ),
            api_key=key,
            temperature=float(os.getenv("JUDGE_TEMPERATURE", "0")),
            max_tokens=int(os.getenv("JUDGE_MAX_TOKENS", "2048")),
            enable_thinking=os.getenv("JUDGE_ENABLE_THINKING", "false").lower() == "true",
            timeout=float(os.getenv("JUDGE_TIMEOUT", "90")),
            max_retries=int(os.getenv("JUDGE_MAX_RETRIES", "3")),
            trace=os.getenv("JUDGE_TRACE", "true").lower() == "true",
        )

    def masked(self) -> dict:
        """脱敏输出，便于日志/报告展示。"""
        return {
            "model": self.model,
            "base_url": self.base_url,
            "api_key": ("set" if self.api_key else "MISSING"),
            "temperature": self.temperature,
            "enable_thinking": self.enable_thinking,
            "max_tokens": self.max_tokens,
        }


# 仲裁档：仅用于 judge 校准阶段的分歧样本二次裁定（成本约 3 倍于 flash）
ARBITER_MODEL = "deepseek-v4-pro"

# 已知可切换的降级候选（涨价/限流时改 JUDGE_MODEL + JUDGE_BASE_URL 即可）
FALLBACK_CANDIDATES: list[dict[str, str]] = [
    {
        "model": "deepseek-v4-flash",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "note": "主 judge（默认）：百炼渠道，复用 BAILIAN_API_KEY",
    },
    {
        "model": "deepseek-v4-pro",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "note": "仲裁档：校准分歧/低置信样本二次裁定，¥3/¥6 每百万",
    },
    {
        "model": "glm-4.7-flash",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "note": "零成本降级档：智谱长期免费（输入/输出/缓存全免，200K 上下文），需 ZHIPU_API_KEY",
    },
]
