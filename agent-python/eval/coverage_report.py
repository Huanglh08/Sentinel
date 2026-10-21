"""评测集覆盖率报告（coverage closure 门禁）。

用法：
    cd agent-python
    python -m eval.coverage_report                       # 默认读 evaluation/seeds/case-seeds.jsonl
    python -m eval.coverage_report --seeds <path>        # 指定种子集
    python -m eval.coverage_report --strict              # 覆盖不达标则退出码 1（门禁用）
    python -m eval.coverage_report --json out.json       # 同时输出机器可读结果
    python -m eval.coverage_report --top 15              # 展示前 15 个缺口

设计对齐芯片验证的 coverage report：
- **严格覆盖**（covered）= 命中数 ≥ 每 bin 目标数，用于签收门禁
- **宽松覆盖**（touched）= 至少有 1 条样本，用于看推进进度
两个口径都报，避免「有 1 条就算覆盖」自欺。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from eval.coverage import (
    BOUNDARIES,
    COVERPOINTS,
    CROSSES,
    CoverageTag,
    compute_boundaries,
    compute_cross,
    compute_values,
    untagged_rows,
)
from eval.seedgen.generator import load_jsonl

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SEEDS = _REPO_ROOT / "evaluation" / "seeds" / "case-seeds.jsonl"
DEFAULT_OUT_DIR = _REPO_ROOT / "evaluation" / "reports"


# ===========================================================================
# 计算
# ===========================================================================


def build_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """算全部覆盖指标。"""
    tagged = [r for r in rows if CoverageTag.from_row(r) is not None]
    values = compute_values(rows)
    cross_results = {c.key: compute_cross(c, rows) for c in CROSSES}
    boundaries = compute_boundaries(rows)

    cp_cov = {}
    for cp in COVERPOINTS:
        vals = values[cp.key]
        hit_values = sum(1 for v in cp.values if vals.get(v, 0) > 0)
        cp_cov[cp.key] = {
            "name": cp.name,
            "priority": cp.priority,
            "source": cp.source,
            "values": vals,
            "hit_values": hit_values,
            "total_values": len(cp.values),
            "rate": hit_values / len(cp.values) if cp.values else 0.0,
        }

    cross_cov = {}
    for key, res in cross_results.items():
        cross_cov[key] = {
            "left": res.cross.left,
            "right": res.cross.right,
            "priority": res.cross.priority,
            "per_bin": res.cross.per_bin,
            "rationale": res.cross.rationale,
            "total_bins": res.total_bins,
            "covered_bins": res.covered_bins,
            "touched_bins": res.touched_bins,
            "rate": res.rate,
            "touched_rate": res.touched_bins / res.total_bins if res.total_bins else 0.0,
            "gaps": [
                {"bin": list(b), "have": h, "need": t} for b, h, t in res.gaps()[:30]
            ],
        }

    bd_cov = {}
    for b in BOUNDARIES:
        got = boundaries.get(b.key, set())
        valid = {p for p in got if p}
        bd_cov[b.key] = {
            "metric": b.metric,
            "threshold": b.threshold,
            "source": b.source,
            "points": list(b.points),
            "covered_points": sorted(valid),
            "hit_points": len(valid),
            "total_points": len(b.points),
            "rate": len(valid) / len(b.points) if b.points else 0.0,
        }

    # 总覆盖率：交叉 bin 的严格覆盖（主口径，因为它才是能力区分度的来源）
    total_bins = sum(c["total_bins"] for c in cross_cov.values())
    covered_bins = sum(c["covered_bins"] for c in cross_cov.values())
    touched_bins = sum(c["touched_bins"] for c in cross_cov.values())

    return {
        "rows": len(rows),
        "tagged_rows": len(tagged),
        "untagged_rows": untagged_rows(rows),
        "conclusion_dist": dict(Counter(str(r.get("expect_conclusion", "?")) for r in rows)),
        "coverpoints": cp_cov,
        "crosses": cross_cov,
        "boundaries": bd_cov,
        "summary": {
            "total_bins": total_bins,
            "covered_bins": covered_bins,
            "touched_bins": touched_bins,
            "strict_rate": covered_bins / total_bins if total_bins else 0.0,
            "touched_rate": touched_bins / total_bins if total_bins else 0.0,
        },
    }


# ===========================================================================
# 渲染
# ===========================================================================


def render_md(rep: dict[str, Any], top: int = 12) -> str:
    s = rep["summary"]
    L: list[str] = []
    L.append("# 评测集覆盖率报告（case-seeds）\n")
    L.append(
        f"- 样本总数：**{rep['rows']}**（已标注覆盖标签 {rep['tagged_rows']}，"
        f"未标注 {rep['untagged_rows']}）"
    )
    L.append(f"- 结论分布：`{rep['conclusion_dist']}`")
    L.append(
        f"- **交叉 bin 严格覆盖：{s['covered_bins']}/{s['total_bins']} "
        f"= {s['strict_rate']:.1%}**"
    )
    L.append(
        f"- 交叉 bin 宽松覆盖（≥1 条样本）：{s['touched_bins']}/{s['total_bins']} "
        f"= {s['touched_rate']:.1%}\n"
    )
    L.append("> 严格覆盖 = 该 bin 样本数达到目标条数；宽松覆盖 = 至少 1 条。\n")

    L.append("## 一、维度覆盖（Coverpoint）\n")
    L.append("| 维度 | 优先级 | 覆盖值 | 覆盖率 | 依据 |")
    L.append("|---|---|---|---|---|")
    for key, c in rep["coverpoints"].items():
        if c["priority"] == "P0":
            flag = "[OK]" if c["rate"] >= 1.0 else ("[PART]" if c["rate"] > 0 else "[MISS]")
        else:
            flag = "[OK]" if c["rate"] >= 1.0 else "[--]"
        L.append(
            f"| {flag} {c['name']} (`{key}`) | {c['priority']} | "
            f"{c['hit_values']}/{c['total_values']} | {c['rate']:.0%} | {c['source']} |"
        )
    L.append("")

    L.append("## 二、交叉覆盖（Cross Coverage）\n")
    L.append("| 交叉 | 优先级 | 每bin目标 | 严格覆盖 | 宽松覆盖 | 防什么失效 |")
    L.append("|---|---|---|---|---|---|")
    for key, c in rep["crosses"].items():
        L.append(
            f"| `{key}` | {c['priority']} | {c['per_bin']} | "
            f"{c['covered_bins']}/{c['total_bins']} ({c['rate']:.0%}) | "
            f"{c['touched_bins']}/{c['total_bins']} ({c['touched_rate']:.0%}) | "
            f"{c['rationale']} |"
        )
    L.append("")

    L.append("## 三、未达标 bin（缺口 Top %d）\n" % top)
    n = 0
    for key, c in rep["crosses"].items():
        if not c["gaps"]:
            continue
        L.append(f"**`{key}`**（{c['left']} × {c['right']}）\n")
        L.append("| bin | 现有 | 目标 | 缺 |")
        L.append("|---|---|---|---|")
        for g in c["gaps"][:top]:
            a, b = g["bin"]
            L.append(f"| {a} × {b} | {g['have']} | {g['need']} | {g['need'] - g['have']} |")
        L.append("")
        n += 1
        if n >= 3:
            break
    if n == 0:
        L.append("（无缺口）\n")

    L.append("## 四、阈值边界覆盖（Boundary）\n")
    L.append("| 阈值 | 指标 | 取值 | 采样点覆盖 | 依据 |")
    L.append("|---|---|---|---|---|")
    for key, b in rep["boundaries"].items():
        flag = "[OK]" if b["rate"] >= 1.0 else ("[PART]" if b["hit_points"] > 0 else "[MISS]")
        L.append(
            f"| {flag} `{key}` | {b['metric']} | {b['threshold']} | "
            f"{b['hit_points']}/{b['total_points']} | {b['source']} |"
        )
    L.append("")
    return "\n".join(L)


# ===========================================================================
# CLI
# ===========================================================================


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，报告含表格符号与中文，统一转 UTF-8 避免 UnicodeEncodeError
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser(description="评测集覆盖率报告")
    ap.add_argument("--seeds", type=Path, default=DEFAULT_SEEDS)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--md", type=Path, default=None)
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--strict", action="store_true", help="未达标则退出码 1")
    ap.add_argument(
        "--threshold", type=float, default=0.95, help="strict 模式下的最低严格覆盖率"
    )
    args = ap.parse_args(argv)

    if not args.seeds.exists():
        print(f"[coverage] 种子集不存在: {args.seeds}", file=sys.stderr)
        print("[coverage] 先跑 seedgen 生成 case-seeds，或用 --seeds 指定路径", file=sys.stderr)
        return 2

    rows = load_jsonl(args.seeds)
    rep = build_report(rows)
    md = render_md(rep, top=args.top)
    print(md)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n[coverage] JSON 已写入 {args.json}")
    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(md, encoding="utf-8")
        print(f"[coverage] Markdown 已写入 {args.md}")

    s = rep["summary"]
    if args.strict and s["strict_rate"] < args.threshold:
        print(
            f"\n[coverage] 门禁未通过：严格覆盖率 {s['strict_rate']:.1%} < "
            f"{args.threshold:.0%}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
