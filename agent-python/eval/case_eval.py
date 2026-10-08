"""案件调查链路评测（P2 调查评测集上线）。

读取 seed-case.jsonl,对每条 clue 调用
    POST http://localhost:8001/case/investigate   (SSE)
先 POST /auth/login 换 token。解析事件流:
    phase(start/done) / case.created / step / report / approval.created /
    l1.autopass / end(tta/ttr/steps/forced_stop_reason) / error

确定性判定:
- hit_conclusion : report.conclusion 命中 expect_conclusion
- hit_confidence : report.confidence >= min_confidence
- phases_ok      : triage/investigate/report 三段全部 done
- no_forced_stop : 步数/时间预算未被强制打断（inconclusive 期望用例豁免）

汇总指标:
- 结论准确率 / 置信度达标率 / 阶段完整性率 / 预算打断率
- TTA(建案) / TTR(报告) 平均延迟

输出 JSON + Markdown 报告。

运行:
    python -m eval.case_eval --username admin --password admin
或:
    python -m eval.case_eval --base http://localhost:8001 \
        --seed eval/data/seed-case.jsonl --output eval/reports
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

# 三段状态机的期望阶段序（分诊 -> 调查 -> 报告）
EXPECTED_PHASES = ("triage", "investigate", "report")


# ---------- 数据模型 ----------


@dataclass
class CaseSeed:
    name: str
    clue: str
    expect_conclusion: str
    min_confidence: float = 0.6
    category: str = "general"


@dataclass
class CaseEvalResult:
    name: str
    category: str
    ground_truth: str
    predicted_conclusion: str
    confidence: float
    hit_conclusion: bool
    hit_confidence: bool
    phases_ok: bool
    phases_seen: list[str]
    forced_stop: bool
    forced_stop_reason: str
    steps: int
    has_approval: bool
    l1_autopass: bool
    case_id: str
    passed: bool
    tta_ms: float
    ttr_ms: float
    total_ms: float
    error: str | None = None


@dataclass
class CaseEvalSummary:
    total: int
    successful: int
    failed: int
    conclusion_accuracy: float
    confidence_pass_rate: float
    phase_integrity_rate: float
    forced_stop_rate: float
    approval_rate: float
    l1_autopass_count: int
    tta_avg: float
    ttr_avg: float
    cases: list[CaseEvalResult] = field(default_factory=list)


# ---------- IO ----------


def load_seeds(path: Path) -> list[CaseSeed]:
    if not path.exists():
        raise FileNotFoundError(f"seed 文件不存在:{path}")
    out: list[CaseSeed] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        item = json.loads(line)
        out.append(
            CaseSeed(
                name=str(item["name"]).strip(),
                clue=str(item["clue"]).strip(),
                expect_conclusion=str(item["expect_conclusion"]).strip().lower(),
                min_confidence=float(item.get("min_confidence", 0.6)),
                category=str(item.get("category", "general")),
            )
        )
    if not out:
        raise ValueError(f"seed 文件为空:{path}")
    return out


def _auth_headers(api_key: str, bearer_token: str) -> dict[str, str]:
    """agent-python 鉴权:优先 X-API-Key,其次 Bearer JWT;两者都空则匿名(require_auth=False 默认)。

    agent-python 没有 /auth/login 端点——不要对着 Java 网关的登录接口打。"""
    if api_key:
        return {"X-API-Key": api_key, "Accept": "text/event-stream"}
    if bearer_token:
        return {"Authorization": f"Bearer {bearer_token}", "Accept": "text/event-stream"}
    return {"Accept": "text/event-stream"}


# ---------- SSE 解析 ----------


def _parse_sse_frame(block: str) -> tuple[str, str]:
    """把一个 SSE 帧（event+data 行）解析为 (event, data)。"""
    event, data = "message", ""
    for ln in block.splitlines():
        if ln.startswith("event:"):
            event = ln[len("event:"):].strip()
        elif ln.startswith("data:"):
            data += ln[len("data:"):].strip()
    return event, data


async def _eval_one(
    client: httpx.AsyncClient,
    base: str,
    api_key: str,
    bearer_token: str,
    case: CaseSeed,
    timeout: float,
) -> CaseEvalResult:
    headers = _auth_headers(api_key, bearer_token)
    phases: list[str] = []
    phase_done: set[str] = set()
    report: dict = {}
    case_id = ""
    forced_stop = ""
    steps = 0
    has_approval = False
    l1_autopass = False
    tta_ms = ttr_ms = 0.0
    error: str | None = None

    t0 = time.perf_counter()
    try:
        async with client.stream(
            "POST",
            f"{base.rstrip('/')}/case/investigate",
            json={"clue": case.clue, "source": "case_eval"},
            headers=headers,
            timeout=timeout,
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "ignore")
                return CaseEvalResult(
                    name=case.name, category=case.category,
                    ground_truth=case.expect_conclusion,
                    predicted_conclusion="", confidence=0.0,
                    hit_conclusion=False, hit_confidence=False,
                    phases_ok=False, phases_seen=[], forced_stop=True,
                    forced_stop_reason=f"HTTP {resp.status_code}", steps=0,
                    has_approval=False, l1_autopass=False, case_id="",
                    passed=False, tta_ms=0.0, ttr_ms=0.0,
                    total_ms=(time.perf_counter() - t0) * 1000.0,
                    error=f"HTTP {resp.status_code}: {body[:200]}",
                )
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                while "\n\n" in buf:
                    frame, buf = buf.split("\n\n", 1)
                    event, data = _parse_sse_frame(frame)
                    if not data:
                        continue
                    try:
                        payload = json.loads(data)
                    except Exception:  # noqa: BLE001
                        continue
                    if event == "error":
                        error = str(payload.get("message", "unknown"))
                    elif event == "end":
                        if payload.get("status") == "done":
                            break
                    elif event == "message":
                        etype = payload.get("type", "")
                        if etype == "case.created":
                            case_id = str(payload.get("case_id", ""))
                            tta_ms = (time.perf_counter() - t0) * 1000.0
                        elif etype == "phase":
                            # 系统发大写(TRIAGE/INVESTIGATE/REPORT)，统一小写再比对
                            phase = str(payload.get("phase", "")).lower()
                            status = str(payload.get("status", ""))
                            phases.append(f"{phase}:{status}")
                            if status == "done":
                                phase_done.add(phase)
                                if phase == "report":
                                    ttr_ms = (time.perf_counter() - t0) * 1000.0
                        elif etype == "step":
                            steps += 1
                        elif etype == "report":
                            report = payload.get("report") or {}
                        elif etype == "end":
                            # 调查结束事件：读取步数与预算打断原因（权威值）
                            forced_stop = str(payload.get("forced_stop_reason", "") or "")
                            try:
                                steps = int(payload.get("steps", steps))
                            except (TypeError, ValueError):
                                pass
                        elif etype == "approval.created":
                            has_approval = True
                        elif etype == "l1.autopass":
                            l1_autopass = True
    except Exception as e:  # noqa: BLE001
        error = error or f"exception: {e!s}"

    total_ms = (time.perf_counter() - t0) * 1000.0
    conclusion = str(report.get("conclusion", "")).lower()
    confidence = float(report.get("confidence") or 0.0)
    phases_ok = all(p in phase_done for p in EXPECTED_PHASES)
    expect_inconclusive = case.expect_conclusion == "inconclusive"
    no_forced_stop = (forced_stop == "") or expect_inconclusive

    passed = (
        error is None
        and conclusion == case.expect_conclusion
        and confidence >= case.min_confidence
        and phases_ok
        and no_forced_stop
    )
    return CaseEvalResult(
        name=case.name,
        category=case.category,
        ground_truth=case.expect_conclusion,
        predicted_conclusion=conclusion,
        confidence=round(confidence, 4),
        hit_conclusion=(conclusion == case.expect_conclusion),
        hit_confidence=(confidence >= case.min_confidence),
        phases_ok=phases_ok,
        phases_seen=phases,
        forced_stop=(forced_stop != ""),
        forced_stop_reason=forced_stop,
        steps=steps,
        has_approval=has_approval,
        l1_autopass=l1_autopass,
        case_id=case_id,
        passed=passed,
        tta_ms=round(tta_ms, 2),
        ttr_ms=round(ttr_ms, 2),
        total_ms=round(total_ms, 2),
        error=error,
    )


# ---------- 主流程 ----------


async def _run(
    base: str,
    seeds: list[CaseSeed],
    api_key: str,
    bearer_token: str,
    timeout: float,
) -> CaseEvalSummary:
    async with httpx.AsyncClient() as client:
        results: list[CaseEvalResult] = []
        for i, case in enumerate(seeds, 1):  # 调查链路串行（共享主 Agent 底座）
            r = await _eval_one(client, base, api_key, bearer_token, case, timeout)
            results.append(r)
            print(
                f"[case_eval] {i}/{len(seeds)} {r.name} "
                f"conclusion={r.predicted_conclusion or '-'}(期望 {r.ground_truth}) "
                f"conf={r.confidence:.2f} steps={r.steps} "
                f"{'PASS' if r.passed else 'FAIL'}"
                f"{'' if r.error is None else f' err={r.error[:80]}'}",
                file=sys.stderr,
            )

    ok = [r for r in results if r.error is None]
    n_ok = len(ok)
    return CaseEvalSummary(
        total=len(results),
        successful=n_ok,
        failed=len(results) - n_ok,
        conclusion_accuracy=round(sum(1 for r in ok if r.hit_conclusion) / n_ok, 4) if n_ok else 0.0,
        confidence_pass_rate=round(sum(1 for r in ok if r.hit_confidence) / n_ok, 4) if n_ok else 0.0,
        phase_integrity_rate=round(sum(1 for r in ok if r.phases_ok) / n_ok, 4) if n_ok else 0.0,
        forced_stop_rate=round(sum(1 for r in ok if r.forced_stop) / n_ok, 4) if n_ok else 0.0,
        approval_rate=round(sum(1 for r in ok if r.has_approval) / n_ok, 4) if n_ok else 0.0,
        l1_autopass_count=sum(1 for r in ok if r.l1_autopass),
        tta_avg=round(statistics.mean([r.tta_ms for r in ok]), 2) if ok else 0.0,
        ttr_avg=round(statistics.mean([r.ttr_ms for r in ok]), 2) if ok else 0.0,
        cases=results,
    )


def _render_md(summary: CaseEvalSummary, base: str, seed_path: str) -> str:
    def _fmt(v: float) -> str:
        return f"{v:.2%}" if 0 < v <= 1 else str(v)

    lines = [
        "# 案件调查链路评测报告",
        "",
        f"- 目标接口: `POST {base}/case/investigate` (SSE)",
        f"- 种子集: `{seed_path}`",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 用例总数 | {summary.total} |",
        f"| 成功 / 失败 | {summary.successful} / {summary.failed} |",
        f"| 结论准确率 | {_fmt(summary.conclusion_accuracy)} |",
        f"| 置信度达标率 | {_fmt(summary.confidence_pass_rate)} |",
        f"| 三段阶段完整性率 | {_fmt(summary.phase_integrity_rate)} |",
        f"| 预算打断率 | {_fmt(summary.forced_stop_rate)} |",
        f"| 人工审批产生率 | {_fmt(summary.approval_rate)} |",
        f"| L1 自动放行次数 | {summary.l1_autopass_count} |",
        f"| TTA 均值(建案) | {summary.tta_avg:.0f} ms |",
        f"| TTR 均值(报告) | {summary.ttr_avg:.0f} ms |",
        "",
        "## 明细",
        "",
        "| 用例 | 期望 | 实际 | 置信度 | 阶段完整 | 步数 | 审批 | L1 | 结果 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in summary.cases:
        lines.append(
            f"| {r.name} | {r.ground_truth} | {r.predicted_conclusion or '-'} "
            f"| {r.confidence:.2f} | {'✓' if r.phases_ok else '✗'} | {r.steps} "
            f"| {'是' if r.has_approval else '-'} | {'是' if r.l1_autopass else '-'} "
            f"| {'PASS' if r.passed else 'FAIL'}"
            f"{'' if r.error is None else f'<br>{r.error[:60]}'} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    eval_dir = Path(__file__).resolve().parent
    repo_root = Path(__file__).resolve().parents[2]
    default_seed = repo_root / "evaluation" / "seeds" / "case-seeds.jsonl"
    default_output = eval_dir / "reports"

    parser = argparse.ArgumentParser(description="案件调查链路评测（SSE 端到端）")
    parser.add_argument("--base", default="http://localhost:8001", help="agent-python 服务地址")
    parser.add_argument("--seed", default=str(default_seed), help="seed jsonl 路径")
    parser.add_argument("--output", default=str(default_output), help="报告输出目录")
    parser.add_argument("--api-key", default="", help="X-API-Key(若服务开启 require_auth)")
    parser.add_argument("--token", default="", help="Bearer JWT(若服务开启 require_auth 且用 JWT)")
    parser.add_argument("--timeout", type=float, default=300.0, help="单条用例超时秒数")
    parser.add_argument("--name", default="case", help="报告名前缀")
    args = parser.parse_args()

    seeds = load_seeds(Path(args.seed))
    print(f"[case_eval] 加载 {len(seeds)} 条 seed from {args.seed}", file=sys.stderr)

    summary = asyncio.run(_run(args.base, seeds, args.api_key, args.token, args.timeout))

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
    print(f"[case_eval] JSON 报告:{json_path}", file=sys.stderr)

    md_path = output_dir / f"{args.name}-{timestamp}.md"
    md_path.write_text(
        _render_md(summary, base=args.base, seed_path=str(args.seed)),
        encoding="utf-8",
    )
    print(f"[case_eval] Markdown 报告:{md_path}", file=sys.stderr)

    if summary.conclusion_accuracy < 0.8 and summary.successful > 0:
        print(
            f"[case_eval] 警告:结论准确率 {summary.conclusion_accuracy:.2%} 低于 80% 目标",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
