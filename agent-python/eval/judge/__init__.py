"""LLM-as-a-Judge 评测模块（v4）。

用途：为核心指标 #4（Faithfulness）与诊断指标（引用精确率 / 句级引用覆盖率 /
Answer Relevancy）提供 LLM 判分，并用拒答判定修正确定性拒答特征的边界误差。

默认模型：deepseek-v4-flash（阿里云百炼 OpenAI 兼容端点，复用 BAILIAN_API_KEY）。
选型依据见 config.py 顶部说明与 evaluation/README.md「Judge 选型」章节。

启用前置条件（README 门禁）：
    judge 校准集 20 条，judge 判分与人工标注一致率 ≥ 90%，否则 judge 类指标不得计入报告。

用法：
    from eval.judge import JudgeClient, JUDGE_TASKS, build_user_prompt, normalize_result

    client = JudgeClient()
    data = client.run(
        task="faithfulness",
        system=JUDGE_TASKS["faithfulness"]["system"],
        user=build_user_prompt("faithfulness", question=q, answer=a, contexts=ctx),
    )
    result = normalize_result("faithfulness", data)
"""
from __future__ import annotations

from eval.judge.client import JudgeClient, JudgeUsage
from eval.judge.config import ARBITER_MODEL, FALLBACK_CANDIDATES, JudgeConfig
from eval.judge.rubrics import (
    JUDGE_TASKS,
    SYSTEM_ANSWER_RELEVANCY,
    SYSTEM_CITATION_COVERAGE,
    SYSTEM_CITATION_PRECISION,
    SYSTEM_FAITHFULNESS,
    SYSTEM_REFUSAL,
    build_user_prompt,
    normalize_result,
)

__all__ = [
    "JudgeClient",
    "JudgeUsage",
    "JudgeConfig",
    "ARBITER_MODEL",
    "FALLBACK_CANDIDATES",
    "JUDGE_TASKS",
    "SYSTEM_FAITHFULNESS",
    "SYSTEM_CITATION_PRECISION",
    "SYSTEM_CITATION_COVERAGE",
    "SYSTEM_ANSWER_RELEVANCY",
    "SYSTEM_REFUSAL",
    "build_user_prompt",
    "normalize_result",
]
