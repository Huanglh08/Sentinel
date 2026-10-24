"""切块本体的两项体检：块完整度、语义集中度（检索集合侧）。

块完整度 = 块是不是一个自足单元，与检索无关，逐块静态检查：
    缺标题   块从文档中途断出，开头没有标题，读者不知道这段在讲哪一节
    挂空标题 块末尾是一个小节标题，正文落到了下一块
    表格断头 块里有表格行，但表头（含 |--- 分隔行）不在本块
    列表断链 块以列表项开头，引导句留在了上一块
    句中断   块末尾不是句读符号，句子被切一半（不含标题行、表格行）

语义集中度 = 检索回来的前 k 条是不是聚在同一篇文档上（HHI，1 表示全来自一篇），
    以及最高分与均分的落差。不需要期望文档，负例题也能算。

跑法：python -m eval.chunk_quality --top-k 12
向量复用 chunk_experiment 的缓存，不重复调 embedding。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from eval.chunk_experiment import (
    _VEC_DIR,
    CASE_IDS,
    EMBED_MODEL,
    _norm,
    build_variants,
    load_docs,
)

_HEAD_RE = re.compile(r"^#")
_TABLE_HEAD_RE = re.compile(r"^\|.*\|")
_TABLE_SEP_RE = re.compile(r"^\|[\s:|-]+\|$")
_LIST_RE = re.compile(r"^(?:[-*+]|\d+[.、)])\s")
_END_OK = tuple("。：！？；.|)）】」”\"':;")


def completeness(chunks: list[dict]) -> dict:
    """逐块检查五类残缺，返回占比。块按文档内顺序给出。"""
    n = len(chunks)
    no_head = dangling = table_broken = list_orphan = cut_sentence = 0
    for i, c in enumerate(chunks):
        lines = [ln for ln in c["text"].splitlines() if ln.strip()]
        if not lines:
            continue
        first, last = lines[0].lstrip(), lines[-1].strip()
        # 缺标题：中途断出的块开头没有标题（文档首块豁免）
        if not _HEAD_RE.match(first) and i > 0 and chunks[i - 1]["doc"] == c["doc"]:
            no_head += 1
        # 挂空标题：块以一个小节标题收尾，正文在下一块
        if _HEAD_RE.match(last):
            dangling += 1
        # 表格断头：有表格行，但表头分隔行不在这块
        has_row = any(_TABLE_HEAD_RE.match(ln.strip()) for ln in lines[1:])
        has_sep = any(_TABLE_SEP_RE.match(ln.strip()) for ln in lines)
        if has_row and not has_sep:
            table_broken += 1
        # 列表断链：块里没有任何标题作为引子，第一行就是列表项，说明引导句在上一块
        titled = any(_HEAD_RE.match(ln.lstrip()) for ln in lines)
        if not titled and _LIST_RE.match(first):
            list_orphan += 1
        # 句中断：末尾不是句读，且不是标题行、表格行
        if not last.endswith(_END_OK) and not _HEAD_RE.match(last) and not _TABLE_HEAD_RE.match(last):
            cut_sentence += 1
    pct = lambda x: round(x / n, 4) if n else 0.0
    return {
        "chunks": n,
        "no_heading": pct(no_head),
        "dangling_heading": pct(dangling),
        "table_broken": pct(table_broken),
        "list_orphan": pct(list_orphan),
        "cut_sentence": pct(cut_sentence),
    }


def concentration(variant: str, chunks: list[dict], cases, k: int) -> dict:
    """前 k 条的语义集中度：同一篇文档的聚集程度 + 首名与均分的落差。"""
    cache = _VEC_DIR / f"{EMBED_MODEL.split('/')[-1]}-{variant}-{len(chunks)}.npy"
    if not cache.exists():
        return {}
    mat = np.load(cache).astype(np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9
    qpath = _VEC_DIR / f"queries-{len(cases)}.json"
    if not qpath.exists():
        return {}
    qvecs = [_norm(v) for v in json.loads(qpath.read_text(encoding="utf-8"))]
    hhis, margins, sims, neg_sims = [], [], [], []
    for qv, case in zip(qvecs, cases):
        sims_all = mat @ np.asarray(qv, dtype=np.float32)
        idx = np.argsort(-sims_all)[:k]
        docs = [chunks[i]["doc"] for i in idx]
        share: dict[str, int] = {}
        for d in docs:
            share[d] = share.get(d, 0) + 1
        hhis.append(sum((v / k) ** 2 for v in share.values()))
        top = float(sims_all[idx[0]])
        sims.append(top)
        margins.append(top - float(sims_all[idx].mean()))
        if not case.get("expected_doc_ids"):
            neg_sims.append(top)
    return {
        "hhi": round(sum(hhis) / len(hhis), 4),
        "top1_sim": round(sum(sims) / len(sims), 4),
        "top1_vs_mean": round(sum(margins) / len(margins), 4),
        # 无答案题：库里没有该答案，首名相似度越接近有答案题说明检索越没有区分力
        "neg_top1_sim": round(sum(neg_sims) / len(neg_sims), 4) if neg_sims else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=12)
    args = ap.parse_args()

    docs, base = load_docs()
    variants = build_variants(docs, base)

    cases = [
        c
        for c in (
            json.loads(line)
            for line in (Path(__file__).resolve().parents[2] / "evaluation" / "seeds" / "ragas-seeds.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        )
        if c["id"] in CASE_IDS
    ]

    print(
        "变体 | 块数 | 缺标题 | 挂空标题 | 表格断头 | 列表断链 | 句中断 | "
        "聚集度HHI | 首名相似度 | 首名领先均分 | 无答案题首名相似度"
    )
    for name, chunks in variants.items():
        q = completeness(chunks)
        c = concentration(name, chunks, cases, args.top_k)
        print(
            f"{name} | {q['chunks']} | {q['no_heading']:.3f} | {q['dangling_heading']:.3f} | "
            f"{q['table_broken']:.3f} | {q['list_orphan']:.3f} | {q['cut_sentence']:.3f} | "
            f"{c.get('hhi', '-')} | {c.get('top1_sim', '-')} | {c.get('top1_vs_mean', '-')} | "
            f"{c.get('neg_top1_sim', '-')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
