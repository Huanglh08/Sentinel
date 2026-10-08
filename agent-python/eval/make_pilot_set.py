# -*- coding: utf-8 -*-
"""Step 2: 抽 30 条试点集（judge 校准方案 v2）。

分层：guideline 锚例条全进（11 条，直接测标尺复现），其余按
分裂条（任一维度三盲跨档）/ near-edge（旧中位数落 0.3-0.6 或 0.7-0.95）/ 共识
三层固定 seed 随机抽样。输出 pilot golden 子集 jsonl，供打分包与试点统计用。

自检：30 条、无重复、逐条含 human_scores_detail 三盲分。
用法（agent-python 目录下）：python -m eval.make_pilot_set
"""
import json
import random
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN = REPO_ROOT / "evaluation" / "seeds" / "judge-calibration.jsonl"
OUT = REPO_ROOT / "evaluation" / "seeds" / "judge-calibration-pilot30.jsonl"
BB = (0.5, 0.85)
RATERS = ("md", "csv", "csv2")
ANCHORS = ["JC04", "JC18", "JC21", "JC53", "JC54", "JC64",
           "JC65", "JC72", "JC75", "JC83", "JC100"]
QUOTA = {"split": 8, "edge": 6, "consensus": 5}
SEED = 20260905  # 固定种子，重跑可复现


def band(x):
    return 0 if x < BB[0] else (1 if x <= BB[1] else 2)


def tri_flags(g):
    """(has_split, has_edge)：任一维度三盲跨档 / 旧中位数落边界带。"""
    split = edge = False
    for mkey, hkey in (("rel", "human_answer_relevancy"), ("faith", "human_faithfulness")):
        tri = [g["human_scores_detail"][r][mkey] for r in RATERS]
        med = g[hkey]
        if len({band(v) for v in tri}) > 1:
            split = True
        if 0.30 <= med <= 0.60 or 0.70 <= med <= 0.95:
            edge = True
    return split, edge


def main() -> int:
    recs = [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(recs) == 100, f"golden {len(recs)} != 100"
    by_id = {g["id"]: g for g in recs}
    missing = [a for a in ANCHORS if a not in by_id]
    assert not missing, f"锚例缺条: {missing}"

    picked = ANCHORS[:]
    layers = {"split": [], "edge": [], "consensus": []}
    for g in recs:
        if g["id"] in ANCHORS:
            continue
        s, e = tri_flags(g)
        layers["split" if s else ("edge" if e else "consensus")].append(g["id"])

    rng = random.Random(SEED)
    for name, ids in layers.items():
        picked += rng.sample(ids, min(QUOTA[name], len(ids)))
    # 层容量不足时从共识层补齐
    if len(picked) < 30:
        rest = [i for i in layers["consensus"] if i not in picked]
        picked += rng.sample(rest, 30 - len(picked))
    assert len(picked) == 30, f"试点集 {len(picked)} != 30"
    assert len(set(picked)) == 30, "重复抽样"

    OUT.write_text(
        "\n".join(json.dumps(by_id[i], ensure_ascii=False) for i in picked) + "\n",
        encoding="utf-8")
    print("[pilot] layers: " + ", ".join(f"{k}={len(v)}" for k, v in layers.items()))
    print(f"[pilot] 30 items -> {OUT}")
    print("[pilot] ids:", " ".join(picked))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
