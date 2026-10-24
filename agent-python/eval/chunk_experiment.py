"""切块粒度对照实验（纯向量召回，不含精排、门控、关键词通道）。

对比三种切法在同一批题上的召回表现：
    A 现状：库里现有的 441 块（每篇平均两块，均值约 1000 字）
    B 按二级标题切：语义边界优先，不设 overlap
    C 按二级标题切 + 单节超 600 字再按段落切

指标：
    key_coverage  参考答案里的关键数值被 top-k 覆盖的比例（低于 1 说明答案被切丢）
    purity        top-k 中命中期望文档的字数占比（越低说明混入的无关内容越多）
    ctx_chars     top-k 总字数（越大信噪比越差）
    hit_doc       期望文档是否出现在 top-k

只做离线实验，不写库、不影响生产索引。

运行：
    python -m eval.chunk_experiment --top-k 8
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import argparse
import asyncio
import json
import math
import re
import sys

import numpy as np
from collections import defaultdict
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_AGENT_ROOT), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dotenv import load_dotenv

load_dotenv()

EMBED_MODEL = os.getenv("RAGAS_EMBED_MODEL", "Qwen/Qwen3-Embedding-8B")
EMBED_BASE_URL = os.getenv("RAGAS_EMBED_BASE_URL", "https://api.siliconflow.cn/v1")
QUERY_INSTRUCTION = (
    "Instruct: Given a user question about AI security risk control, "
    "retrieve the most relevant security documentation\nQuery: "
)

# 代表题：覆盖单值、长清单、跨表、跨文档冲突、混淆、负例（S/M/A 各取若干 + X 短问 + N 负例）
CASE_IDS = [
    "S001", "S003", "S006", "S010", "S014", "S018", "S024", "S029", "S032",
    "M001", "M004", "M005", "M010", "M012", "M016", "M017",
    "A001", "A002", "A006", "A008", "A010",
    "X001", "X004", "X006", "X009",
    "N001", "N005",
]

KEY_PAT = re.compile(r"\d+(?:\.\d+)?\s*(?:%|分|元|万元|天|小时|分钟|秒|次|篇|条|年|个工作日|工作日|倍)")

# [chunk_exp] 块向量缓存：同一模型/同一变体/同一块数直接复用，改指标不用重新调 embedding
_VEC_DIR = _REPO_ROOT / ".sentinel" / "chunk_exp_vecs"


def load_docs() -> tuple[dict[str, str], list[dict]]:
    from core.db import get_pool

    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT metadata->>'source_file' AS f, (metadata->>'chunk_index')::int AS i,"
            " content FROM t_knowledge_vector ORDER BY 1, 2"
        ).fetchall()
    grouped: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for f, i, content in rows:
        grouped[f].append((i, content))

    docs: dict[str, str] = {}
    for f, items in grouped.items():
        docs[f] = "\n".join(c for _, c in sorted(items))
    chunks = [{"doc": f, "text": c} for f, _, c in rows]
    return docs, chunks


def _merge_head(parts: list[str]) -> list[str]:
    """首个片段是不带二级标题的文档头（业务码/域/版本），并进第一节。"""
    if len(parts) > 1 and not parts[0].startswith("## "):
        parts = [parts[0].rstrip() + "\n\n" + parts[1]] + parts[2:]
    return parts


def split_by_heading(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?m)^(?=## )", text) if p.strip()]
    return _merge_head(parts)


def _pack(paras: list[str], cap: int) -> list[str]:
    """按空行装箱。标题行不留在块尾：它会被带到下一块开头，否则那块就没有标题。"""
    out: list[str] = []
    buf: list[str] = []
    size = 0
    carry = ""
    for para in paras:
        if size + len(para) > cap and buf:
            while len(buf) > 1 and buf[-1].startswith("#"):
                carry = buf.pop() + ("\n\n" + carry if carry else "")
            out.append("\n\n".join(buf))
            buf, size = ([carry] if carry else []), len(carry)
            carry = ""
        buf.append(para)
        size += len(para)
    if buf:
        out.append("\n\n".join(buf))
    return out


def split_by_heading_capped(text: str, cap: int = 600, keep_heading: bool = False) -> list[str]:
    """按二级标题切，单节超 cap 再按空行装箱。

    keep_heading=True 时把本节标题回填到每个子块开头，并保证标题不落在块尾：
    一节被拆开后若只有首块带标题，其余块开头直接是正文或列表项，
    读者和模型都不知道这段属于哪一节。
    """
    out: list[str] = []
    for section in split_by_heading(text):
        if len(section) <= cap:
            out.append(section)
            continue
        head = section.split("\n", 1)[0].strip() if section.startswith("#") else ""
        # 三级以下标题常紧跟正文（单换行），先把它拆成独立段落，才能不落在块尾
        body = re.sub(r"(?m)^(#{1,6} )", r"\n\n\1", section)
        paras = [
            p.strip()
            for p in re.split(r"\n\s*\n", body)
            if p.strip() and not (keep_heading and p.strip() == head)
        ]
        for part in _pack(paras, cap):
            out.append(f"{head}\n\n{part}" if keep_heading and head else part)
    return out


def build_variants(docs: dict[str, str], base_chunks: list[dict]) -> dict[str, list[dict]]:
    """五种切法 + 一种带标题回填的切法，实验与体检共用同一份定义。"""
    variants = {
        "A_现状": base_chunks,
        "B_按标题": [{"doc": f, "text": t} for f, tx in docs.items() for t in split_by_heading(tx)],
    }
    for cap in (300, 600, 1000):
        variants[f"标题+{cap}"] = [
            {"doc": f, "text": t} for f, tx in docs.items() for t in split_by_heading_capped(tx, cap)
        ]
    variants["标题+600+回填"] = [
        {"doc": f, "text": t}
        for f, tx in docs.items()
        for t in split_by_heading_capped(tx, 600, keep_heading=True)
    ]
    return variants


async def embed_texts(texts: list[str], batch: int = 32) -> list[list[float]]:
    from openai import AsyncOpenAI

    key = os.environ.get("SILICONFLOW_API_KEY")
    if not key:
        raise RuntimeError("缺少 SILICONFLOW_API_KEY")
    client = AsyncOpenAI(api_key=key, base_url=EMBED_BASE_URL, timeout=120.0, max_retries=3)
    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        part = texts[i : i + batch]
        resp = await client.embeddings.create(model=EMBED_MODEL, input=part, dimensions=1536)
        out.extend(d.embedding for d in resp.data)
        print(f"[chunk_exp] embed {min(i + batch, len(texts))}/{len(texts)}", file=sys.stderr)
    return out


def _norm(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def retrieve(qvec: list[float], mat: list[list[float]], k: int) -> list[int]:
    scores = [(sum(a * b for a, b in zip(qvec, m)), i) for i, m in enumerate(mat)]
    scores.sort(reverse=True)
    return [i for _, i in scores[:k]]


def _num_keys(reference: str) -> list[str]:
    """参考答案里的关键数值（去掉单位）。文档常写「高=40」，参考答案写「40 分」，
    只比数值才能反映证据是否被召回，否则度量的是措辞而非内容。"""
    return list(dict.fromkeys(re.sub(r"[^\d.]", "", k) for k in KEY_PAT.findall(reference)))


def _missing(keys: list[str], blob: str) -> list[str]:
    return [k for k in keys if not re.search(rf"(?<![\d.]){re.escape(k)}(?![\d.])", blob)]


def key_coverage(reference: str, texts: list[str]) -> float | None:
    keys = _num_keys(reference)
    if not keys:
        return None
    blob = "".join(texts).replace(" ", "")
    return round(sum(1 for k in keys if k not in _missing([k], blob)) / len(keys), 4)


def run_variant(name: str, chunks: list[dict], qvecs: dict[str, list[float]], vecs, cases, k: int) -> dict:
    rows = []
    probe = max(k, 30)  # 额外看前 30 名，算「拿全关键值最少要几名」
    for case in cases:
        idx = retrieve(qvecs[case["q"]], vecs, probe)
        picked = [chunks[i] for i in idx]
        keys = _num_keys(case["reference"])
        exp = set(case["expected_doc_ids"])
        acc = ""
        min_rank = None
        for rank, c in enumerate(picked, 1):
            if any(e in c["doc"] for e in exp):
                acc += c["text"].replace(" ", "")
            if keys and not _missing(keys, acc):
                min_rank = rank
                break
        if keys and min_rank is None:
            min_rank = -1  # 前 30 名都凑不齐
        picked = picked[:k]
        texts = [c["text"] for c in picked]
        total = sum(len(t) for t in texts)
        in_exp = [c["text"] for c in picked if any(e in c["doc"] for e in exp)]
        rel = sum(len(t) for t in in_exp)
        # 只在期望文档的块里找：无关文档里碰巧出现同一个数字不算覆盖
        miss_exp = _missing(keys, "".join(in_exp).replace(" ", "")) if keys else []
        rows.append(
            {
                "id": case["id"],
                "min_rank_all_keys": min_rank,
                "ctx_chars": total,
                "purity": round(rel / total, 4) if total else 0.0,
                "hit_doc": any(any(e in c["doc"] for e in exp) for c in picked) if exp else None,
                "key_coverage": key_coverage(case["reference"], texts),
                "key_cov_exp": round(1 - len(miss_exp) / len(keys), 4) if keys else None,
                "top1_chars": len(texts[0]) if texts else 0,
                "missing_keys": miss_exp,
                "top_k": [
                    {"doc": c["doc"], "chars": len(c["text"]), "head": c["text"][:50].replace("\n", " ")}
                    for c in picked
                ],
            }
        )
    return {"name": name, "chunk_count": len(chunks), "cases": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description="切块粒度对照实验")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--output", default=str(_REPO_ROOT / "evaluation" / "reports"))
    args = parser.parse_args()

    docs, base_chunks = load_docs()
    variants = build_variants(docs, base_chunks)

    seeds = {
        r["id"]: r
        for r in (
            json.loads(line)
            for line in (_REPO_ROOT / "evaluation" / "seeds" / "ragas-seeds.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        )
    }
    cases = [seeds[i] for i in CASE_IDS if i in seeds]

    for name, chunks in variants.items():
        sizes = sorted(len(c["text"]) for c in chunks)
        p50 = sizes[len(sizes) // 2]
        p90 = sizes[int(len(sizes) * 0.9)]
        print(f"[chunk_exp] {name}: {len(chunks)} 块，中位 {p50} 字，P90 {p90} 字", file=sys.stderr)

    async def build():
        qtexts = [QUERY_INSTRUCTION + c["q"] for c in cases]
        qcache = _VEC_DIR / f"queries-{len(cases)}.json"
        if qcache.exists():
            qvecs = json.loads(qcache.read_text(encoding="utf-8"))
        else:
            qvecs = await embed_texts(qtexts)
            _VEC_DIR.mkdir(parents=True, exist_ok=True)
            qcache.write_text(json.dumps(qvecs), encoding="utf-8")
        out = {}
        for name, chunks in variants.items():
            texts = [c["text"] for c in chunks]
            cache = _VEC_DIR / f"{EMBED_MODEL.split('/')[-1]}-{name}-{len(texts)}.npy"
            if cache.exists():
                vecs = np.load(cache).tolist()
            else:
                vecs = await embed_texts(texts)
                _VEC_DIR.mkdir(parents=True, exist_ok=True)
                np.save(cache, np.asarray(vecs, dtype=np.float32))
            out[name] = ([_norm(v) for v in vecs], [_norm(v) for v in qvecs])
        return out

    embedded = asyncio.run(build())
    results = []
    for name, chunks in variants.items():
        vecs, qvecs = embedded[name]
        qmap = {c["q"]: qvecs[i] for i, c in enumerate(cases)}
        results.append(run_variant(name, chunks, qmap, vecs, cases, args.top_k))

    report = {"top_k": args.top_k, "cases": CASE_IDS, "variants": results}
    (Path(args.output) / f"chunk-exp-top{args.top_k}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n变体 | 块数 | 平均上下文 | 平均纯度 | 前%d覆盖 | 拿全关键值名次(均值/中位) | 文档命中" % args.top_k)
    for r in results:
        rows = r["cases"]
        avg_ctx = round(sum(x["ctx_chars"] for x in rows) / len(rows))
        pur = [x["purity"] for x in rows]
        cov = [x["key_cov_exp"] for x in rows if x["key_cov_exp"] is not None]
        ranks = sorted(30 if x["min_rank_all_keys"] == -1 else x["min_rank_all_keys"]
                       for x in rows if x["min_rank_all_keys"] is not None)
        hit = [x["hit_doc"] for x in rows if x["hit_doc"] is not None]
        never = sum(1 for x in rows if x["min_rank_all_keys"] == -1)
        print(
            f"{r['name']} | {r['chunk_count']} | {avg_ctx} | {sum(pur)/len(pur):.3f} | "
            f"{sum(cov)/len(cov) if cov else 0:.3f} | "
            f"{sum(ranks)/len(ranks) if ranks else 0:.1f}/{ranks[len(ranks)//2] if ranks else 0}"
            f"{'（%d 题 30 名内凑不齐）' % never if never else ''} | {sum(hit)}/{len(hit)}"
        )
    print("\n逐题（期望文档内关键值覆盖 / 缺哪个值）：")
    for r in results:
        print(" ", r["name"], {x["id"]: (x["key_cov_exp"], x["missing_keys"]) for x in r["cases"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
