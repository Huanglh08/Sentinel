# -*- coding: utf-8 -*-
"""Step 2 试点统计：三盲新分的一致性与新天花板（只读，不写回 golden）。

输入 = 30 条试点打分三源（打分包 md + csv + csv2）+ pilot 子集 jsonl（含旧 golden 分）。
输出：
  - 三对评分员 × rel/faith：band κ（界 0.5/0.85）、Kendall τ-b、±0.10 一致率
  - 各评分员及三盲新中位数 vs 旧 golden：band κ / τ-b
  - 新天花板 = 三对人-人 band κ 均值（分维度），及 0.9×天花板门禁预览

用法（agent-python 目录下，试点标注交回后）：
  python -m eval.pilot_stats --md <scored.md> --csv <r2.csv> --csv2 <r3.csv>
"""
import argparse
import json
import statistics
from collections import Counter
from pathlib import Path

from eval.merge_human_scores import parse_csv, parse_md

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PILOT = REPO_ROOT / "evaluation" / "seeds" / "judge-calibration-pilot30.jsonl"
TOL = 0.10
BANDS = range(3)


def band(x):
    return 0 if x < 0.5 else (1 if x <= 0.85 else 2)


def kappa_bands(a, b):
    n = len(a)
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[i] * cb[i] for i in BANDS) / n / n
    return (po - pe) / (1 - pe) if pe < 1 else 1.0


def tau_b(x, y):
    """Kendall τ-b，手写避免新依赖。"""
    n = len(x)
    nc = nd = 0
    for i in range(n):
        for j in range(i + 1, n):
            sx = (x[i] > x[j]) - (x[i] < x[j])
            sy = (y[i] > y[j]) - (y[i] < y[j])
            nc += sx * sy > 0
            nd += sx * sy < 0
    n0 = n * (n - 1) / 2
    tx = sum(1 for i in range(n) for j in range(i + 1, n) if x[i] == x[j])
    ty = sum(1 for i in range(n) for j in range(i + 1, n) if y[i] == y[j])
    denom = ((n0 - tx) * (n0 - ty)) ** 0.5
    return (nc - nd) / denom if denom else 0.0


def report(name_a, va, name_b, vb, idx, metric):
    a = [v[idx] for v in va]
    b = [v[idx] for v in vb]
    diffs = [abs(p - q) for p, q in zip(a, b)]
    agree = sum(d <= TOL for d in diffs)
    k = kappa_bands([band(x) for x in a], [band(x) for x in b])
    print(f"  [{name_a} vs {name_b}] {metric}: band_kappa={k:.4f} tau_b={tau_b(a, b):.4f} "
          f"agree(|d|<={TOL})={agree}/{len(a)} avg|d|={sum(diffs)/len(diffs):.3f}")
    return k


def selftest():
    """手算期望值断言：κ 与 τ-b 各两例（含 tie）。"""
    # band κ: a=[0,1,2] b=[0,1,1] -> po=2/3, pe=1/3, kappa=0.5
    assert abs(kappa_bands([0, 1, 2], [0, 1, 1]) - 0.5) < 1e-9
    # band κ 完全一致 -> 1.0
    assert abs(kappa_bands([2, 2, 1], [2, 2, 1]) - 1.0) < 1e-9
    # τ-b 无 tie: x=[1,2,3,4] y=[1,3,2,4] -> nc=5 nd=1 -> 4/6（与 scipy kendalltau 一致）
    assert abs(tau_b([1, 2, 3, 4], [1, 3, 2, 4]) - 2 / 3) < 1e-9
    # τ-b 完全逆序 -> -1
    assert abs(tau_b([1, 2], [2, 1]) - (-1.0)) < 1e-9
    # τ-b 带 tie: x=[1,1,2] y=[1,2,2] -> nc=1 nd=0, denom=sqrt(2*2)=2 -> 0.5
    assert abs(tau_b([1, 1, 2], [1, 2, 2]) - 0.5) < 1e-9
    # 档界：0.85=mid, 0.86=high
    assert band(0.85) == 1 and band(0.86) == 2 and band(0.49) == 0
    print("[selftest] kappa/tau-b expected values OK")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="只跑统计函数自检")
    ap.add_argument("--pilot", default=str(DEFAULT_PILOT))
    ap.add_argument("--md")
    ap.add_argument("--csv", help="打分者2 csv（题号,rel,faith,备注）")
    ap.add_argument("--csv2", help="打分者3 csv（ID,rel,faith,平均分,备注）")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return 0
    if not (args.md and args.csv and args.csv2):
        ap.error("--md --csv --csv2 are required (unless --selftest)")

    pilot = [json.loads(l) for l in Path(args.pilot).read_text(encoding="utf-8").splitlines() if l.strip()]
    ids = [g["id"] for g in pilot]
    r1, r2, r3 = parse_md(Path(args.md)), parse_csv(Path(args.csv), "题号"), parse_csv(Path(args.csv2), "ID")
    for name, r in (("md", r1), ("csv", r2), ("csv2", r3)):
        missing = [i for i in ids if i not in r]
        assert not missing, f"{name} missing {len(missing)}: {missing[:5]}"

    raters = {"md": r1, "csv": r2, "csv2": r3}
    pairs = [("md", "csv"), ("md", "csv2"), ("csv", "csv2")]

    print("== 人-人一致性（新口径三盲，30 条）==")
    ceiling = {}
    for idx, metric in ((0, "rel"), (1, "faith")):
        ks = [report(a, raters[a], b, raters[b], idx, metric) for a, b in pairs]
        ceiling[metric] = statistics.mean(ks)

    old = {g["id"]: g for g in pilot}
    print("== vs 旧 golden（漂移检查）==")
    for idx, metric in ((0, "rel"), (1, "faith")):
        new_med = {}
        for i in ids:
            new_med[i] = (statistics.median([raters[n][i][0] for n in raters]),
                          statistics.median([raters[n][i][1] for n in raters]))
        for name, r in raters.items():
            a = [r[i][idx] for i in ids]
            b = [old[i]["human_answer_relevancy" if idx == 0 else "human_faithfulness"] for i in ids]
            k = kappa_bands([band(x) for x in a], [band(x) for x in b])
            print(f"  [{name} vs old] {metric}: band_kappa={k:.4f} tau_b={tau_b(a, b):.4f}")
        a = [new_med[i][idx] for i in ids]
        b = [old[i]["human_answer_relevancy" if idx == 0 else "human_faithfulness"] for i in ids]
        print(f"  [new-median vs old] {metric}: band_kappa={kappa_bands([band(x) for x in a], [band(x) for x in b]):.4f} "
              f"tau_b={tau_b(a, b):.4f}")

    print("== 新天花板与门禁预览 ==")
    for metric in ("rel", "faith"):
        c = ceiling[metric]
        print(f"  {metric}: ceiling(band_kappa mean)={c:.4f}  gate 0.9x={0.9 * c:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
