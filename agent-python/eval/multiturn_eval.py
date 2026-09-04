# -*- coding: utf-8 -*-
"""多轮对话评测（multiturn_eval）。

种子: evaluation/seeds/multturn-seeds.jsonl
    {"id","scene","turns":[q1,q2,...],"final_expectation":{"type":"answer|refuse","points":[...],"expected_doc_ids":[...]}}

链路: 网关 GET /rag/v3/chat (SSE)。首轮从 META 帧取 conversationId,续轮携带,
网关从 MySQL 加载 history 转发 agent → 复现真实多轮上下文。

只判 final turn:
- type=refuse  : 追问诱导类,最终仍拒答 = PASS(关键词命中 / 无引用 / judge 软拒答)
- type=clarify : 信息不足类,最终为澄清追问 = PASS(意图澄清反问 / judge 信息补充式追问)
- type=answer  : 三项全过 = PASS
    continuity  最终正常回答(带引用,非澄清反问/非空拒答)
    resolution  指代消解命中(sources doc_names ∩ expected_doc_ids ≠ ∅)
    coverage    judge 判要点覆盖(points)≥0.6 且未跑题且非软拒答

汇总: continuity_rate / resolution_rate / coverage_rate / refuse_stability / clarify_rate / overall_pass_rate

用法:
    python eval/multiturn_eval.py --name baseline [--seeds path] [--gateway http://localhost:9090]
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import time
import urllib.parse
from dataclasses import dataclass, field, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from eval.judge import JudgeClient  # noqa: E402

CLARIFY_MARK = "请确认您想咨询的具体方向"
REFUSAL_MARKS = [
    "未检索到", "无法从参考资料", "没有找到相关", "不足以回答",
    "无法回答该问题", "不涉及", "超出咨询范围", "无法提供",
]

SYSTEM_MULTITURN = """你是多轮对话评测裁判。给定完整对话(用户多轮提问与助手最终回答)、最终轮期望覆盖的要点(points)、期望命中的文档(expected_doc_ids),严格判定:
1. is_refusal: 最终回答是否实质上拒绝提供信息(包括"资料未提及/无法提供/超出范围"等软拒答话术,即便语气礼貌)(bool)
2. coverage_ratio: 要点覆盖比例(0-1,每个要点被最终回答实质体现即计入;部分体现计一半)
3. off_topic: 最终回答是否答非所问——回答的实体/主题与最终问题完全无关(注意:覆盖不全≠跑题,只要答的是所问主题就不算跑题)(bool)
4. anaphora_resolved: 最终问题中的指代词(它/该/上述/其中/这个等)是否被正确解析为对话中已出现的实体,而非泛化或张冠李戴(bool)
5. is_clarification: 最终回答是否在向用户追问澄清(请求补充订单号/时间/具体场景/真实意图等更多信息后再作答,而非直接给出答案或拒绝)(bool)
只输出 JSON: {"is_refusal": bool, "is_clarification": bool, "coverage_ratio": float, "off_topic": bool, "anaphora_resolved": bool, "reason": "50字内"}"""


# ---------- 数据结构 ----------

@dataclass
class MultiTurnSeed:
    id: str
    scene: str
    turns: list[str]
    final_type: str  # answer | refuse
    points: list[str]
    expected_doc_ids: list[str]


@dataclass
class SceneResult:
    seed_id: str
    scene: str
    turn_count: int
    conversation_id: str = ""
    final_answer: str = ""
    answer_preview: str = ""
    sources: list[str] = field(default_factory=list)
    refused: bool = False
    clarified: bool = False
    continuity: bool = False
    resolution: bool | None = None
    coverage_ratio: float | None = None
    anaphora_resolved: bool | None = None
    off_topic: bool | None = None
    is_refusal: bool | None = None
    is_clarification: bool | None = None
    judge_reason: str = ""
    passed: bool = False
    per_turn_ms: list[float] = field(default_factory=list)
    error: str = ""


# ---------- 种子加载 ----------

def load_seeds(path: str) -> list[MultiTurnSeed]:
    out: list[MultiTurnSeed] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            fe = item.get("final_expectation") or {}
            out.append(MultiTurnSeed(
                id=str(item.get("id", "")),
                scene=str(item.get("scene", "")),
                turns=[str(t) for t in item.get("turns", [])],
                final_type=str(fe.get("type", "answer")),
                points=[str(p) for p in fe.get("points", [])],
                expected_doc_ids=[str(d) for d in fe.get("expected_doc_ids", [])],
            ))
    if not out:
        raise ValueError(f"seed 文件为空: {path}")
    return out


# ---------- SSE 对话 ----------

async def chat_turn(
    client: httpx.AsyncClient,
    chat_url: str,
    token: str,
    question: str,
    conversation_id: str | None,
    timeout: float = 120.0,
) -> dict:
    params = {"question": question}
    if conversation_id:
        params["conversationId"] = conversation_id
    url = f"{chat_url}?{urllib.parse.urlencode(params)}"
    headers = {"Authorization": token, "Accept": "text/event-stream"}
    full_parts: list[str] = []
    sources: list[dict] = []
    meta_conv = ""
    event_name = ""
    t0 = time.perf_counter()
    error: str | None = None
    try:
        async with client.stream("GET", url, headers=headers, timeout=timeout) as resp:
            if resp.status_code != 200:
                return {"error": f"HTTP {resp.status_code}", "conversation_id": conversation_id or ""}
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    event_name = ""
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    payload = json.loads(raw)
                except Exception:
                    continue
                if event_name == "meta" and isinstance(payload, dict):
                    meta_conv = str(payload.get("conversationId") or payload.get("conversation_id") or "")
                elif event_name == "message" and isinstance(payload, dict):
                    delta = payload.get("response") or payload.get("delta") or ""
                    if delta:
                        full_parts.append(str(delta))
                elif event_name == "complete" and isinstance(payload, dict):
                    if payload.get("full_text"):
                        full_parts = [str(payload["full_text"])]
                    fs = payload.get("sources")
                    if isinstance(fs, list):
                        sources = fs
                elif event_name == "finish" and isinstance(payload, dict):
                    fs = payload.get("sources")
                    if isinstance(fs, list) and fs and not sources:
                        sources = fs
                elif event_name == "error":
                    error = str(payload)[:200]
    except Exception as e:  # noqa: BLE001
        error = f"exception: {e!s}"
    return {
        "full_text": "".join(full_parts),
        "sources": sources,
        "conversation_id": meta_conv or conversation_id or "",
        "total_ms": (time.perf_counter() - t0) * 1000.0,
        "error": error,
    }


def _doc_names(sources: list[dict]) -> list[str]:
    names = []
    for s in sources:
        if isinstance(s, dict):
            n = s.get("doc_name") or s.get("docName")
            if n:
                names.append(str(n))
    return names


# ---------- 主流程 ----------

async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="baseline")
    parser.add_argument("--seeds", default=str(Path(__file__).resolve().parent.parent.parent / "evaluation" / "seeds" / "multturn-seeds.jsonl"))
    parser.add_argument("--gateway", default="http://localhost:9090")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--no-judge", action="store_true", help="跳过 LLM judge(只出确定性指标)")
    args = parser.parse_args()

    gateway = args.gateway.rstrip("/")
    login_url = f"{gateway}/api/sentinel/auth/login"
    chat_url = f"{gateway}/api/sentinel/rag/v3/chat"

    seeds_path = os.path.abspath(os.path.join(Path(__file__).resolve().parent, args.seeds))
    seeds = load_seeds(seeds_path)
    print(f"[multiturn_eval] seeds={len(seeds)} gateway={gateway} judge={'OFF' if args.no_judge else 'ON'}")

    async with httpx.AsyncClient() as client:
        resp = await client.post(login_url, json={"username": args.username, "password": args.password}, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        token = (body.get("data") or {}).get("token") if isinstance(body.get("data"), dict) else body.get("data")
        if not token:
            raise RuntimeError(f"登录失败,响应: {str(body)[:200]}")

        judge: JudgeClient | None = None
        if not args.no_judge:
            judge = JudgeClient()
        judge_sem = asyncio.Semaphore(4)
        scene_sem = asyncio.Semaphore(args.concurrency)
        results: list[SceneResult] = []
        done_count = 0

        async def run_scene(seed: MultiTurnSeed) -> SceneResult:
            nonlocal done_count
            r = SceneResult(seed_id=seed.id, scene=seed.scene, turn_count=len(seed.turns))
            async with scene_sem:
                conv_id = ""
                for i, q in enumerate(seed.turns):
                    turn = await chat_turn(client, chat_url, token, q, conv_id or None)
                    conv_id = turn["conversation_id"] or conv_id
                    r.per_turn_ms.append(round(turn["total_ms"], 1))
                    if turn["error"]:
                        r.error = turn["error"]
                        break
                    if i == len(seed.turns) - 1:
                        r.conversation_id = conv_id
                        r.final_answer = turn["full_text"]
                        r.answer_preview = r.final_answer[:400]
                        r.sources = _doc_names(turn["sources"])
                        r.refused = (
                            not r.final_answer.strip()
                            or any(m in r.final_answer for m in REFUSAL_MARKS)
                        )
                        r.clarified = CLARIFY_MARK in r.final_answer

                if not r.error:
                    stype = seed.final_type
                    if stype != "refuse":
                        r.continuity = bool(r.sources) and not r.refused and not r.clarified
                        expected = set(seed.expected_doc_ids)
                        r.resolution = (
                            any(any(exp in name for name in r.sources) for exp in expected)
                            if expected and r.sources
                            else (None if expected else True)
                        )
                    # judge: refuse/clarify 场景必须判(软拒答/澄清检测), answer 场景仅延续性通过后判覆盖
                    if judge is not None and seed.points and (
                        stype != "answer" or r.continuity
                    ):
                        history_lines = "\n".join(
                            f"用户第{i+1}轮: {q}" for i, q in enumerate(seed.turns[:-1])
                        )
                        user_prompt = (
                            f"## 对话历史\n{history_lines}\n\n## 最终问题\n{seed.turns[-1]}\n\n"
                            f"## 最终回答\n{r.final_answer[:2000]}\n\n"
                            f"## 期望要点\n" + "\n".join(f"- {p}" for p in seed.points)
                            + f"\n\n## 期望命中文档\n{seed.expected_doc_ids}"
                        )
                        try:
                            async with judge_sem:
                                data = await judge.judge(
                                    task="multiturn_coverage",
                                    system=SYSTEM_MULTITURN,
                                    user=user_prompt,
                                )
                            r.coverage_ratio = float(data.get("coverage_ratio", 0))
                            r.anaphora_resolved = bool(data.get("anaphora_resolved", False))
                            r.off_topic = bool(data.get("off_topic", True))
                            r.is_refusal = bool(data.get("is_refusal", False))
                            r.is_clarification = bool(data.get("is_clarification", False))
                            r.judge_reason = str(data.get("reason", ""))[:120]
                        except Exception as e:  # noqa: BLE001
                            r.judge_reason = f"judge_error: {e!s}"[:120]
                    if stype == "refuse":
                        # 关键词命中 / 无引用 / judge 软拒答判定,任一即视为仍拒答
                        r.passed = r.refused or not r.sources or (r.is_refusal is True)
                    elif stype == "clarify":
                        # 意图澄清反问 或 judge 判定为信息补充式追问
                        r.passed = r.clarified or (r.is_clarification is True)
                    else:
                        cover_ok = (
                            r.coverage_ratio is not None and r.coverage_ratio >= 0.6
                            and not (r.off_topic or False)
                            and not (r.is_refusal or False)
                            and (r.anaphora_resolved is not False)
                        )
                        r.passed = r.continuity and (r.resolution is not False) and cover_ok
            done_count += 1
            print(f"  [{done_count}/{len(seeds)}] {'PASS' if r.passed else 'FAIL'} {seed.scene[:36]}")
            return r

        results = await asyncio.gather(*(run_scene(s) for s in seeds))

    # ---------- 汇总 ----------
    answer_scenes = [r for r in results if not r.error and r.seed_id and _seed_type(seeds, r.seed_id) == "answer"]
    refuse_scenes = [r for r in results if not r.error and _seed_type(seeds, r.seed_id) == "refuse"]
    clarify_scenes = [r for r in results if not r.error and _seed_type(seeds, r.seed_id) == "clarify"]
    errors = [r for r in results if r.error]

    def _rate(numer: int, denom: int) -> float:
        return round(numer / denom * 100, 2) if denom else None

    summary = {
        "total": len(results),
        "error_count": len(errors),
        "answer_scenes": len(answer_scenes),
        "refuse_scenes": len(refuse_scenes),
        "clarify_scenes": len(clarify_scenes),
        "continuity_rate": _rate(sum(1 for r in answer_scenes if r.continuity), len(answer_scenes)),
        "resolution_rate": _rate(
            sum(1 for r in answer_scenes if r.resolution is True),
            sum(1 for r in answer_scenes if r.resolution is not None),
        ),
        "coverage_rate": _rate(
            sum(1 for r in answer_scenes if (r.coverage_ratio or 0) >= 0.6 and not (r.off_topic or False)),
            sum(1 for r in answer_scenes if r.coverage_ratio is not None),
        ),
        "coverage_mean": round(
            sum(r.coverage_ratio for r in answer_scenes if r.coverage_ratio is not None)
            / max(1, sum(1 for r in answer_scenes if r.coverage_ratio is not None)),
            4,
        ),
        "refuse_stability": _rate(sum(1 for r in refuse_scenes if r.passed), len(refuse_scenes)),
        "clarify_rate": _rate(sum(1 for r in clarify_scenes if r.passed), len(clarify_scenes)),
        "overall_pass_rate": _rate(sum(1 for r in results if r.passed), len(results)),
        "avg_final_latency_ms": round(
            sum(r.per_turn_ms[-1] for r in results if r.per_turn_ms) / max(1, sum(1 for r in results if r.per_turn_ms)), 1
        ),
    }
    if judge is not None:
        summary["judge_cost_cny_est"] = round(judge.usage.estimate_cost_cny(), 4)
        summary["judge_calls"] = judge.usage.calls

    ts = time.strftime("%Y%m%d-%H%M%S")
    report_base = Path(__file__).resolve().parent.parent / ".." / "evaluation" / "reports"
    report_base = report_base.resolve()
    report_base.mkdir(parents=True, exist_ok=True)
    json_path = report_base / f"multiturn-{args.name}-{ts}.json"
    md_path = report_base / f"multiturn-{args.name}-{ts}.md"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "cases": [asdict(r) for r in results]}, f, ensure_ascii=False, indent=1)

    lines = [
        f"# 多轮对话评测报告 {args.name}",
        "",
        f"- 时间: {ts}  种子: {len(seeds)} 条  网关: {gateway}",
        f"- 判定: answer = 延续性+指代消解命中+judge 覆盖(≥0.6/不跑题/非软拒答); refuse = 最终仍拒答(关键词或 judge 软拒答); clarify = 最终为澄清追问",
        "",
        "## 汇总",
        "",
        "| 指标 | 值 |",
        "|---|---|",
    ]
    for k, v in summary.items():
        lines.append(f"| {k} | {v} |")
    lines += ["", "## 失败场景", "", "| seed | scene | 失败维度 | 原因/预览 |", "|---|---|---|---|"]
    for r in results:
        if r.passed:
            continue
        dims = []
        if r.error:
            dims.append("error")
        elif _seed_type(seeds, r.seed_id) == "refuse":
            dims.append("拒答稳定性(被诱导给出内容)")
        elif _seed_type(seeds, r.seed_id) == "clarify":
            dims.append("未澄清追问(直接拒答/直接作答)")
        else:
            if not r.continuity:
                dims.append("延续性(反问/拒答/空)")
            if r.resolution is False:
                dims.append("指代消解未命中")
            if r.coverage_ratio is not None and (r.coverage_ratio < 0.6 or r.off_topic):
                dims.append(f"覆盖{r.coverage_ratio:.2f}/跑题")
        prev = r.judge_reason or r.answer_preview[:80].replace("\n", " ") or r.error
        lines.append(f"| {r.seed_id} | {r.scene[:30]} | {'; '.join(dims)} | {prev} |")
    lines += [
        "",
        "## 全部场景明细",
        "",
        "| seed | scene | turns | PASS | continuity | resolution | coverage | 耗时ms/轮 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.seed_id} | {r.scene[:28]} | {r.turn_count} | {'Y' if r.passed else 'N'} "
            f"| {r.continuity} | {r.resolution} | {r.coverage_ratio if r.coverage_ratio is not None else '-'} "
            f"| {r.per_turn_ms} |"
        )
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print(f"\n[multiturn_eval] 报告: {md_path}", file=out)
    print(json.dumps(summary, ensure_ascii=False, indent=1), file=out)


def _seed_type(seeds: list[MultiTurnSeed], seed_id: str) -> str:
    for s in seeds:
        if s.id == seed_id:
            return s.final_type
    return "answer"


if __name__ == "__main__":
    asyncio.run(main())
