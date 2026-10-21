"""自主分诊指标统计（只读 case_eval 报告）。

口径见 evaluation/METRICS-20261017.md:
- 真值: benign = 可自主结案(dismiss)，其余三档 = 应转人工(escalate)
- 预测: 结论 benign 且 L1 自动放行 = dismiss，其余 = escalate
另给一套「结论口径」(只看 benign，不看 L1) 用于区分判据问题与动作配置问题。
比例统一给 Wilson 95% 区间；并按主体类型、难度、数据档位分层输出。

跑法: python -m eval.case_metrics eval/reports/r4-*.json
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

DISMISS_TRUTH = "benign"
Z95 = 1.959963984540054
TIER_MAP = {"完整": "full", "部分缺失": "partial", "严重缺失": "sparse"}


def _matrix(cases: list[dict], pred_dismiss) -> dict:
    m = {"tp": 0, "tn": 0, "fp": 0, "fn": 0}
    for c in cases:
        truth_dismiss = c["ground_truth"] == DISMISS_TRUTH
        got_dismiss = pred_dismiss(c)
        if truth_dismiss and got_dismiss:
            m["tn"] += 1
        elif truth_dismiss:
            m["fp"] += 1
        elif got_dismiss:
            m["fn"] += 1
        else:
            m["tp"] += 1
    return m


def wilson(k: int, n: int, z: float = Z95) -> dict:
    """Wilson 区间：小样本/极端比例下比正态近似稳定。n=0 返回空区间。"""
    if n <= 0:
        return {"point": 0.0, "low": 0.0, "high": 0.0, "n": 0}
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return {
        "point": round(p, 4),
        "low": round(max(0.0, centre - half), 4),
        "high": round(min(1.0, centre + half), 4),
        "n": n,
    }


def _rates(m: dict, n: int) -> dict:
    fp_tn = m["fp"] + m["tn"]
    tp_fn = m["tp"] + m["fn"]
    return {
        "autonomous_rate": round((m["tn"] + m["fn"]) / n, 4) if n else 0.0,
        "escalation_rate": round((m["tp"] + m["fp"]) / n, 4) if n else 0.0,
        "false_positive_rate": round(m["fp"] / fp_tn, 4) if fp_tn else 0.0,
        "missed_release": m["fn"],
        "recall": round(m["tp"] / tp_fn, 4) if tp_fn else 0.0,
    }


def _ci(m: dict, n: int) -> dict:
    return {
        "autonomous_rate": wilson(m["tn"] + m["fn"], n),
        "escalation_rate": wilson(m["tp"] + m["fp"], n),
        "false_positive_rate": wilson(m["fp"], m["fp"] + m["tn"]),
        "recall": wilson(m["tp"], m["tp"] + m["fn"]),
    }


def _compute(cases: list[dict]) -> dict:
    n = len(cases)
    exec_m = _matrix(cases, lambda c: c["predicted_conclusion"] == DISMISS_TRUTH and bool(c.get("l1_autopass")))
    concl_m = _matrix(cases, lambda c: c["predicted_conclusion"] == DISMISS_TRUTH)
    return {
        "total": n,
        "matrix": exec_m,  # 执行口径（官方指标）
        "conclusion_matrix": concl_m,  # 结论口径（诊断用）
        **_rates(exec_m, n),
        "ci": _ci(exec_m, n),
        "conclusion_only": {**_rates(concl_m, n), "ci": _ci(concl_m, n)},
    }


def compute_metrics(report_path: str) -> dict:
    data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    cases = [c for c in data.get("cases", []) if not c.get("error")]
    result = _compute(cases)
    result["strata"] = _compute_strata(cases)
    return result


def _group_key(c: dict, dim: str) -> str:
    cov = c.get("coverage") or {}
    if dim == "subject":
        return str(cov.get("subject_type") or "unknown")
    if dim == "difficulty":
        return str(c.get("difficulty") or "unknown")
    if dim == "tier":
        return TIER_MAP.get(str(cov.get("info_completeness") or ""), "unknown")
    return "unknown"


def _compute_strata(cases: list[dict]) -> dict:
    strata: dict[str, dict] = {}
    for dim in ("subject", "difficulty", "tier"):
        groups: dict[str, list[dict]] = {}
        for c in cases:
            groups.setdefault(_group_key(c, dim), []).append(c)
        strata[dim] = {k: _compute(v) for k, v in sorted(groups.items())}
    return strata


def _pct(r: dict) -> str:
    ci = r.get("ci") or {}
    lo, hi = ci.get("low", 0.0), ci.get("high", 0.0)
    return f"{r['point']:.2%} [{lo:.2%}, {hi:.2%}]"


def _render(name: str, r: dict) -> str:
    m, cm = r["matrix"], r["conclusion_matrix"]
    lines = [
        f"## {name}",
        "",
        f"样本 {r['total']} 条；执行口径矩阵: TP={m['tp']} TN={m['tn']} FP={m['fp']} FN={m['fn']}",
        "",
        "| 指标 | 执行口径 (95% Wilson) | 结论口径 |",
        "|---|---|---|",
        f"| 自主分诊率 | {_pct({'point': r['autonomous_rate'], 'ci': r['ci']['autonomous_rate']})} "
        f"| {r['conclusion_only']['autonomous_rate']:.2%} |",
        f"| 转人工率 | {_pct({'point': r['escalation_rate'], 'ci': r['ci']['escalation_rate']})} "
        f"| {r['conclusion_only']['escalation_rate']:.2%} |",
        f"| 误报率(良性被转人工) | {_pct({'point': r['false_positive_rate'], 'ci': r['ci']['false_positive_rate']})} "
        f"| {r['conclusion_only']['false_positive_rate']:.2%} |",
        f"| 漏放行数 FN | {r['missed_release']} | {r['conclusion_only']['missed_release']} |",
        f"| Recall(应转人工) | {_pct({'point': r['recall'], 'ci': r['ci']['recall']})} "
        f"| {r['conclusion_only']['recall']:.2%} |",
        "",
        f"结论口径矩阵: TP={cm['tp']} TN={cm['tn']} FP={cm['fp']} FN={cm['fn']}",
    ]
    labels = {"subject": "主体类型", "difficulty": "难度", "tier": "数据档位"}
    for dim, groups in (r.get("strata") or {}).items():
        lines += ["", f"### 分层：{labels.get(dim, dim)}", ""]
        lines += ["| 组 | 样本 | 自主分诊率 (95%CI) | 转人工率 | 良性误转率 | Recall |", "|---|---|---|---|---|---|"]
        for key, g in groups.items():
            lines.append(
                f"| {key} | {g['total']} "
                f"| {_pct({'point': g['autonomous_rate'], 'ci': g['ci']['autonomous_rate']})} "
                f"| {g['escalation_rate']:.2%} | {g['false_positive_rate']:.2%} | {g['recall']:.2%} |"
            )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("reports", nargs="+")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    for path in args.reports:
        r = compute_metrics(path)
        if args.json:
            print(json.dumps({"report": path, **r}, ensure_ascii=False))
        else:
            print(_render(Path(path).stem, r))
            print()


if __name__ == "__main__":
    main()
