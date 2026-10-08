"""基于 Java 端 EvalController 的检索质量评测（v3 离线指标：Recall@5 / HitRate@5 / MRR）。

读取 seed-retrieval.jsonl,对每条 q 调用
    GET http://localhost:9090/api/sentinel/rag/eval?question=<q>
拿到检索文档业务码列表(retrievedDocIds,与 expected_doc_ids 同为 doc_name 剥后缀),统计:

- Recall@5: |top5 ∩ expected| / |expected| 的宏平均
- HitRate@5: top5 至少命中一个 expected 的比例
- MRR: expected 中第一个命中文档排名倒数的平均
- 检索延迟 P50 / P90 / P95

输出 JSON + Markdown 报告。

运行:
    python -m eval.retrieval_eval
或:
    python -m eval.retrieval_eval --eval-url http://localhost:9090/api/sentinel/rag/eval \
        --seed eval/data/seed-retrieval.jsonl --output eval/reports
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

# 支持脚本方式运行: python eval\retrieval_eval.py（把 agent-python 目录加入 sys.path）
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------- 鉴权 ----------


async def login(client: httpx.AsyncClient, api_base: str, username: str, password: str) -> str:
    """登录 Java 网关获取 token(裸 token,不带 Bearer 前缀)。"""
    resp = await client.post(
        f"{api_base.rstrip('/')}/api/sentinel/auth/login",
        json={"username": username, "password": password},
        timeout=15.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    # 响应结构:{"code": "0", "message": null, "data": {"token": "...", ...}}
    if str(payload.get("code")) != "0":
        raise RuntimeError(f"登录失败:code={payload.get('code')}, message={payload.get('message')}")
    token = (payload.get("data") or {}).get("token")
    if not token:
        raise RuntimeError(f"登录响应中无 token:{payload}")
    return token


# ---------- 数据模型 ----------


@dataclass
class RetrievalSeedCase:
    q: str
    expected_doc_ids: list[str] = field(default_factory=list)
    category: str = "general"


@dataclass
class RetrievalCaseResult:
    question: str
    category: str
    expected_doc_ids: list[str]
    retrieved_doc_ids: list[str]
    top5_doc_ids: list[str]
    recall_at_5: float
    hit_at_5: bool
    reciprocal_rank: float
    latency_ms: float
    rewritten_query: str = ""
    error: str | None = None


@dataclass
class RetrievalEvalSummary:
    total: int
    successful: int
    failed: int
    positive_total: int
    negative_total: int
    recall_at_5: float
    hit_rate_at_5: float
    mrr: float
    negative_retrieval_rate: float
    latency_p50: float
    latency_p90: float
    latency_p95: float
    latency_avg: float
    cases: list[RetrievalCaseResult]


# ---------- IO ----------


def load_seeds(path: Path) -> list[RetrievalSeedCase]:
    if not path.exists():
        raise FileNotFoundError(f"seed 文件不存在:{path}")
    out: list[RetrievalSeedCase] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        out.append(
            RetrievalSeedCase(
                q=str(item["q"]).strip(),
                expected_doc_ids=[str(x) for x in item.get("expected_doc_ids", [])],
                category=str(item.get("category", "general")),
            )
        )
    if not out:
        raise ValueError(f"seed 文件为空:{path}")
    return out


# ---------- 指标计算（确定性，无 LLM 依赖） ----------


def _score_case(expected: list[str], retrieved: list[str], k: int = 5) -> tuple[float, bool, float]:
    """返回 (recall@k, hit@k, reciprocal_rank)。

    expected 为空（负样本,库中本无相关信息）时返回 0 值:
    聚合时负样本不参与 Recall/MRR 统计,单独计 negative_retrieval_rate。
    """
    top_k = retrieved[:k]
    expected_set = set(expected)
    if not expected_set:
        return 0.0, False, 0.0
    hits = len(expected_set & set(top_k))
    recall = hits / len(expected_set)
    hit = hits > 0
    rr = 0.0
    for idx, doc in enumerate(top_k, 1):
        if doc in expected_set:
            rr = 1.0 / idx
            break
    return recall, hit, rr


# ---------- 单条评测 ----------


async def _eval_one(
    client: httpx.AsyncClient,
    base_url: str,
    case: RetrievalSeedCase,
    timeout: float,
    rewrite: bool = False,
) -> RetrievalCaseResult:
    url = f"{base_url.rstrip('/')}?question={urllib.parse.quote(case.q)}"
    if rewrite:
        url += "&rewrite=true"
    t0 = time.perf_counter()
    try:
        resp = await client.get(url, timeout=timeout)
        latency = (time.perf_counter() - t0) * 1000.0
        if resp.status_code != 200:
            return RetrievalCaseResult(
                question=case.q,
                category=case.category,
                expected_doc_ids=case.expected_doc_ids,
                retrieved_doc_ids=[],
                top5_doc_ids=[],
                recall_at_5=0.0,
                hit_at_5=False,
                reciprocal_rank=0.0,
                latency_ms=round(latency, 2),
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )
        data = resp.json()
    except Exception as e:
        latency = (time.perf_counter() - t0) * 1000.0
        return RetrievalCaseResult(
            question=case.q,
            category=case.category,
            expected_doc_ids=case.expected_doc_ids,
            retrieved_doc_ids=[],
            top5_doc_ids=[],
            recall_at_5=0.0,
            hit_at_5=False,
            reciprocal_rank=0.0,
            latency_ms=round(latency, 2),
            error=f"exception: {e!s}",
        )

    # Result 包装校验:{"code": "0", "message": ..., "data": {...}}
    biz_code = data.get("code")
    if biz_code is not None and str(biz_code) != "0":
        return RetrievalCaseResult(
            question=case.q,
            category=case.category,
            expected_doc_ids=case.expected_doc_ids,
            retrieved_doc_ids=[],
            top5_doc_ids=[],
            recall_at_5=0.0,
            hit_at_5=False,
            reciprocal_rank=0.0,
            latency_ms=round(latency, 2),
            error=f"业务错误 code={biz_code}: {data.get('message')}",
        )
    body = data.get("data") if isinstance(data.get("data"), dict) else data
    # 兼容字段(后端用 camelCase)
    retrieved = body.get("retrievedDocIds") or body.get("retrieved_doc_ids") or []
    # MRR 口径修正：/debug/retrieval 的 retrievedDocIds 是 sorted set（无序），
    # 排名信息必须取 final chunks（按 score 降序）剥后缀的保序 doc 序列
    ordered: list[str] = []
    final_chunks = body.get("final") or []
    if isinstance(final_chunks, list) and final_chunks:
        for c in final_chunks:
            name = ""
            if isinstance(c, dict):
                name = str(c.get("doc_name") or "")
            else:
                name = str(getattr(c, "doc_name", "") or "")
            if name:
                ordered.append(name.rsplit(".", 1)[0] if name.rfind(".") > 0 else name)
    seen_set: set[str] = set()
    ordered = [d for d in ordered if not (d in seen_set or seen_set.add(d))]
    if ordered:
        retrieved = ordered
    rewritten = str(body.get("rewritten_query") or "")

    recall, hit, rr = _score_case(case.expected_doc_ids, list(retrieved), k=5)
    return RetrievalCaseResult(
        question=case.q,
        category=case.category,
        expected_doc_ids=case.expected_doc_ids,
        retrieved_doc_ids=list(retrieved),
        top5_doc_ids=list(retrieved[:5]),
        recall_at_5=round(recall, 4),
        hit_at_5=hit,
        reciprocal_rank=round(rr, 4),
        latency_ms=round(latency, 2),
        rewritten_query=rewritten,
        error=None,
    )


# ---------- 主流程 ----------


async def _run(
    base_url: str,
    seeds: list[RetrievalSeedCase],
    concurrency: int,
    timeout: float,
    username: str,
    password: str,
    rewrite: bool = False,
) -> RetrievalEvalSummary:
    sem = asyncio.Semaphore(concurrency)
    results: list[RetrievalCaseResult] = []

    # 从 eval-url 提取 origin 作为登录 base
    parsed = urllib.parse.urlsplit(base_url)
    api_base = f"{parsed.scheme}://{parsed.netloc}"

    async with httpx.AsyncClient() as client:
        try:
            token = await login(client, api_base, username, password)
            print(f"[retrieval_eval] 登录成功,token={token[:8]}***", file=sys.stderr)
            client.headers["Authorization"] = token
        except Exception as exc:  # noqa: BLE001
            print(f"[retrieval_eval] 登录跳过(直连无鉴权诊断端点): {exc}", file=sys.stderr)

        async def _one(c: RetrievalSeedCase) -> RetrievalCaseResult:
            async with sem:
                return await _eval_one(client, base_url, c, timeout, rewrite)

        tasks = [asyncio.create_task(_one(c)) for c in seeds]
        for i, coro in enumerate(asyncio.as_completed(tasks), 1):
            r = await coro
            results.append(r)
            print(
                f"[retrieval_eval] {i}/{len(seeds)} hit5={'✓' if r.hit_at_5 else '✗'} "
                f"recall5={r.recall_at_5:.2f} rr={r.reciprocal_rank:.2f} lat={r.latency_ms:.0f}ms "
                f"q='{r.question[:30]}'",
                file=sys.stderr,
            )

    successful = [r for r in results if r.error is None]
    failed = [r for r in results if r.error is not None]
    positive = [r for r in successful if r.expected_doc_ids]
    negative = [r for r in successful if not r.expected_doc_ids]
    latencies = sorted([r.latency_ms for r in successful])

    def _pct(p: float) -> float:
        if not latencies:
            return 0.0
        idx = max(0, min(len(latencies) - 1, int(round((p / 100.0) * (len(latencies) - 1)))))
        return round(latencies[idx], 2)

    neg_retrieved = sum(1 for r in negative if r.top5_doc_ids)
    return RetrievalEvalSummary(
        total=len(results),
        successful=len(successful),
        failed=len(failed),
        positive_total=len(positive),
        negative_total=len(negative),
        recall_at_5=round(statistics.mean([r.recall_at_5 for r in positive]), 4) if positive else 0.0,
        hit_rate_at_5=round(sum(1 for r in positive if r.hit_at_5) / len(positive), 4) if positive else 0.0,
        mrr=round(statistics.mean([r.reciprocal_rank for r in positive]), 4) if positive else 0.0,
        negative_retrieval_rate=round(neg_retrieved / len(negative), 4) if negative else 0.0,
        latency_p50=_pct(50),
        latency_p90=_pct(90),
        latency_p95=_pct(95),
        latency_avg=round(statistics.mean(latencies), 2) if latencies else 0.0,
        cases=results,
    )


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    default_seed = repo_root / "evaluation" / "seeds" / "retrieval-seeds.jsonl"
    default_output = repo_root / "evaluation" / "reports"

    parser = argparse.ArgumentParser(description="基于 Java EvalController 的检索质量评测")
    parser.add_argument(
        "--eval-url",
        default="http://localhost:8001/debug/retrieval",
        help="检索评测端点（默认 agent-python 诊断端点；亦可指向 9090 Java EvalController）",
    )
    parser.add_argument("--seed", default=str(default_seed), help="seed jsonl 路径")
    parser.add_argument("--output", default=str(default_output), help="报告输出目录")
    parser.add_argument("--concurrency", type=int, default=4, help="并发请求数")
    parser.add_argument("--timeout", type=float, default=120.0, help="单条超时秒数")
    parser.add_argument("--name", default="retrieval", help="报告名前缀")
    parser.add_argument("--username", default="admin", help="网关登录用户名")
    parser.add_argument("--password", default="admin", help="网关登录密码")
    parser.add_argument(
        "--rewrite",
        dest="rewrite",
        action="store_true",
        default=True,
        help="评测前先过 QueryRewriter（对齐生产链路口径，每条 +1 次 LLM 调用；默认开启）",
    )
    parser.add_argument(
        "--no-rewrite",
        dest="rewrite",
        action="store_false",
        help="关闭改写（原句直检，仅用于对照实验）",
    )
    parser.add_argument("--category", default="", help="按 seed category 子串过滤（逗号分隔多个），空=全部")
    parser.add_argument("--docs", default="", help="按 expected_doc_ids 子串过滤（逗号分隔多个），空=全部")
    parser.add_argument("--limit", type=int, default=0, help="仅取前 N 条，0=不限")
    args = parser.parse_args()

    seeds = load_seeds(Path(args.seed))
    cats = [c.strip() for c in args.category.split(",") if c.strip()]
    docs = [d.strip() for d in args.docs.split(",") if d.strip()]
    if cats:
        seeds = [s for s in seeds if any(c in (s.category or "") for c in cats)]
    if docs:
        seeds = [s for s in seeds if any(d in doc for doc in s.expected_doc_ids for d in docs)]
    if args.limit > 0:
        seeds = seeds[: args.limit]
    print(f"[retrieval_eval] 加载 {len(seeds)} 条 seed from {args.seed}", file=sys.stderr)

    summary = asyncio.run(
        _run(args.eval_url, seeds, args.concurrency, args.timeout, args.username, args.password, args.rewrite)
    )

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = output_dir / f"{args.name}-{timestamp}.json"
    json_path.write_text(
        json.dumps(
            {
                "summary": {k: v for k, v in asdict(summary).items() if k != "cases"},
                "cases": [asdict(c) for c in summary.cases],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[retrieval_eval] JSON 报告:{json_path}", file=sys.stderr)

    from eval.report_renderer import render_retrieval_report  # noqa: PLC0415

    md_path = output_dir / f"{args.name}-{timestamp}.md"
    md_path.write_text(
        render_retrieval_report(summary, eval_url=args.eval_url, seed_path=str(args.seed)),
        encoding="utf-8",
    )
    print(f"[retrieval_eval] Markdown 报告:{md_path}", file=sys.stderr)

    if summary.recall_at_5 < 0.85 and summary.successful > 0:
        print(
            f"[retrieval_eval] 警告:Recall@5 {summary.recall_at_5:.2%} 低于 85% 目标",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
