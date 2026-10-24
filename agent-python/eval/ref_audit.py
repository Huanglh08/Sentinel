"""参考答案逐句核对：哪些断言在期望文档里找不到依据。

为什么需要：RAGAS 的上下文召回把参考答案当「必须可归因于语料」的真值。答案里
夹带语料没有的推论（对比结论、处置建议）时，召回无论检索多准都会被判低——先要
分清是「检索没拿到」还是「答案本身超纲」。

方法：把参考答案拆成断言，逐条让评测模型在期望文档原文里找依据，判
direct（原文直接表述）/ partial（可推出但未直说）/ none（原文没有）。
自检：模型给出的依据必须是文档原文的逐字片段（空白归一后），验证不过的标
evidence_verified=false，避免模型编造引文——与 answer-spans 的校验同一口径。

用法：
    python -m eval.ref_audit --seed ../evaluation/seeds/ragas-seeds-v2.jsonl \
        --out ../evaluation/reports/ref-audit-v2.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

from eval.chunk_experiment import load_docs  # noqa: E402
from eval.judge.client import JudgeClient  # noqa: E402
from eval.judge.config import JudgeConfig  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MAX_DOC_CHARS = 200000  # 两篇合计最大 9.2 万字（会话指南 + 认证指南），截断会砍掉后一篇导致误判

_SYSTEM = """你是评测数据质检员。任务：核对一条参考答案里的每一条断言，能否在给定的原始文档里找到依据。

规则：
1. 把参考答案按语义拆成尽量细的断言：一句一断言，比较、因果、结论、建议都要单独列出。
2. 每条断言给 status：
   - direct：文档里有明确表述，意思一致；
   - partial：文档只覆盖一半，或需要推断才能得到；
   - none：文档里没有这个意思。
3. evidence 必须是从文档里逐字复制的英文片段（不超过 200 字符），不许改写、不许翻译、不许拼接；
   status 为 none 时 evidence 留空字符串。
4. 不要因为断言「常识上正确」或「行业通行」就判 direct，只看文档写了什么。
5. 断言里的中文表述若对应文档的英文说法，按语义对应即可，但 evidence 仍必须是英文原文。

只输出 JSON：{"assertions":[{"text":"断言原文","status":"direct|partial|none","evidence":"","note":"简短理由"}]}"""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _evidence_ok(evidence: str, doc_text: str) -> bool:
    ev = _norm(evidence)
    if not ev:
        return False
    doc = _norm(doc_text)
    if ev in doc:
        return True
    return len(ev) >= 60 and ev[:60] in doc


def _user_prompt(case: dict, doc_text: str) -> str:
    return (
        f"【问题】\n{case['q']}\n\n"
        f"【原始文档】\n{doc_text[:_MAX_DOC_CHARS]}\n\n"
        f"【参考答案】\n{case['reference']}"
    )


async def _audit_one(client: JudgeClient, case: dict, doc_text: str, sem: asyncio.Semaphore) -> dict:
    async with sem:
        try:
            out = await client.judge(task="ref_audit", system=_SYSTEM, user=_user_prompt(case, doc_text))
        except Exception as e:  # noqa: BLE001
            return {"id": case["id"], "error": f"{type(e).__name__}: {e}"}
    rows = []
    for a in out.get("assertions") or []:
        text = str(a.get("text") or "").strip()
        if not text:
            continue
        ev = str(a.get("evidence") or "").strip()
        rows.append(
            {
                "text": text,
                "status": str(a.get("status") or "").strip().lower(),
                "evidence": ev,
                "evidence_verified": _evidence_ok(ev, doc_text),
                "note": str(a.get("note") or "").strip(),
            }
        )
    return {
        "id": case["id"],
        "category": case["category"],
        "question": case["q"],
        "reference": case["reference"],
        "assertions": rows,
        "unsupported": [r["text"] for r in rows if r["status"] in ("none", "partial")],
    }


_RECHECK_SYSTEM = """你是文档核对员。给定一条断言和原始文档，先找出能支撑它的原文段落，再判断支持程度。

