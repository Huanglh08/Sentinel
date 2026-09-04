"""双模型 rerank 门控阈值离线校准（P1 负样本治理）。

流程:
  1. 读取 retrieval-seeds.jsonl（负样本 = expected_doc_ids 为空）
  2. 逐条调用 Java EvalController /rag/eval，取 rerank 后的 retrievedContexts
     ——与生产门控打分对象一致（门控位于 Rerank 之后）
  3. DualRerankGate（bge-reranker-v2-m3 × Qwen3-Reranker-0.6B）对每条打分
  4. 阈值网格扫描: 每个 (bge_t, qwen3_t) 组合输出负样本切空率 / 正样本误伤清单
  5. 给出「误伤=0 约束下切空率最大化」的推荐阈值

输出 JSON + Markdown 报告。

运行（必须以模块方式、在 agent-python 目录下）:
    python -m eval.rerank_gate_eval
或:
    python -m eval.rerank_gate_eval --eval-url http://localhost:9090/api/sentinel/rag/eval \
        --seed ../evaluation/seeds/retrieval-seeds.jsonl --output ../evaluation/reports
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from eval.retrieval_eval import RetrievalSeedCase, load_seeds, login  # noqa: E402


# ---------- 数据模型 ----------


@dataclass
class GateCaseResult:
    question: str
    category: str
    is_negative: bool
    n_contexts: int
    bge_top1: float
    qwen3_top1: float
    bge_scores: list[float] = field(default_factory=list)
    qwen3_scores: list[float] = field(default_factory=list)
    contexts_head: list[str] = field(default_factory=list)  # 前 2 条上下文摘要（报告排查用）
    error: str | None = None


@dataclass
class GridCell:
    bge_t: float
    qwen3_t: float
    neg_total: int
    neg_cut: int
    neg_cut_rate: float
    pos_total: int
    pos_hurt: int
    pos_hurt_rate: float
    hurt_questions: list[str] = field(default_factory=list)


# ---------- 拉取链路检索结果 ----------


async def fetch_contexts(
    client: httpx.AsyncClient,
    base_url: str,
    case: RetrievalSeedCase,
    sem: asyncio.Semaphore,
    timeout: float,
) -> tuple[RetrievalSeedCase, list[str], str | None]:
    url = f"{base_url.rstrip('/')}?question={urllib.parse.quote(case.q)}"
    async with sem:
        try:
            resp = await client.get(url, timeout=timeout)
            if resp.status_code != 200:
                return case, [], f"HTTP {resp.status_code}: {resp.text[:200]}"
            data = resp.json()
            biz = data.get("code")
            if biz is not None and str(biz) != "0":
                return case, [], f"业务错误 code={biz}: {data.get('message')}"
            body = data.get("data") if isinstance(data.get("data"), dict) else data
            contexts = body.get("retrievedContexts") or []
            return case, [str(c) for c in contexts], None
        except Exception as e:  # noqa: BLE001
            return case, [], f"exception: {e!s}"


async def collect_link_results(
    eval_url: str,
    seeds: list[RetrievalSeedCase],
    concurrency: int,
    timeout: float,
    username: str,
    password: str,
) -> list[tuple[RetrievalSeedCase, list[str], str | None]]:
    sem = asyncio.Semaphore(concurrency)
    parsed = urllib.parse.urlsplit(eval_url)
    api_base = f"{parsed.scheme}://{parsed.netloc}"
    async with httpx.AsyncClient() as client:
        token = await login(client, api_base, username, password)
        print(f"[gate_eval] 登录成功,token={token[:8]}***", file=sys.stderr)
        client.headers["Authorization"] = token
        tasks = [asyncio.create_task(fetch_contexts(client, eval_url, s, sem, timeout)) for s in seeds]
        out = []
        for i, coro in enumerate(asyncio.as_completed(tasks), 1):
            r = await coro
            out.append(r)
            print(
                f"[gate_eval] {i}/{len(seeds)} ctx={len(r[1])} err={r[2] or '-'} q='{r[0].q[:30]}'",
                file=sys.stderr,
            )
        return out


# ---------- 双模型打分 ----------


def score_all(link_results, top_n: int) -> list[GateCaseResult]:
    from core.retrieval.rerank_gate import DEFAULT_BGE_THRESHOLD, DEFAULT_QWEN3_THRESHOLD, DualRerankGate

    gate = DualRerankGate()
    results: list[GateCaseResult] = []
    for case, contexts, err in link_results:
        if err:
            results.append(
                GateCaseResult(
                    question=case.q, category=case.category,
                    is_negative=not case.expected_doc_ids, n_contexts=len(contexts),
                    bge_top1=0.0, qwen3_top1=0.0, error=f"link: {err}",
                )
            )
            continue
        g = gate.gate(
            case.q, contexts,
            bge_threshold=DEFAULT_BGE_THRESHOLD, qwen3_threshold=DEFAULT_QWEN3_THRESHOLD,
            top_n=top_n,
        )
        results.append(
            GateCaseResult(
                question=case.q, category=case.category,
                is_negative=not case.expected_doc_ids, n_contexts=len(contexts),
                bge_top1=g.bge_top1, qwen3_top1=g.qwen3_top1,
                bge_scores=g.bge_scores, qwen3_scores=g.qwen3_scores,
                contexts_head=[c[:80] for c in contexts[:2]],
                error=g.error,
            )
        )
        flag = "NEG" if case.expected_doc_ids == [] else "POS"
        print(
            f"[gate_eval] {flag} bge={g.bge_top1:.4f} qwen3={g.qwen3_top1:.4f} "
            f"err={g.error or '-'} q='{case.q[:30]}'",
            file=sys.stderr,
        )
    return results


# ---------- 阈值网格扫描 ----------


def grid_scan(results: list[GateCaseResult], bge_ts: list[float], qwen3_ts: list[float]) -> list[GridCell]:
    scored = [r for r in results if r.error is None and r.n_contexts > 0]
    cells: list[GridCell] = []
    for bt in bge_ts:
        for qt in qwen3_ts:
            neg = [r for r in scored if r.is_negative]
            pos = [r for r in scored if not r.is_negative]
            neg_cut = [r for r in neg if r.bge_top1 < bt and r.qwen3_top1 < qt]
            hurt = [r for r in pos if r.bge_top1 < bt and r.qwen3_top1 < qt]
            cells.append(
                GridCell(
                    bge_t=bt, qwen3_t=qt,
                    neg_total=len(neg), neg_cut=len(neg_cut),
                    neg_cut_rate=round(len(neg_cut) / len(neg), 4) if neg else 0.0,
                    pos_total=len(pos), pos_hurt=len(hurt),
                    pos_hurt_rate=round(len(hurt) / len(pos), 4) if pos else 0.0,
                    hurt_questions=[r.question for r in hurt],
                )
            )
    return cells


def recommend(cells: list[GridCell]) -> GridCell | None:
    """误伤=0 约束下切空率最大化；无零误伤解则取误伤率最小的最大切空率格。"""
    zero = [c for c in cells if c.pos_hurt == 0]
    pool = zero or sorted(cells, key=lambda c: (c.pos_hurt_rate, -c.neg_cut_rate))
    return max(pool, key=lambda c: (c.neg_cut_rate, -c.pos_hurt_rate), default=None)


# ---------- 报告渲染 ----------


def _margin_dist(results: list[GateCaseResult]) -> str:
    """两模型 top1 分数的边际分布（分桶计数），便于目测分离度。"""
    scored = [r for r in results if r.error is None and r.n_contexts > 0]
    buckets = [0.0, 0.02, 0.05, 0.10, 0.20, 0.40, 0.60, 0.80, 1.01]
    lines = ["| 分数桶 | bge 负样本 | bge 正样本 | qwen3 负样本 | qwen3 正样本 |",
             "|---|---|---|---|---|"]
    for lo, hi in zip(buckets, buckets[1:]):
        b_neg = sum(1 for r in scored if r.is_negative and lo <= r.bge_top1 < hi)
        b_pos = sum(1 for r in scored if not r.is_negative and lo <= r.bge_top1 < hi)
        q_neg = sum(1 for r in scored if r.is_negative and lo <= r.qwen3_top1 < hi)
        q_pos = sum(1 for r in scored if not r.is_negative and lo <= r.qwen3_top1 < hi)
        lines.append(f"| [{lo:.2f},{hi:.2f}) | {b_neg} | {b_pos} | {q_neg} | {q_pos} |")
    return "\n".join(lines)


def render_report(
    results: list[GateCaseResult],
    cells: list[GridCell],
    rec: GridCell | None,
    meta: dict,
) -> str:
    scored = [r for r in results if r.error is None and r.n_contexts > 0]
    neg = [r for r in scored if r.is_negative]
    pos = [r for r in scored if not r.is_negative]
    failed = [r for r in results if r.error is not None or r.n_contexts == 0]

    lines = [
        "# 双模型 rerank 门控阈值校准报告",
        "",
        f"- 时间:{meta['time']} / 评估端点:{meta['eval_url']}",
        f"- 种子:{meta['seed']}（负样本 {len(neg)} / 正样本 {len(pos)} / 打分失败 {len(failed)}）",
        f"- 门控打分对象:rerank 后 retrievedContexts 前 {meta['top_n']} 条（与生产门控一致）",
        f"- 模型:bge-reranker-v2-m3 × Qwen3-Reranker-0.6B（AND 门控）",
        "",
        "## 分数边际分布",
        "",
        _margin_dist(results),
        "",
        "## 阈值网格（AND 门控:bge_top1 < T1 且 qwen3_top1 < T2 → 切空）",
        "",
        "| bge_t | qwen3_t | 负样本切空 | 切空率 | 正样本误伤 | 误伤率 |",
        "|---|---|---|---|---|---|",
    ]
    for c in sorted(cells, key=lambda x: (x.neg_cut_rate, -x.pos_hurt_rate), reverse=False):
        lines.append(
            f"| {c.bge_t} | {c.qwen3_t} | {c.neg_cut}/{c.neg_total} | {c.neg_cut_rate:.1%} "
            f"| {c.pos_hurt}/{c.pos_total} | {c.pos_hurt_rate:.1%} |"
        )
    if rec:
        lines += [
            "",
            f"## 推荐阈值:bge_t={rec.bge_t} qwen3_t={rec.qwen3_t}",
            f"- 负样本切空率 **{rec.neg_cut_rate:.1%}**（{rec.neg_cut}/{rec.neg_total}）",
            f"- 正样本误伤率 **{rec.pos_hurt_rate:.1%}**（{rec.pos_hurt}/{rec.pos_total}）",
        ]
        if rec.hurt_questions:
            lines += ["", "误伤题目:", *[f"- {q}" for q in rec.hurt_questions]]
    lines += ["", "## 明细（含错误样本）", "", "| 类型 | bge_top1 | qwen3_top1 | 状态 | 题目 |",
              "|---|---|---|---|---|"]
    for r in sorted(scored, key=lambda r: (not r.is_negative, r.bge_top1)):
        lines.append(
            f"| {'NEG' if r.is_negative else 'POS'} | {r.bge_top1:.4f} | {r.qwen3_top1:.4f} "
            f"| ok | {r.question[:40]} |"
        )
    for r in failed:
        reason = r.error or "no-contexts"
        lines.append(f"| {'NEG' if r.is_negative else 'POS'} | - | - | {reason[:40]} | {r.question[:40]} |")
    return "\n".join(lines) + "\n"


# ---------- 主流程 ----------


async def _run(args) -> tuple[list[GateCaseResult], list[GridCell], GridCell | None]:
    seeds = load_seeds(Path(args.seed))
    print(f"[gate_eval] 加载 {len(seeds)} 条 seed", file=sys.stderr)
    link = await collect_link_results(
        args.eval_url, seeds, args.concurrency, args.timeout, args.username, args.password
    )
    print("[gate_eval] 链路检索结果收集完毕,开始双模型打分（首次运行含模型加载）", file=sys.stderr)
    results = await asyncio.to_thread(score_all, link, args.top_n)
    cells = grid_scan(results, args.bge_thresholds, args.qwen3_thresholds)
    return results, cells, recommend(cells)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    default_seed = repo_root / "evaluation" / "seeds" / "retrieval-seeds.jsonl"
    default_output = repo_root / "evaluation" / "reports"

    parser = argparse.ArgumentParser(description="双模型 rerank 门控阈值校准")
    parser.add_argument("--eval-url", default="http://localhost:9090/api/sentinel/rag/eval")
    parser.add_argument("--seed", default=str(default_seed))
    parser.add_argument("--output", default=str(default_output))
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--top-n", type=int, default=8, help="每题取 rerank 后前 N 条打分")
    parser.add_argument("--name", default="rerank-gate")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin")
    parser.add_argument(
        "--bge-thresholds", type=float, nargs="+",
        default=[0.02, 0.05, 0.10, 0.15, 0.20, 0.30],
    )
    parser.add_argument(
        "--qwen3-thresholds", type=float, nargs="+",
        default=[0.20, 0.30, 0.40, 0.50, 0.60],
    )
    args = parser.parse_args()

    t0 = time.perf_counter()
    results, cells, rec = asyncio.run(_run(args))

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    meta = {
        "time": timestamp,
        "eval_url": args.eval_url,
        "seed": str(args.seed),
        "top_n": args.top_n,
    }
    json_path = output_dir / f"{args.name}-{timestamp}.json"
    json_path.write_text(
        json.dumps(
            {
                "meta": meta,
                "recommend": asdict(rec) if rec else None,
                "grid": [asdict(c) for c in cells],
                "cases": [asdict(r) for r in results],
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    md_path = output_dir / f"{args.name}-{timestamp}.md"
    md_path.write_text(render_report(results, cells, rec, meta), encoding="utf-8")
    print(f"[gate_eval] JSON:{json_path}", file=sys.stderr)
    print(f"[gate_eval] MD:{md_path}", file=sys.stderr)
    if rec:
        print(
            f"[gate_eval] 推荐:bge_t={rec.bge_t} qwen3_t={rec.qwen3_t} "
            f"切空率={rec.neg_cut_rate:.1%} 误伤率={rec.pos_hurt_rate:.1%}",
            file=sys.stderr,
        )
    print(f"[gate_eval] 总耗时 {time.perf_counter() - t0:.0f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
