"""基于 Java 端 EvalController 的意图识别评测。

读取 seed-intent.jsonl,对每条 q 调用
    GET http://localhost:9090/api/ragent/rag/eval?question=<q>
拿到意图 Top-K 列表,与真值 intent_l2 比对,统计:

- Top-1 accuracy:模型给的第一名 intent 命中真值的比例
- Top-3 accuracy:模型给的前三名 intent 包含真值的比例
- 意图延迟 P50 / P90 / P95
- 每条用例的详细:question / predicted_top1 / predicted_top3 / ground_truth / hit_top1 / hit_top3 / latency_ms

输出 JSON + Markdown 报告。

运行:
    python -m eval.intent_eval
或:
    python -m eval.intent_eval --eval-url http://localhost:9090/api/ragent/rag/eval \
        --seed eval/data/seed-intent.jsonl --output eval/reports
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
from typing import Any

import httpx


# ---------- 数据模型 ----------


@dataclass
class SeedCase:
    q: str
    intent_l2: str
    expected_doc_ids: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    question: str
    ground_truth: str
    predicted_top1: str
    predicted_top3: list[str]
    has_sub_intent: bool
    hit_top1: bool
    hit_top3: bool
    latency_ms: float
    intent_leaf_ids: list[str]
    error: str | None = None


@dataclass
class EvalSummary:
    total: int
    successful: int
    failed: int
    top1_accuracy: float
    top3_accuracy: float
    latency_p50: float
    latency_p90: float
    latency_p95: float
    latency_avg: float
    sub_intent_rate: float
    cases: list[CaseResult]


# ---------- IO ----------


def load_seeds(path: Path) -> list[SeedCase]:
    if not path.exists():
        raise FileNotFoundError(f"seed 文件不存在:{path}")
    out: list[SeedCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        out.append(
            SeedCase(
                q=str(item["q"]).strip(),
                intent_l2=str(item["intent_l2"]).strip(),
                expected_doc_ids=[str(x) for x in item.get("expected_doc_ids", [])],
            )
        )
    if not out:
        raise ValueError(f"seed 文件为空:{path}")
    return out


# ---------- 单条评测 ----------


async def _eval_one(
    client: httpx.AsyncClient,
    base_url: str,
    case: SeedCase,
    timeout: float,
) -> CaseResult:
    url = f"{base_url.rstrip('/')}?question={urllib.parse.quote(case.q)}"
    t0 = time.perf_counter()
    try:
        resp = await client.get(url, timeout=timeout)
        latency = (time.perf_counter() - t0) * 1000.0
        if resp.status_code != 200:
            return CaseResult(
                question=case.q,
                ground_truth=case.intent_l2,
                predicted_top1="",
                predicted_top3=[],
                has_sub_intent=False,
                hit_top1=False,
                hit_top3=False,
                latency_ms=round(latency, 2),
                intent_leaf_ids=[],
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )
        data = resp.json()
    except Exception as e:
        latency = (time.perf_counter() - t0) * 1000.0
        return CaseResult(
            question=case.q,
            ground_truth=case.intent_l2,
            predicted_top1="",
            predicted_top3=[],
            has_sub_intent=False,
            hit_top1=False,
            hit_top3=False,
            latency_ms=round(latency, 2),
            intent_leaf_ids=[],
            error=f"exception: {e!s}",
        )

    # 兼容字段(后端用 camelCase)
    intent_leaf_ids = data.get("intentLeafIds") or data.get("intent_leaf_ids") or []
    sub_intents = data.get("subIntents") or data.get("sub_intents") or []

    top1 = intent_leaf_ids[0] if intent_leaf_ids else ""
    top3 = list(intent_leaf_ids[:3])

    return CaseResult(
        question=case.q,
        ground_truth=case.intent_l2,
        predicted_top1=top1,
        predicted_top3=top3,
        has_sub_intent=len(sub_intents) > 0,
        hit_top1=(top1 == case.intent_l2),
        hit_top3=case.intent_l2 in top3,
        latency_ms=round(latency, 2),
        intent_leaf_ids=list(intent_leaf_ids),
        error=None,
    )


# ---------- 主流程 ----------


async def _run(
    base_url: str,
    seeds: list[SeedCase],
    concurrency: int,
    timeout: float,
) -> EvalSummary:
    sem = asyncio.Semaphore(concurrency)
    results: list[CaseResult] = []

    async with httpx.AsyncClient() as client:
        async def _one(c: SeedCase) -> CaseResult:
            async with sem:
                return await _eval_one(client, base_url, c, timeout)

        tasks = [asyncio.create_task(_one(c)) for c in seeds]
        for i, coro in enumerate(asyncio.as_completed(tasks), 1):
            r = await coro
            results.append(r)
            print(
                f"[intent_eval] {i}/{len(seeds)} top1={'✓' if r.hit_top1 else '✗'} "
                f"top3={'✓' if r.hit_top3 else '✗'} lat={r.latency_ms:.0f}ms q='{r.question[:30]}'",
                file=sys.stderr,
            )

    successful = [r for r in results if r.error is None]
    failed = [r for r in results if r.error is not None]
    hits_top1 = [r for r in successful if r.hit_top1]
    hits_top3 = [r for r in successful if r.hit_top3]
    latencies = sorted([r.latency_ms for r in successful])
    sub_intent_n = sum(1 for r in successful if r.has_sub_intent)

    def _pct(p: float) -> float:
        if not latencies:
            return 0.0
        idx = max(0, min(len(latencies) - 1, int(round((p / 100.0) * (len(latencies) - 1)))))
        return round(latencies[idx], 2)

    return EvalSummary(
        total=len(results),
        successful=len(successful),
        failed=len(failed),
        top1_accuracy=round(len(hits_top1) / len(successful), 4) if successful else 0.0,
        top3_accuracy=round(len(hits_top3) / len(successful), 4) if successful else 0.0,
        latency_p50=_pct(50),
        latency_p90=_pct(90),
        latency_p95=_pct(95),
        latency_avg=round(statistics.mean(latencies), 2) if latencies else 0.0,
        sub_intent_rate=round(sub_intent_n / len(successful), 4) if successful else 0.0,
        cases=results,
    )


def main() -> int:
    eval_dir = Path(__file__).resolve().parent
    default_seed = eval_dir / "data" / "seed-intent.jsonl"
    default_output = eval_dir / "reports"

    parser = argparse.ArgumentParser(description="基于 Java EvalController 的意图评测")
    parser.add_argument(
        "--eval-url",
        default="http://localhost:9090/api/ragent/rag/eval",
        help="Java 端 EvalController 的 URL",
    )
    parser.add_argument("--seed", default=str(default_seed), help="seed jsonl 路径")
    parser.add_argument("--output", default=str(default_output), help="报告输出目录")
    parser.add_argument("--concurrency", type=int, default=4, help="并发请求数")
    parser.add_argument("--timeout", type=float, default=120.0, help="单条超时秒数")
    parser.add_argument("--name", default="intent", help="报告名前缀")
    args = parser.parse_args()

    seeds = load_seeds(Path(args.seed))
    print(f"[intent_eval] 加载 {len(seeds)} 条 seed from {args.seed}", file=sys.stderr)

    summary = asyncio.run(
        _run(args.eval_url, seeds, args.concurrency, args.timeout)
    )

    # 写 JSON
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = output_dir / f"{args.name}-{timestamp}.json"
    json_path.write_text(
        json.dumps(
            {
                "summary": {
                    **{k: v for k, v in asdict(summary).items() if k != "cases"},
                },
                "cases": [asdict(c) for c in summary.cases],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[intent_eval] JSON 报告:{json_path}", file=sys.stderr)

    # 写 Markdown
    from eval.report_renderer import render_intent_report

    md_path = output_dir / f"{args.name}-{timestamp}.md"
    md_path.write_text(
        render_intent_report(summary, eval_url=args.eval_url, seed_path=str(args.seed)),
        encoding="utf-8",
    )
    print(f"[intent_eval] Markdown 报告:{md_path}", file=sys.stderr)

    if summary.top1_accuracy < 0.9 and summary.successful > 0:
        print(
            f"[intent_eval] 警告:Top-1 准确率 {summary.top1_accuracy:.2%} 低于 90% 目标",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
