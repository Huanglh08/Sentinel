"""切块选型门禁：检索失败率 + 按答案片段计算的 token 级精度。

选型只看这两个数，其余指标不参与排序：
    检索失败率 = 前 k 条没能拿全答案片段的题数占比（1 - recall@k 的严格版）
    token 级精度 = 命中的答案片段字符数 / 前 k 条总字符数
        分子按重数取交集（重叠重复的字符只算一次有效），分母统计全部召回字符，
        所以块间重叠造成的冗余会被罚分。

答案片段的锚定方式：参考答案里每个关键数值，在期望文档里找出包含它的句子，
取其中较短的一句，合并成该题的锚点。参考答案里的数值只要有一个在期望文档里
找不到，这道题就不可用（说明题目和语料对不上，或参考答案是转述）。

用法：
    python -m eval.chunk_gate --audit        # 只看样本够不够
    python -m eval.chunk_gate --top-k 8,12   # 跑两个指标
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.chunk_experiment import (
    KEY_PAT,
    _VEC_DIR,
    CASE_IDS,
    EMBED_MODEL,
    QUERY_INSTRUCTION,
    _norm,
    _num_keys,
    build_variants,
    embed_texts,
    load_docs,
    retrieve,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEEDS = _REPO_ROOT / "evaluation" / "seeds" / "ragas-seeds.jsonl"
_SENT_SPLIT = re.compile(r"(?<=[。！？；\n])")


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _sentences(text: str) -> list[str]:
    return [s for s in _SENT_SPLIT.split(text) if s.strip()]


_UNIT_ALIASES = {
    "秒": ["s", "sec"],
    "分钟": ["min", "m"],
    "分": ["分钟", "min"],
    "小时": ["h", "hr"],
    "天": ["d"],
    "工作日": ["个工作日"],
    "万元": ["万"],
    "%": ["％"],
}


def _unit_aliases(unit: str) -> list[str]:
    out = list(_UNIT_ALIASES.get(unit, []))
    return out


def build_anchor(case: dict, docs: dict[str, str]) -> tuple[str, list[str], str]:
    """返回 (锚点文本, 缺失的关键值, 不可用原因)。"""
    values = _num_keys(case["reference"])
    if not values:
        return "", [], "参考答案没有可锚定的数值"
    exp_docs = [docs[d] for d in case["expected_doc_ids"] if d in docs]
    if not exp_docs:
        return "", [], "期望文档不在库里"

    keys = list(dict.fromkeys(KEY_PAT.findall(case["reference"])))
    picked: list[str] = []
    missing: list[str] = []
    for key in keys:
        value = re.sub(r"[^\d.]", "", key)
        unit = re.sub(r"[\d.\s]", "", key)
        # 文档与参考答案的单位写法常不一致（秒/s、小时/h、分钟/min），逐个别名尝试；
        # 最后按裸数值兜底，兜底时排除 R4/R5、C01、v1.0 这类标识符里的数字
        pats = [re.escape(_norm_text(value + unit))]
        pats += [rf"(?<![\w./-]){re.escape(value)}{re.escape(_norm_text(a))}" for a in _unit_aliases(unit)]
        pats.append(rf"(?<![\w./-]){re.escape(value)}(?![\w./-])")
        hits: list[str] = []
        for pat in pats:
            hits = [s for d in exp_docs for s in _sentences(d) if re.search(pat, _norm_text(s))]
            if hits:
                break
        if not hits:
            missing.append(key)
            continue
        best = min(hits, key=len).strip()
        if best not in picked:
            picked.append(best)
    if missing:
        return "", missing, "关键值在期望文档里找不到"
    return "\n".join(picked), [], ""


def score(anchor: str, texts: list[str]) -> tuple[float, float]:
    """返回 (recall, precision)，字符当 token，分子按重数取交集。"""
    te = Counter(_norm_text(anchor))
    tr = Counter(_norm_text("".join(texts)))
    hit = sum(min(n, tr[ch]) for ch, n in te.items())
    total_e = sum(te.values())
    total_r = sum(tr.values())
    return (hit / total_e if total_e else 0.0, hit / total_r if total_r else 0.0)


def load_cases(ids: list[str] | None = None) -> list[dict]:
    rows = [json.loads(line) for line in _SEEDS.read_text(encoding="utf-8").splitlines() if line.strip()]
    if ids is None:
        return rows
    keep = set(ids)
    return [r for r in rows if r["id"] in keep]


def audit(docs: dict[str, str]) -> None:
    for label, cases in (("本次 27 题", load_cases(CASE_IDS)), ("全量 80 题", load_cases())):
        ok, reasons, short = 0, Counter(), 0
        print(f"\n== {label} 锚点可用性 ==")
        for case in cases:
            anchor, missing, why = build_anchor(case, docs)
            if why:
                reasons[why] += 1
                if label == "本次 27 题":
                    print(f"  {case['id']}: 不可用（{why}）缺失值 {missing or '-'}")
                continue
            ok += 1
            if len(anchor) < 20:
                short += 1
            if label == "本次 27 题":
                print(f"  {case['id']}: 锚点 {len(anchor)} 字 {len(_num_keys(case['reference']))} 个值")
        print(f"  可用 {ok}/{len(cases)}，短缺（锚点<20 字）{short}，原因分布 {dict(reasons)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--top-k", default="8,12")
    ap.add_argument("--spans", default="", help="片段标注文件，默认 evaluation/seeds/answer-spans.jsonl")
    ap.add_argument("--ids", default="all", choices=["all", "subset"], help="subset=原来那 27 题")
    args = ap.parse_args()

    docs, base = load_docs()
    if args.audit:
        audit(docs)
        return 0

    variants = build_variants(docs, base)
    span_file = Path(args.spans) if args.spans else _REPO_ROOT / "evaluation" / "seeds" / "answer-spans.jsonl"
    if not span_file.exists():
        print(f"缺少片段标注 {span_file}，先跑 python -m eval.build_spans")
        return 1
    spans = {
        row["id"]: "\n".join(row["spans"])
        for row in (json.loads(line) for line in span_file.read_text(encoding="utf-8").splitlines() if line.strip())
    }
    cases = [c for c in (load_cases(CASE_IDS) if args.ids == "subset" else load_cases()) if c["id"] in spans]
    anchors = {c["id"]: spans[c["id"]] for c in cases}
    if not cases:
        print("没有可锚定的题，先跑 python -m eval.build_spans")
        return 1

    async def chunk_vecs(name: str, chunks: list[dict]) -> list[list[float]]:
        texts = [c["text"] for c in chunks]
        cache = _VEC_DIR / f"{EMBED_MODEL.split('/')[-1]}-{name}-{len(texts)}.npy"
        if cache.exists():
            import numpy as np

            return [_norm(v) for v in np.load(cache).tolist()]
        vecs = await embed_texts(texts)
        _VEC_DIR.mkdir(parents=True, exist_ok=True)
        import numpy as np

        np.save(cache, np.asarray(vecs, dtype=np.float32))
        return [_norm(v) for v in vecs]

    async def query_vecs() -> list[list[float]]:
        qcache = _VEC_DIR / f"queries-{len(cases)}.json"
        if qcache.exists():
            return [_norm(v) for v in json.loads(qcache.read_text(encoding="utf-8"))]
        vecs = await embed_texts([QUERY_INSTRUCTION + c["q"] for c in cases])
        _VEC_DIR.mkdir(parents=True, exist_ok=True)
        qcache.write_text(json.dumps(vecs), encoding="utf-8")
        return [_norm(v) for v in vecs]

    qvecs = asyncio.run(query_vecs())
    ks = [int(x) for x in str(args.top_k).split(",") if x.strip()]
    report = {"cases": [c["id"] for c in cases], "variants": []}

    for name, chunks in variants.items():
        vecs = asyncio.run(chunk_vecs(name, chunks))
        row = {"name": name, "chunks": len(chunks), "per_k": {}}
        for k in ks:
            fails, recalls, precs = [], [], []
            for qv, case in zip(qvecs, cases):
                picked = [chunks[i] for i in retrieve(qv, vecs, k)]
                recall, prec = score(anchors[case["id"]], [c["text"] for c in picked])
                fails.append(case["id"] if recall < 1.0 else None)
                recalls.append(recall)
                precs.append(prec)
            n = len(cases)
            row["per_k"][k] = {
                "fail_rate": round(sum(1 for x in fails if x) / n, 4),
                "failed_cases": [x for x in fails if x],
                "recall": round(sum(recalls) / n, 4),
                "precision_pct": round(100 * sum(precs) / n, 2),
            }
        report["variants"].append(row)

    for k in ks:
        print(f"\n前 {k} 条 | 变体 | 块数 | 失败率 | 平均召回 | token 级精度")
        for row in report["variants"]:
            m = row["per_k"][k]
            print(
                f"  {row['name']} | {row['chunks']} | {m['fail_rate']:.3f} | "
                f"{m['recall']:.3f} | {m['precision_pct']:.2f}%"
            )

    out = _REPO_ROOT / "evaluation" / "reports" / "chunk-gate-latest.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
