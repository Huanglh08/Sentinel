# 一次性诊断：embedding 跨语言 A/B —— 同一套生产 chunk 文本，四个配置对比 Recall@5
# 配置：qwen3-8b（现状） / qwen3-8b+instruction（官方用法） / BAAI/bge-m3 / netease-youdao/bce-embedding-base_v1
# chunk 文本从 t_knowledge_vector 捞（与生产分块策略完全一致），查询用 retrieval-seeds.jsonl 正例。
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))  # .env 相对进程 cwd 加载

import numpy as np

from core.embedding import EmbeddingConfig
from core.embedding.base import ModelTarget
from core.embedding.clients.siliconflow import SiliconFlowEmbeddingClient
from core.embedding.config import ModelCandidate

SEED_PATH = os.path.join("..", "evaluation", "seeds", "retrieval-seeds.jsonl")
TOPK = 5
# Qwen3-Embedding 官方查询侧 instruction（Qwen3-Embedding 用法：Instruct 前缀 + Query）
QWEN_INSTRUCTION = (
    "Instruct: Given a user question about AI security risk control, "
    "retrieve the most relevant security documentation\nQuery: "
)

# (label, model, dimension, use_instruction, no_dimensions)
# bce-emb-v1 已从硅基流动下架（403 Model disabled），不再纳入
CONFIGS = [
    ("qwen3-8b(现状)", "Qwen/Qwen3-Embedding-8B", 1536, False, False),
    ("qwen3-8b+instr", "Qwen/Qwen3-Embedding-8B", 1536, True, False),
    ("bge-m3", "BAAI/bge-m3", 1024, False, True),
]


class NoDimClient(SiliconFlowEmbeddingClient):
    """bge-m3/BCE 不接受 dimensions 参数——base 类写入后经 customize 删除。"""

    def customize_request_body(self, body: dict, target: ModelTarget) -> None:
        super().customize_request_body(body, target)
        body.pop("dimensions", None)


qwen_client = SiliconFlowEmbeddingClient()  # 保留 dimensions=1536，与生产 body 一致
nodim_client = NoDimClient()
cfg = EmbeddingConfig()
print("siliconflow key set:", bool(cfg.siliconflow_api_key))


def make_target(model: str, dim: int) -> ModelTarget:
    return ModelTarget(
        id=f"probe-{model}",
        candidate=ModelCandidate(id="probe", provider="siliconflow", model=model, dimension=dim),
        provider=cfg.get_provider("siliconflow"),
    )


def embed_all(texts: list[str], target: ModelTarget, no_dim: bool = False) -> np.ndarray:
    cli = nodim_client if no_dim else qwen_client
    out: list[list[float]] = []
    for i in range(0, len(texts), 32):  # ponytail: 硅基流动 max_batch_size=32，硬编码与 client 一致
        out.extend(cli.embed_batch(texts[i : i + 32], target))
    arr = np.asarray(out, dtype=np.float32)
    arr /= (np.linalg.norm(arr, axis=1, keepdims=True) + 1e-12)  # 归一化后点积=cosine
    return arr


def strip_md(name: str) -> str:
    return name.removesuffix(".md")


# 1) 捞生产 chunk（content + doc_name）
import psycopg

from core.db import get_dsn

# metadata 键名是 source_file（非 doc_name）
with psycopg.connect(get_dsn()) as conn, conn.cursor() as cur:
    cur.execute("SELECT content, metadata->>'source_file' FROM t_knowledge_vector")
    rows = cur.fetchall()
chunks = [r[0] for r in rows]
chunk_docs = [strip_md(r[1] or "") for r in rows]
print(f"chunks: {len(chunks)}")

# 2) 正例 seed
with open(SEED_PATH, encoding="utf-8") as f:
    seeds = [json.loads(line) for line in f if line.strip()]
seeds = [s for s in seeds if s.get("expected_doc_ids")]
print(f"positive seeds: {len(seeds)}")

# 3) 逐配置评测
results: dict[str, dict] = {}
for label, model, dim, use_instr, no_dim in CONFIGS:
    t0 = time.time()
    target = make_target(model, dim)
    cmat = embed_all(chunks, target, no_dim)
    q_texts = [((QWEN_INSTRUCTION + s["q"]) if use_instr else s["q"]) for s in seeds]
    qmat = embed_all(q_texts, target, no_dim)
    sims = qmat @ cmat.T  # (n_seeds, n_chunks)

    rec_i, hit_i, mrr_i = [], [], []
    fails: list[str] = []
    for si, s in enumerate(seeds):
        exp = set(s["expected_doc_ids"])
        top = np.argsort(-sims[si])[:TOPK]
        got = {chunk_docs[j] for j in top}
        inter = len(got & exp)
        rec_i.append(inter / len(exp))
        hit = inter > 0
        hit_i.append(1.0 if hit else 0.0)
        if not hit:
            fails.append(s["id"])
        rr = 0.0
        for rank, j in enumerate(top, 1):
            if chunk_docs[j] in exp:
                rr = 1.0 / rank
                break
        mrr_i.append(rr)
    results[label] = {
        "recall@5": sum(rec_i) / len(rec_i),
        "hit@5": sum(hit_i) / len(hit_i),
        "mrr": sum(mrr_i) / len(mrr_i),
        "fails": fails,
        "sec": time.time() - t0,
    }
    print(f"[{label}] recall@5={results[label]['recall@5']:.4f} hit@5={results[label]['hit@5']:.4f} "
          f"mrr={results[label]['mrr']:.4f} fails={len(fails)} ({results[label]['sec']:.0f}s)")

# 4) 差异分析：换模型能救的题 / 会丢的题
base_fails = set(results[CONFIGS[0][0]]["fails"])
print("\n--- 差异明细 ---")
for label, *_ in CONFIGS[1:]:
    f = set(results[label]["fails"])
    fixed = sorted(base_fails - f)
    lost = sorted(f - base_fails)
    print(f"[{label}] 修复(现状挂它过)={fixed or '-'}  退化(现状过它挂)={lost or '-'}")
print("\n现状失败清单:", sorted(base_fails))
