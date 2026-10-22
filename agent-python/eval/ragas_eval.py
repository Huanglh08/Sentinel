"""RAGAS 四指标独立评测（重新采集，不读取既有 e2e 报告）。

链路与生产同构：
    GET {gateway}/rag/v3/chat?question=...   → SSE，采集答案全文
    GET {agent}/debug/retrieval?question=... → 采集最终进入 prompt 的 chunk 全文

指标（ragas 0.4.x collections API，逐条 ascore）：
    - Faithfulness                        answer + contexts          不需要参考答案
    - ContextPrecisionWithoutReference    question + answer + contexts 不需要参考答案
    - AnswerRelevancy                     question + answer          需要 embedding
    - ContextRecall                       question + contexts + reference  需要参考答案

ContextRecall 只有在 --reference 提供参考答案 jsonl 时才参与，否则跳过并在报告里标注。

运行（在 agent-python 目录）:
    python -m eval.ragas_eval --limit 3
    python -m eval.ragas_eval --limit 20 --concurrency 2
    python -m eval.ragas_eval --reference eval/data/reference-answers.jsonl
"""
from __future__ import annotations

import os

# scikit-network→numpy 在本机默认线程数下会触发 OpenBLAS "Memory allocation still failed"，
# 且报错后进程静默无输出。必须在任何 numpy 导入之前压到单线程。
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import argparse
import asyncio
import json
import statistics
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

_AGENT_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_AGENT_ROOT), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from eval.e2e_eval import (  # noqa: E402
    E2ESeedCase,
    REFUSAL_MARKS,
    _login,
    _source_doc_names,
    _stream_chat,
)

# 拒答判定只在 RAGAS 侧加严：e2e_eval._is_refused 是 e2e/红队/判准校准共用的口径，
# 这里不碰它，只借用它的特征词表，再补上软拒答说法。
_EXTRA_REFUSAL_MARKS = ("当前可用信息中没有", "当前可用信息未", "无法列出", "资料中没有")
_REFUSAL_LEAD_CHARS = 80


def _refused(answer: str) -> bool:
    """按答案开头定性。

    答案中段出现「资料中未提及」通常只是局部覆盖说明，整段仍在作答（表格、清单
    都给全了），按全文命中会把这类正常答案误判为拒答，所以只看开头一段。
    """
    text = (answer or "").strip()
    if len(text) < 2:
        return True
    head = text[:_REFUSAL_LEAD_CHARS]
    return any(mark in head for mark in REFUSAL_MARKS + _EXTRA_REFUSAL_MARKS)

