"""合并多打分者盲评分到 golden：human_* = 三人中位数，原始分存 human_scores_detail。

用法:
    python -m eval.merge_human_scores \
        --md "..\\evaluation\\reports\\scoring-pack-20260903-131149-scored.md" \
        --csv "D:\\EdgeDownload\\table.csv" \
        --csv2 "D:\\EdgeDownload\\Sentinel_盲评_分数汇总.csv"

同时输出打分者间一致性（两两 Pearson + |Δ|<=0.10 比例），评估打分者口径漂移。
"""
import argparse
import csv
import json
import re
import statistics
import sys
from pathlib import Path

GOLDEN_PATH = Path(__file__).resolve().parents[2] / "evaluation" / "seeds" / "judge-calibration.jsonl"
TOL = 0.10


def parse_md(path: Path) -> dict[str, tuple[float, float]]:
    """解析打分包 md 的 `- JCxx rel: 1.00` / `- JCxx faith: 0.75` 行。"""
    text = path.read_text(encoding="utf-8")
    scores: dict[str, dict[str, float]] = {}
    for m in re.finditer(r"^-\s+(JC\d+)\s+rel:\s*([\d.]+)\s*$", text, re.M):
        scores.setdefault(m.group(1), {})["rel"] = float(m.group(2))
    for m in re.finditer(r"^-\s+(JC\d+)\s+faith:\s*([\d.]+)\s*$", text, re.M):
        scores.setdefault(m.group(1), {})["faith"] = float(m.group(2))
    return {k: (v["rel"], v["faith"]) for k, v in scores.items() if "rel" in v and "faith" in v}


def parse_csv(path: Path, id_col: str) -> dict[str, tuple[float, float]]:
    scores = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            jid = (row.get(id_col) or "").strip()
            if not jid:
                continue
            scores[jid] = (float(row["rel"]), float(row["faith"]))
    return scores


def pearson(xs: list[float], ys: list[float]) -> float:
    return statistics.correlation(xs, ys)


def inter_rater(name_a: str, ra: dict, name_b: str, rb: dict, ids: list[str]) -> str:
    lines = []
    for idx, metric in ((0, "rel"), (1, "faith")):
        a = [ra[i][idx] for i in ids]
        b = [rb[i][idx] for i in ids]
        diffs = [abs(x - y) for x, y in zip(a, b)]
        agree = sum(d <= TOL for d in diffs)
        r = pearson(a, b)
        lines.append(
            f"  {metric}: pearson={r:.4f} agree(|d|<=0.10)={agree}/{len(ids)} "
            f"avg|d|={sum(diffs)/len(diffs):.3f} max|d|={max(diffs):.2f}"
        )
    return f"[{name_a} vs {name_b}]\n" + "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=str(GOLDEN_PATH))
    ap.add_argument("--md", required=True)
    ap.add_argument("--csv", required=True, help="打分者2 csv（题号,rel,faith,备注）")
    ap.add_argument("--csv2", required=True, help="打分者3 csv（ID,rel,faith,平均分,备注）")
    args = ap.parse_args()

    r1 = parse_md(Path(args.md))
    r2 = parse_csv(Path(args.csv), "题号")
    r3 = parse_csv(Path(args.csv2), "ID")

    golden = [json.loads(l) for l in Path(args.golden).read_text(encoding="utf-8").splitlines() if l.strip()]
    ids = [g["id"] for g in golden]

    # 完整性校验
    for name, r in (("md", r1), ("csv", r2), ("csv2", r3)):
        missing = [i for i in ids if i not in r]
        if missing:
            print(f"[merge] {name} missing {len(missing)}: {missing[:5]}", file=sys.stderr)
            return 1
    print(f"[merge] 3 raters x {len(ids)} items, all complete", file=sys.stderr)

    # 打分者间一致性
    print(inter_rater("md", r1, "csv", r2, ids), file=sys.stderr)
    print(inter_rater("md", r1, "csv2", r3, ids), file=sys.stderr)
    print(inter_rater("csv", r2, "csv2", r3, ids), file=sys.stderr)

    # 写回：human_* = 三人中位数；detail 存原始三人分
    for g in golden:
        jid = g["id"]
        rels = sorted([r1[jid][0], r2[jid][0], r3[jid][0]])
        faiths = sorted([r1[jid][1], r2[jid][1], r3[jid][1]])
        g["human_answer_relevancy"] = statistics.median(rels)
        g["human_faithfulness"] = statistics.median(faiths)
        g["human_scores_detail"] = {
            "md": {"rel": r1[jid][0], "faith": r1[jid][1]},
            "csv": {"rel": r2[jid][0], "faith": r2[jid][1]},
            "csv2": {"rel": r3[jid][0], "faith": r3[jid][1]},
        }
        g["human_note"] = "3-rater median (md/csv/csv2), AI-assisted blind scoring"

    Path(args.golden).write_text(
        "\n".join(json.dumps(g, ensure_ascii=False) for g in golden) + "\n", encoding="utf-8"
    )
    print(f"[merge] golden written: {len(golden)} items, human_* = median of 3", file=sys.stderr)

    # 自检：中位数写回后无缺失
    out = [json.loads(l) for l in Path(args.golden).read_text(encoding="utf-8").splitlines() if l.strip()]
    assert all(o["human_answer_relevancy"] is not None and o["human_faithfulness"] is not None for o in out)
    assert len(out) == len(ids)
    print("[merge] self-check passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
