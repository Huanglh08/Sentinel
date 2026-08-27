"""评测报告 Markdown 渲染器。"""
from __future__ import annotations

import time
from pathlib import Path

from eval.intent_eval import EvalSummary


def _bar(pct: float, width: int = 20) -> str:
    """简易文本进度条,纯 ASCII 避免编码问题。"""
    pct = max(0.0, min(1.0, pct))
    filled = int(round(pct * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def render_intent_report(
    summary: EvalSummary,
    eval_url: str,
    seed_path: str,
) -> str:
    """渲染意图评测报告为 Markdown 字符串。"""
    lines: list[str] = []
    lines.append("# Intent Recognition Evaluation Report")
    lines.append("")
    lines.append(f"- Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- Eval Endpoint: `{eval_url}`")
    lines.append(f"- Seed Dataset: `{seed_path}`")
    lines.append("")

    # ----- Summary -----
    lines.append("## 1. Summary")
    lines.append("")
    lines.append("| Metric | Value | Target | Status |")
    lines.append("|---|---|---|---|")
    top1_ok = "PASS" if summary.top1_accuracy >= 0.9 else "FAIL"
    lines.append(
        f"| Top-1 Accuracy | **{summary.top1_accuracy:.2%}** "
        f"| >= 90.00% | {top1_ok} {_bar(summary.top1_accuracy)} |"
    )
    top3_ok = "PASS" if summary.top3_accuracy >= 0.8 else "FAIL"
    lines.append(
        f"| Top-3 Accuracy | **{summary.top3_accuracy:.2%}** "
        f"| >= 80.00% | {top3_ok} {_bar(summary.top3_accuracy)} |"
    )
    lines.append(
        f"| Sub-Intent Rate | {summary.sub_intent_rate:.2%} | - | INFO |"
    )
    lines.append("")
    lines.append("### Latency (ms)")
    lines.append("")
    lines.append("| Percentile | Latency | Target |")
    lines.append("|---|---|---|")
    lines.append(f"| Avg | {summary.latency_avg:.1f} | - |")
    lines.append(
        f"| P50 | {summary.latency_p50:.1f} "
        f"| - |"
    )
    p90_ok = "PASS" if summary.latency_p90 <= 7000 else "FAIL"
    lines.append(
        f"| P90 | {summary.latency_p90:.1f} | <= 7000 | {p90_ok} |"
    )
    lines.append(
        f"| P95 | {summary.latency_p95:.1f} | - |"
    )
    lines.append("")
    lines.append(f"- Total cases: **{summary.total}**")
    lines.append(f"- Successful: **{summary.successful}**")
    lines.append(f"- Failed: **{summary.failed}**")
    lines.append("")

    # ----- Failed cases -----
    failed_cases = [c for c in summary.cases if c.error]
    if failed_cases:
        lines.append("## 2. Failed Cases (HTTP/Exception)")
        lines.append("")
        lines.append("| # | Question | Error |")
        lines.append("|---|---|---|")
        for i, c in enumerate(failed_cases, 1):
            q_esc = c.question.replace("|", "\\|")
            err_esc = (c.error or "")[:120].replace("|", "\\|")
            lines.append(f"| {i} | {q_esc} | {err_esc} |")
        lines.append("")

    # ----- Missed cases -----
    missed_top1 = [c for c in summary.cases if c.error is None and not c.hit_top1]
    lines.append("## 3. Missed Top-1 (需要人工 review)")
    lines.append("")
    if not missed_top1:
        lines.append("_全部命中 Top-1,无遗漏。_")
    else:
        lines.append(f"共 **{len(missed_top1)}** 条未命中 Top-1。")
        lines.append("")
        lines.append("| # | Question | Ground Truth | Predicted Top-3 | Hit@3 |")
        lines.append("|---|---|---|---|---|")
        for i, c in enumerate(missed_top1, 1):
            q_esc = c.question.replace("|", "\\|")
            gt_esc = c.ground_truth.replace("|", "\\|")
            top3_esc = " / ".join(c.predicted_top3) or "_(空)_"
            top3_esc = top3_esc.replace("|", "\\|")
            hit3 = "YES" if c.hit_top3 else "NO"
            lines.append(f"| {i} | {q_esc} | `{gt_esc}` | `{top3_esc}` | {hit3} |")
    lines.append("")

    # ----- Sample hits -----
    hit_cases = [c for c in summary.cases if c.error is None and c.hit_top1]
    if hit_cases:
        lines.append("## 4. Sample Hits (Top-1 正确,展示模型行为)")
        lines.append("")
        lines.append("| # | Question | Predicted Top-1 | Latency (ms) |")
        lines.append("|---|---|---|---|")
        sample = hit_cases[: min(10, len(hit_cases))]
        for i, c in enumerate(sample, 1):
            q_esc = c.question.replace("|", "\\|")
            p_esc = c.predicted_top1.replace("|", "\\|")
            lines.append(f"| {i} | {q_esc} | `{p_esc}` | {c.latency_ms:.0f} |")
        if len(hit_cases) > 10:
            lines.append(f"\n_(共 {len(hit_cases)} 条 Top-1 命中,仅展示前 10 条)_")
    lines.append("")

    # ----- Footer -----
    lines.append("---")
    lines.append("")
    lines.append("## How to reproduce")
    lines.append("")
    lines.append("```bash")
    lines.append("# 1. 重新生成 seed 集")
    lines.append("DASHSCOPE_API_KEY=xxx python -m eval.build_seed")
    lines.append("")
    lines.append("# 2. 跑意图评测")
    lines.append("python -m eval.intent_eval --eval-url http://localhost:9090/api/ragent/rag/eval")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)