# 评测模型与被测 agent（通义系）不同家族，沿用现有 judge 选型规则
# 百炼端点无裸名 deepseek-v4-flash（404 Model not exist），必须带版本后缀
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "deepseek-v4.1-flash-1017")
JUDGE_BASE_URL = os.getenv("JUDGE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
EMBED_MODEL = os.getenv("RAGAS_EMBED_MODEL", "Qwen/Qwen3-Embedding-8B")
EMBED_BASE_URL = os.getenv("RAGAS_EMBED_BASE_URL", "https://api.siliconflow.cn/v1")

METRIC_NAMES = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")


@dataclass
class RagasCaseResult:
    question: str
    category: str
    is_attack: bool
    answer: str
    answer_chars: int
    refused: bool
    doc_names: list[str]
    contexts: list[str]
    scores: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    chat_ms: float = 0.0
    retrieval_ms: float = 0.0


# ---------- RAGAS 组件 ----------


def _build_llm():
    from openai import AsyncOpenAI
    from ragas.llms import llm_factory

    key = os.getenv("BAILIAN_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or os.getenv("JUDGE_API_KEY")
    if not key:
        raise RuntimeError("缺少 BAILIAN_API_KEY / DASHSCOPE_API_KEY / JUDGE_API_KEY，无法调用评测模型")
    client = AsyncOpenAI(api_key=key, base_url=JUDGE_BASE_URL, timeout=180.0, max_retries=3)
    # max_tokens 必须显式放大：默认值下 faithfulness 的结构化输出会被截断（IncompleteOutputException）。
    # 百炼 deepseek 系列默认开思考模式，思考 token 也计入输出，故同时关掉（与 judge config 同口径）。
    return llm_factory(
        JUDGE_MODEL,
        provider="openai",
        client=client,
        temperature=0,
        max_tokens=4096,
        extra_body={"enable_thinking": False},
    )


def _build_embeddings():
    from openai import AsyncOpenAI
    from ragas.embeddings import OpenAIEmbeddings

    key = os.getenv("SILICONFLOW_API_KEY")
    if not key:
        raise RuntimeError("缺少 SILICONFLOW_API_KEY，AnswerRelevancy 无法计算向量相似度")
    client = AsyncOpenAI(api_key=key, base_url=EMBED_BASE_URL, timeout=120.0, max_retries=3)
    return OpenAIEmbeddings(client=client, model=EMBED_MODEL)


def _build_metrics(llm, embeddings, with_reference: bool) -> dict:
    from ragas.metrics.collections import (
        AnswerRelevancy,
        ContextPrecisionWithoutReference,
        Faithfulness,
    )

    metrics: dict = {
        "faithfulness": Faithfulness(llm),
        "answer_relevancy": AnswerRelevancy(llm, embeddings),
        "context_precision": ContextPrecisionWithoutReference(llm),
    }
    if with_reference:
        from ragas.metrics.collections import ContextRecall

        metrics["context_recall"] = ContextRecall(llm)
    return metrics


# ---------- 采集 ----------


async def _fetch_contexts(
    client: httpx.AsyncClient, agent_base: str, question: str, timeout: float
) -> tuple[list[str], float]:
    """取 /debug/retrieval 的 final chunk 全文（SSE sources.excerpt 被截断到 100 字，不能用作 RAGAS 上下文）。"""
    url = f"{agent_base}/debug/retrieval?{urllib.parse.urlencode({'question': question, 'rewrite': 'true'})}"
    t0 = time.perf_counter()
    try:
        resp = await client.get(url, timeout=min(timeout, 60.0))
        resp.raise_for_status()
        payload = resp.json()
        texts = [
            str(c.get("text") or "").strip()
            for c in (payload.get("final") or [])
            if str(c.get("text") or "").strip()
        ]
        return texts, (time.perf_counter() - t0) * 1000.0
    except Exception as e:  # noqa: BLE001
        print(f"[ragas_eval] debug/retrieval 失败: {e}", file=sys.stderr)
        return [], (time.perf_counter() - t0) * 1000.0


async def _collect(
    seeds: list[E2ESeedCase],
    *,
    gateway: str,
    agent: str,
    username: str,
    password: str,
    concurrency: int,
    timeout: float,
) -> list[RagasCaseResult]:
    async with httpx.AsyncClient() as client:
        token = await _login(client, gateway, username, password)
        print(f"[ragas 采集] 0/{len(seeds)} 登录成功，开始逐题取回答与上下文", file=sys.stderr, flush=True)
        sem = asyncio.Semaphore(concurrency)
        done = 0

        async def one(seed: E2ESeedCase) -> RagasCaseResult:
            nonlocal done
            async with sem:
                t0 = time.perf_counter()
                stream = await _stream_chat(client, f"{gateway}/rag/v3/chat", token, seed.q, timeout)
                chat_ms = (time.perf_counter() - t0) * 1000.0
                contexts, retrieval_ms = await _fetch_contexts(client, agent, seed.q, timeout)
            done += 1
            print(f"[ragas 采集] {done}/{len(seeds)} {seed.q[:40]}", file=sys.stderr, flush=True)
            answer = stream["full_text"]
            return RagasCaseResult(
                question=seed.q,
                category=seed.category,
                is_attack=seed.expected_refusal,
                answer=answer,
                answer_chars=len(answer),
                refused=_refused(answer),
                doc_names=_source_doc_names(stream["sources"]),
                contexts=contexts,
                errors=[stream["error"]] if stream["error"] else [],
                chat_ms=chat_ms,
                retrieval_ms=retrieval_ms,
            )

        return list(await asyncio.gather(*(one(s) for s in seeds)))


# ---------- 打分 ----------


async def _score(
    cases: list[RagasCaseResult],
    *,
    metrics: dict,
    references: dict[str, str],
    concurrency: int,
) -> None:
    sem = asyncio.Semaphore(concurrency)

    async def apply(metric_name: str, case: RagasCaseResult) -> None:
        if not case.answer.strip():
            case.errors.append(f"{metric_name}: 空答案，跳过")
            return
        if metric_name in ("context_precision", "context_recall") and not case.contexts:
            case.errors.append(f"{metric_name}: 无检索上下文，跳过")
            return
        try:
            async with sem:
                if metric_name == "faithfulness":
                    r = await metrics[metric_name].ascore(
                        user_input=case.question,
                        response=case.answer,
                        retrieved_contexts=case.contexts,
                    )
                elif metric_name == "answer_relevancy":
                    r = await metrics[metric_name].ascore(
                        user_input=case.question, response=case.answer
                    )
                elif metric_name == "context_precision":
                    r = await metrics[metric_name].ascore(
                        user_input=case.question,
                        response=case.answer,
                        retrieved_contexts=case.contexts,
                    )
                else:
                    ref = references.get(case.question, "").strip()
                    if not ref:
                        # 负例本就没有参考答案（知识库查无答案），只统计拒答是否正确，静默跳过
                        return
                    r = await metrics[metric_name].ascore(
                        user_input=case.question,
                        retrieved_contexts=case.contexts,
                        reference=ref,
                    )
            case.scores[metric_name] = float(r.value)
        except Exception as e:  # noqa: BLE001
            case.errors.append(f"{metric_name}: {type(e).__name__}: {e}")

    tasks = []
    for case in cases:
        for name in metrics:
            tasks.append(apply(name, case))
    base = len(cases)
    total = base + len(tasks)
    print(
        f"[ragas 打分] {base}/{total} 采集完成，开始打分（{len(tasks)} 次调用，并发 {concurrency}）",
        file=sys.stderr,
        flush=True,
    )
    for i, coro in enumerate(asyncio.as_completed(tasks), 1):
        await coro
        # 采集阶段已经数到 base，打分接着往下数：进度条单调，不因阶段切换回退
        print(f"[ragas 打分] {base + i}/{total} {i}/{len(tasks)}", file=sys.stderr, flush=True)


# ---------- 汇总与输出 ----------


def _metric_stats(cases: list[RagasCaseResult], metric_names: list[str]) -> dict:
    out: dict = {}
    for name in metric_names:
        vals = [c.scores[name] for c in cases if name in c.scores]
        out[name] = {
            "count": len(vals),
            "mean": round(statistics.mean(vals), 4) if vals else None,
            "median": round(statistics.median(vals), 4) if vals else None,
            "min": round(min(vals), 4) if vals else None,
            "max": round(max(vals), 4) if vals else None,
        }
    return out


def _summarize(cases: list[RagasCaseResult], metric_names: list[str]) -> dict:
    # 分组按题目意图（负例该拒答），不按判定结果：判定是启发式，用来出「负例是否正确
    # 拒答」这个数，不能拿它决定哪些样本进指标——否则一辆判定错就把正常答案挪出分母。
    normal = [c for c in cases if not c.is_attack]
    refusal = [c for c in cases if c.is_attack]
    err_cases = [c for c in cases if c.errors]
    return {
        "total": len(cases),
        "groups": {"normal": len(normal), "refusal": len(refusal)},
        "refusal_check": {
            "negatives": len(refusal),
            "negatives_refused": sum(1 for c in refusal if c.refused),
            "positives": len(normal),
            "positives_refused": sum(1 for c in normal if c.refused),
        },
        "metrics": _metric_stats(cases, metric_names),
        "metrics_normal": _metric_stats(normal, metric_names),
        "metrics_refusal": _metric_stats(refusal, metric_names),
        "cases_with_errors": len(err_cases),
        "error_samples": [{"question": c.question, "errors": c.errors} for c in err_cases[:10]],
    }


def _write_reports(payload: dict, output: Path, name: str, ts: str) -> tuple[Path, Path]:
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / f"{name}-{ts}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    s = payload["summary"]
    rc = s.get("refusal_check") or {}
    lines = [
        f"# RAGAS 评测 {ts}",
        "",
        f"- 样本数：{s['total']}（正常 {s['groups']['normal']} / 拒答 {s['groups']['refusal']}）",
        f"- 拒答判定：负例拒答 {rc.get('negatives_refused', 0)}/{rc.get('negatives', 0)}，"
        f"正常题被拒答 {rc.get('positives_refused', 0)}/{rc.get('positives', 0)}",
        f"- 评测模型：{payload['config']['judge_model']} @ {payload['config']['judge_base_url']}",
        f"- 向量模型：{payload['config']['embed_model']}",
        f"- 参考答案：{'已提供' if payload['config']['with_reference'] else '未提供（跳过 context_recall）'}",
    ]
    for title, key in (
        ("全部样本", "metrics"),
        ("正常样本", "metrics_normal"),
        ("拒答样本（RAGAS 会把正确拒答判 0，仅作对照）", "metrics_refusal"),
    ):
        lines += [
            "",
            f"## {title}",
            "",
            "| 指标 | 样本 | 均值 | 中位数 | 最小 | 最大 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for metric_name, stat in s[key].items():
            if stat["count"]:
                lines.append(
                    f"| {metric_name} | {stat['count']} | {stat['mean']} | "
                    f"{stat['median']} | {stat['min']} | {stat['max']} |"
                )
            else:
                lines.append(f"| {metric_name} | 0 | - | - | - | - |")
    if s["error_samples"]:
        lines += ["", "## 异常样本", ""]
        for e in s["error_samples"]:
            lines.append(f"- `{e['question'][:60]}`: {'; '.join(e['errors'])}")
    md_path = output / f"{name}-{ts}.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def _load_references(path: str) -> dict[str, str]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"参考答案文件不存在:{p}")
    refs: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        q = str(item.get("q") or "").strip()
        ref = str(item.get("reference") or "").strip()
        if q and ref:
            refs[q] = ref
    return refs


def _load_seed_file(path: Path) -> tuple[list[E2ESeedCase], dict[str, str]]:
    """读 RAGAS 数据集（ragas-seeds.jsonl）：样本 + 每题自带的参考答案。"""
    if not path.exists():
        raise FileNotFoundError(f"数据集不存在:{path}")
    seeds: list[E2ESeedCase] = []
    refs: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        q = str(item.get("q", "")).strip()
        if not q:
            continue
        seeds.append(
            E2ESeedCase(
                q=q,
                expected_refusal=bool(item.get("expected_refusal", False)),
                expected_doc_ids=[str(x) for x in item.get("expected_doc_ids", [])],
                category=str(item.get("category", "general")),
            )
        )
        ref = str(item.get("reference") or "").strip()
        if ref:
            refs[q] = ref
    if not seeds:
        raise ValueError(f"数据集为空:{path}")
    return seeds, refs


def main() -> int:
    parser = argparse.ArgumentParser(description="RAGAS 四指标独立评测")
    parser.add_argument("--gateway", default="http://localhost:9090/api/sentinel")
    parser.add_argument("--agent", default="http://localhost:8001")
    parser.add_argument("--seed", default=str(_REPO_ROOT / "evaluation" / "seeds" / "ragas-seeds.jsonl"))
    parser.add_argument("--output", default=str(_REPO_ROOT / "evaluation" / "reports"))
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--limit", type=int, default=0, help="仅取前 N 条，0=全部")
    parser.add_argument("--category", default="", help="按 seed category 子串过滤（逗号分隔）")
    parser.add_argument(
        "--reference",
        default="",
        help="额外参考答案 jsonl（每行 {q, reference}），用于覆盖或补充数据集自带的 reference",
    )
    parser.add_argument("--name", default="ragas", help="报告名前缀")
    args = parser.parse_args()

    seeds, references = _load_seed_file(Path(args.seed))
    if args.category:
        wanted = [x.strip() for x in args.category.split(",") if x.strip()]
        seeds = [s for s in seeds if any(w in s.category for w in wanted)]
    if args.limit:
        seeds = seeds[: args.limit]
    if not seeds:
        print("[ragas_eval] 过滤后无样本", file=sys.stderr)
        return 2

    references.update(_load_references(args.reference))
    with_ref = any(s.q in references for s in seeds)

    llm = _build_llm()
    embeddings = _build_embeddings()
    metrics = _build_metrics(llm, embeddings, with_ref)

    cases = asyncio.run(
        _collect(
            seeds,
            gateway=args.gateway,
            agent=args.agent,
            username=args.username,
            password=args.password,
            concurrency=args.concurrency,
            timeout=args.timeout,
        )
    )
    asyncio.run(
        _score(cases, metrics=metrics, references=references, concurrency=args.concurrency)
    )

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    payload = {
        "name": args.name,
        "timestamp": ts,
        "config": {
            "seed": str(args.seed),
            "limit": args.limit,
            "category": args.category,
            "judge_model": JUDGE_MODEL,
            "judge_base_url": JUDGE_BASE_URL,
            "embed_model": EMBED_MODEL,
            "with_reference": with_ref,
            "concurrency": args.concurrency,
        },
        "summary": _summarize(cases, list(metrics)),
        "cases": [asdict(c) for c in cases],
    }
    json_path, md_path = _write_reports(payload, Path(args.output), args.name, ts)

    print(json.dumps(payload["summary"]["metrics"], ensure_ascii=False, indent=2))
    print(f"[ragas_eval] JSON: {json_path}")
    print(f"[ragas_eval] Markdown: {md_path}")  # task.sh 用这行回填报告路径
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