规则：
1. quote 必须逐字复制文档原文（1-3 句，最多 500 字符），不许改写、翻译、拼接；找不到就留空。
2. status：support（原文明确支持）/ partial（只支持一部分，或需要推断）/ none（原文没有）。
只输出 JSON：{"status":"support|partial|none","quote":"","note":"简短理由"}"""


async def _recheck_one(
    client: JudgeClient, claim: str, doc_text: str, sem: asyncio.Semaphore
) -> dict:
    async with sem:
        out = await client.judge(
            task="ref_recheck",
            system=_RECHECK_SYSTEM,
            user=f"【原始文档】\n{doc_text[:_MAX_DOC_CHARS]}\n\n【断言】\n{claim}",
        )
    ev = str(out.get("quote") or out.get("evidence") or "").strip()
    return {
        "status": str(out.get("status") or "").strip().lower(),
        "evidence": ev,
        "evidence_verified": _evidence_ok(ev, doc_text),
        "note": str(out.get("note") or "").strip(),
    }


async def _run_recheck(
    rows: list[dict], docs: dict[str, str], expected: dict[str, list[str]], concurrency: int
) -> None:
    cfg = JudgeConfig.from_env()
    cfg.model = os.getenv("JUDGE_MODEL") or "deepseek-v4.1-flash-1017"
    cfg.max_tokens = 1024
    client = JudgeClient(cfg)
    sem = asyncio.Semaphore(concurrency)
    items = [
        (r, a)
        for r in rows
        for a in r.get("assertions", [])
        if a["status"] in ("none", "partial")
    ]
    done = 0

    async def one(r: dict, a: dict) -> None:
        nonlocal done
        doc_text = "\n\n".join(
            f"<file: {d}>\n{docs[d]}" for d in expected.get(r["id"], []) if d in docs
        )
        a["recheck"] = await _recheck_one(client, a["text"], doc_text, sem)
        done += 1
        print(f"[ref_audit] 复核 {done}/{len(items)} {r['id']}", file=sys.stderr, flush=True)

    await asyncio.gather(*(one(r, a) for r, a in items))


def _final_verdict(a: dict) -> str:
    """复核优先：复核说 support/partial 但给不出逐字依据的，按「未证实」处理。"""
    rc = a.get("recheck")
    if not rc:
        return a["status"]
    if rc["status"] in ("support", "partial") and rc["evidence_verified"]:
        return rc["status"]
    return "none"


async def _run(cases: list[dict], docs: dict[str, str], concurrency: int) -> list[dict]:
    cfg = JudgeConfig.from_env()
    cfg.model = os.getenv("JUDGE_MODEL") or "deepseek-v4.1-flash-1017"
    cfg.max_tokens = 4096
    client = JudgeClient(cfg)
    sem = asyncio.Semaphore(concurrency)
    done = 0
    total = len(cases)

    async def one(case: dict) -> dict:
        nonlocal done
        doc_text = "\n\n".join(f"<file: {d}>\n{docs[d]}" for d in case["expected_doc_ids"] if d in docs)
        res = await _audit_one(client, case, doc_text, sem)
        done += 1
        print(f"[ref_audit] {done}/{total} {case['id']}", file=sys.stderr, flush=True)
        return res

    return list(await asyncio.gather(*(one(c) for c in cases)))


def main() -> int:
    ap = argparse.ArgumentParser(description="参考答案逐条断言核对")
    ap.add_argument("--seed", default=str(_REPO_ROOT / "evaluation" / "seeds" / "ragas-seeds-v2.jsonl"))
    ap.add_argument("--out", default=str(_REPO_ROOT / "evaluation" / "reports" / "ref-audit-v2.json"))
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", default="", help="只核对指定题号，逗号分隔")
    ap.add_argument("--recheck", default="", help="对已有结果里的 none/partial 断言做二次复核，传上次输出路径")
    args = ap.parse_args()

    docs, _ = load_docs()
    seeds = [json.loads(l) for l in Path(args.seed).read_text(encoding="utf-8").splitlines() if l.strip()]

    if args.recheck:
        prev = json.loads(Path(args.recheck).read_text(encoding="utf-8"))
        rows = prev["results"]
        expected = {s["id"]: s.get("expected_doc_ids") or [] for s in seeds}
        asyncio.run(_run_recheck(rows, docs, expected, args.concurrency))
        for r in rows:
            r["final_unsupported"] = [
                a["text"] for a in r.get("assertions", []) if _final_verdict(a) == "none"
            ]
            r["unsupported"] = r["final_unsupported"]
        out = Path(args.out)
        payload = {
            "seed": prev.get("seed"),
            "cases": len(rows),
            "unsupported_cases": sum(1 for r in rows if r["final_unsupported"]),
            "unsupported_claims": sum(len(r["final_unsupported"]) for r in rows),
            "results": rows,
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        bad = [r for r in rows if r["final_unsupported"]]
        print(
            f"[ref_audit] 复核后有问题的题 {len(bad)}/{len(rows)}，"
            f"无依据断言 {payload['unsupported_claims']} 条"
        )
        for r in bad:
            print(f"  {r['id']}: " + " | ".join(t[:50] for t in r["final_unsupported"]))
        print(f"[ref_audit] 结果: {out}")
        return 0

    cases = [s for s in seeds if s.get("reference") and s.get("expected_doc_ids")]
    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        cases = [c for c in cases if c["id"] in wanted]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("[ref_audit] 没有可核对的题", file=sys.stderr)
        return 2

    rows = asyncio.run(_run(cases, docs, args.concurrency))
    payload = {
        "seed": str(args.seed),
        "cases": len(rows),
        "unsupported_cases": sum(1 for r in rows if r.get("unsupported")),
        "unverified_evidence": sum(
            1 for r in rows for a in r.get("assertions", []) if a["status"] in ("direct", "partial") and not a["evidence_verified"]
        ),
        "results": rows,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    bad = [r for r in rows if r.get("unsupported")]
    print(f"[ref_audit] 有问题的题 {len(bad)}/{len(rows)}，依据未通过逐字校验的断言 {payload['unverified_evidence']} 条")
    for r in bad:
        print(f"  {r['id']}: " + " | ".join(t[:40] for t in r["unsupported"]))
    print(f"[ref_audit] 结果: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
