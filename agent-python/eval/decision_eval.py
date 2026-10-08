"""处置决策评测（ROADMAP 第四批 #12）：决策混淆矩阵 + pass^k。

评测对象是**处置决策**（该自动 / 该升级 / 该放行），不是调查结论（那归 case_eval）。
决策值统一按生产同款规则离线复算（对齐 core/case/agent.py 的 _l1_autopass /
_l2_autopilot 与 store.get_risk_policy 出厂默认），语义 = "若 L1/L2 autopilot 均开启，
系统对该报告会做出什么决策"——因此与两端开关的当前配置无关，可重复。
SSE 观测事件（l1.autopass / approval.created / l2.autopilot）仅用于对齐校验：
L1 默认开启，凡观测到 l1.autopass 而复算不是 AUTO_PASS，记 alignment_warn。

指标（按 seed×run 展平，3×3 混淆矩阵派生）:
- dispose_accuracy       处置正确率（对角线占比）
- risk_mal_dispose_rate  高危误处置（误杀）: golden=ESCALATE 被自动处置 —— 硬门禁 = 0
- risk_ben_dispose_rate  良性误处置: golden=AUTO_PASS 被自动处置
- miss_release_rate      恶性放行: golden=ESCALATE 被当 benign 自动放行 —— 硬门禁 = 0
- miss_dispose_rate      漏处置（效率损失，不进门禁）: golden=AUTO_DISPOSE 升级了人工
- escalation_precision / escalation_recall  升级准确率 / 完整率（recall 门禁 ≥ 0.9）
- pass_k                 τ-bench pass^k: 同一 seed 独立 k 次决策全部命中 golden
                         （arXiv:2406.12045；自主权开启门禁，v1 建议 pass^3 ≥ 0.8）

参照（2026-09-05 链接当日验证存活）:
- τ-bench pass^k（同一任务 k 次全成才算可自主；单次正确率≠可靠性）: https://arxiv.org/abs/2406.12045
- SIR-Bench（专家 golden + 真实事件用例）: https://arxiv.org/abs/2604.12040
- EU AI Act 第 14 条（监督与自主度相称；automation bias → override 率监控）: https://artificialintelligenceact.eu/article/14/

运行:
    python -m eval.decision_eval --selfcheck
    python -m eval.decision_eval --runs 3                # 全量 12 seed × 3
    python -m eval.decision_eval --base http://localhost:8001 --limit 4 --runs 1
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from eval.case_eval import _auth_headers, _parse_sse_frame
from core.case.types import GENERIC_ACTION_WORDS, L2_ACTION_DENYLIST, MANUAL_ACTION_WORDS

DECISIONS = ("AUTO_PASS", "AUTO_DISPOSE", "ESCALATE")
DEFAULT_SEED = Path(__file__).resolve().parents[2] / "evaluation" / "seeds" / "decision-seeds.jsonl"

# ---------- 决策复算（对齐生产规则；改生产规则必须同步这里，见 --selfcheck） ----------

# 出厂默认阈值：CASE_L1_CONFIDENCE=0.85 / CASE_L2_CONFIDENCE=0.90（t_risk_policy 同源）
L1_CONFIDENCE_DEFAULT = 0.85
L2_CONFIDENCE_DEFAULT = 0.90
# CASE_L2_ACTION_WHITELIST 出厂默认（store._policy_defaults），与生产一字不差
WHITELIST_DEFAULT = ("观察", "提醒", "工单", "通知", "标记", "升级", "放行", "解除", "限制")
# CASE_L1_ACTION_WHITELIST 出厂默认（agent.py _l1_autopass），与生产一字不差
L1_WHITELIST_DEFAULT = ("放行", "观察", "提醒", "通知", "标记")
L2_SEVERITY = {"suspicious", "malicious"}  # 对齐 agent.py 的 L2_SEVERITY_SET

# 活策略覆盖：复算参数对齐被测配置（t_risk_policy），读不到时保持出厂默认
_LIVE: dict = {}


def _split_words(v) -> tuple[str, ...]:
    """策略词表兼容 list/逗号分隔 str 两种形态（get_risk_policy 返回 list）"""
    if isinstance(v, (list, tuple)):
        return tuple(str(w).strip() for w in v if str(w).strip())
    return tuple(w.strip() for w in str(v).split(",") if w.strip())


def _load_live_policy() -> None:
    try:
        from core.case.store import get_risk_policy

        pol = get_risk_policy()
        _LIVE["l2_threshold"] = float(pol["l2_confidence"])
        _LIVE["whitelist"] = _split_words(pol["l2_action_whitelist"])
        _LIVE["denylist"] = _split_words(pol["l2_action_denylist"])
    except Exception:  # noqa: BLE001
        pass  # DB 不可达：decide() 用出厂默认


def decide(
    conclusion: str,
    confidence: float,
    recommendations: list | None,
    *,
    l1_threshold: float = L1_CONFIDENCE_DEFAULT,
    l2_threshold: float = L2_CONFIDENCE_DEFAULT,
    whitelist: tuple[str, ...] = WHITELIST_DEFAULT,
    l1_whitelist: tuple[str, ...] = L1_WHITELIST_DEFAULT,
    denylist: tuple[str, ...] | list[str] = L2_ACTION_DENYLIST,
) -> str:
    """复算处置决策。golden 语义 = 规则正确决策，与两端 autopilot 开关无关。"""
    recs = [r for r in (recommendations or []) if isinstance(r, dict)]
    actions = [str(r["action"]) for r in recs if r.get("action")]
    # denylist 一票否决：高危案零自动处置，整案人工（对齐编排 deny_hit 分支）
    if any(kw in a for a in actions for kw in denylist):
        return "ESCALATE"
    # 建议分流（对齐 agent._split_auto）：白名单命中子集自动，其余落审批并行
    if conclusion == "benign":
        # L1：高置信 benign 且存在 L1 白名单命中动作 -> 自动放行
        if confidence >= l1_threshold and any(any(kw in a for kw in l1_whitelist) for a in actions):
            return "AUTO_PASS"
        return "ESCALATE"
    if conclusion in L2_SEVERITY:
        if not actions:
            return "ESCALATE"
        # 建议分流（对齐 _split_auto）：处置深度锚点 = 存在处置级白名单命中
        # （去掉观察/提醒/通知/标记等通用伴随词），仅通用动作不构成自动处置；
        # 转人工语义动作（如「发起人工核查工单」）不算自动处置——其语义即升级
        disposal_wl = tuple(
            w for w in whitelist
            if w not in GENERIC_ACTION_WORDS and not any(m in w for m in MANUAL_ACTION_WORDS)
        )
        if confidence >= l2_threshold and any(
            any(kw in a for kw in disposal_wl)
            and not any(m in a for m in MANUAL_ACTION_WORDS)
            for a in actions
        ):
            return "AUTO_DISPOSE"
    return "ESCALATE"  # inconclusive / 低置信 / 白名单不匹配 / 仅通用伴随动作 -> 人工


# ---------- 数据模型 ----------


@dataclass
class DecisionSeed:
    name: str
    clue: str
    expect_conclusion: str
    expect_decision: str
    min_confidence: float = 0.0
    category: str = "general"


@dataclass
class DecisionResult:
    name: str
    category: str
    run: int
    golden_decision: str
    decision: str
    observed_event: str  # l1.autopass / approval.created / l2.autopilot / ""
    alignment_warn: str  # 观测与复算不一致时记原因，否则为空
    conclusion: str
    confidence: float
    hit_conclusion: bool
    hit_confidence: bool
    recommendations: list = field(default_factory=list)
    case_id: str = ""
    error: str | None = None


# ---------- IO ----------


def load_seeds(path: Path) -> list[DecisionSeed]:
    if not path.exists():
        raise FileNotFoundError(f"seed 文件不存在:{path}")
    out: list[DecisionSeed] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        item = json.loads(line)
        ed = str(item["expect_decision"]).strip().upper()
        if ed not in DECISIONS:
            raise ValueError(f"seed {item.get('name')}: expect_decision 非法 {ed!r}")
        out.append(
            DecisionSeed(
                name=str(item["name"]).strip(),
                clue=str(item["clue"]).strip(),
                expect_conclusion=str(item["expect_conclusion"]).strip().lower(),
                expect_decision=ed,
                min_confidence=float(item.get("min_confidence", 0.0)),
                category=str(item.get("category", "general")),
            )
        )
    if not out:
        raise ValueError(f"seed 文件为空:{path}")
    return out


# ---------- 调查执行（SSE，模式对齐 case_eval） ----------


async def run_one(
    client: httpx.AsyncClient,
    base: str,
    api_key: str,
    bearer_token: str,
    seed: DecisionSeed,
    run: int,
    timeout: float,
) -> DecisionResult:
    headers = _auth_headers(api_key, bearer_token)
    report: dict = {}
    case_id = ""
    observed = ""
    error: str | None = None

    def _finish() -> DecisionResult:
        conclusion = str(report.get("conclusion", "")).lower()
        confidence = float(report.get("confidence") or 0.0)
        recs = report.get("recommendations") or []
        decision = decide(conclusion, confidence, recs, **_LIVE)
        warn = ""
        if observed and observed != decision:
            warn = f"观测 {observed} != 复算 {decision}（检查 decide() 与生产规则是否漂移）"
        return DecisionResult(
            name=seed.name,
            category=seed.category,
            run=run,
            golden_decision=seed.expect_decision,
            decision=decision,
            observed_event=observed,
            alignment_warn=warn,
            conclusion=conclusion,
            confidence=round(confidence, 4),
            hit_conclusion=(conclusion == seed.expect_conclusion),
            hit_confidence=(confidence >= seed.min_confidence),
            recommendations=recs,
            case_id=case_id,
            error=error,
        )

    try:
        async with client.stream(
            "POST",
            f"{base.rstrip('/')}/case/investigate",
            json={"clue": seed.clue, "source": "decision_eval"},
            headers=headers,
            timeout=timeout,
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "ignore")
                error = f"HTTP {resp.status_code}: {body[:200]}"
                return _finish()
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
                    elif event == "end" and payload.get("status") == "done":
                        break
                    elif event == "message":
                        etype = str(payload.get("type", ""))
                        if etype == "case.created":
                            case_id = str(payload.get("case_id", ""))
                        elif etype == "report":
                            report = payload.get("report") or {}
                        elif etype == "approval.created":
                            observed = "approval.created"
                        elif etype == "l1.autopass":
                            observed = "l1.autopass"
                        elif etype == "l2.autopilot":
                            observed = "l2.autopilot"
                        elif etype == "end":
                            break
    except Exception as e:  # noqa: BLE001
        error = error or f"exception: {e!s}"
    if observed == "l2.autopilot":
        observed = "AUTO_DISPOSE"
    elif observed == "l1.autopass":
        observed = "AUTO_PASS"
    elif observed == "approval.created":
        observed = "ESCALATE"
    return _finish()


# ---------- 指标 ----------


def compute_metrics(records: list[DecisionResult], n_seeds: int, runs: int) -> dict:
    """按 seed×run 展平出混淆矩阵与派生指标；pass_k 按 seed 聚合（k=runs 全对才算过）。"""
    m = {g: {d: 0 for d in DECISIONS} for g in DECISIONS}
    for r in records:
        m[r.golden_decision][r.decision] += 1
    total = sum(m[g][d] for g in DECISIONS for d in DECISIONS) or 1
    n_esc = sum(m["ESCALATE"].values()) or 1
    n_pass = sum(m["AUTO_PASS"].values()) or 1
    n_auto = sum(m["AUTO_DISPOSE"].values()) or 1
    esc_recall_den = m["ESCALATE"]["ESCALATE"] + m["ESCALATE"]["AUTO_PASS"] + m["ESCALATE"]["AUTO_DISPOSE"]
    metrics = {
        "samples": total,
        "seeds": n_seeds,
        "runs": runs,
        "confusion": m,  # confusion[golden][decision]
        "dispose_accuracy": round(sum(m[d][d] for d in DECISIONS) / total, 4),
        # 误杀：高危案被自动处置（硬门禁 = 0）
        "risk_mal_dispose_rate": round(m["ESCALATE"]["AUTO_DISPOSE"] / n_esc, 4),
        # 良性案被自动处置
        "risk_ben_dispose_rate": round(m["AUTO_PASS"]["AUTO_DISPOSE"] / n_pass, 4),
        # 恶性放行：高危案被当 benign 自动放行（硬门禁 = 0）
        "miss_release_rate": round(m["ESCALATE"]["AUTO_PASS"] / n_esc, 4),
        # 漏处置：低危案该自动没自动（效率损失，不进门禁）
        "miss_dispose_rate": round(m["AUTO_DISPOSE"]["ESCALATE"] / n_auto, 4),
        "escalation_precision": round(m["ESCALATE"]["ESCALATE"] / (sum(m[g]["ESCALATE"] for g in DECISIONS) or 1), 4),
        "escalation_recall": round(m["ESCALATE"]["ESCALATE"] / (esc_recall_den or 1), 4),
    }
    by_seed: dict[str, list[DecisionResult]] = {}
    for r in records:
        by_seed.setdefault(r.name, []).append(r)
    passed_seeds = sum(
        1 for rs in by_seed.values() if len(rs) == runs and all(x.decision == x.golden_decision for x in rs)
    )
    metrics["pass_k"] = round(passed_seeds / (n_seeds or 1), 4)
    metrics["k"] = runs
    gates = {
        "risk_mal_dispose_zero": metrics["risk_mal_dispose_rate"] == 0.0,
        "miss_release_zero": metrics["miss_release_rate"] == 0.0,
        "escalation_recall_ge_0.9": metrics["escalation_recall"] >= 0.9,
        "pass_k_ge_0.8": metrics["pass_k"] >= 0.8,
    }
    metrics["gates"] = gates
    metrics["gates_pass"] = all(gates.values())
    return metrics


def render_marketdown_report(metrics: dict, records: list[DecisionResult], elapsed_s: float) -> str:
    g = ["# 处置决策评测报告（decision_eval）", ""]
    g.append(f"- 样本：{metrics['seeds']} seeds × {metrics['runs']} runs = {metrics['samples']} 决策，耗时 {elapsed_s:.0f}s")
    g.append(f"- 门禁：{'**PASS**' if metrics['gates_pass'] else '**FAIL**'} {metrics['gates']}")
    g.append("")
    g.append("| 指标 | 值 |")
    g.append("|---|---|")
    for k in (
        "dispose_accuracy", "risk_mal_dispose_rate", "risk_ben_dispose_rate",
        "miss_release_rate", "miss_dispose_rate", "escalation_precision",
        "escalation_recall", "pass_k",
    ):
        g.append(f"| {k} | {metrics[k]} |")
    g.append("")
    g.append("## 混淆矩阵（行=golden，列=decision）")
    g.append("")
    g.append("| golden \\ decision | " + " | ".join(DECISIONS) + " |")
    g.append("|---|" + "---|" * len(DECISIONS))
    for gd in DECISIONS:
        g.append(f"| {gd} | " + " | ".join(str(metrics["confusion"][gd][d]) for d in DECISIONS) + " |")
    g.append("")
    warns = [r for r in records if r.alignment_warn or r.error]
    if warns:
        g.append(f"## 异常/对齐告警（{len(warns)} 条）")
        g.append("")
        for r in warns:
            g.append(f"- {r.name} run{r.run}: {r.error or r.alignment_warn}")
        g.append("")
    g.append("## 逐 seed 结果")
    g.append("")
    g.append("| seed | run | golden | decision | conclusion/conf | 观测 | 对齐 |")
    g.append("|---|---|---|---|---|---|---|")
    for r in records:
        g.append(
            f"| {r.name} | {r.run} | {r.golden_decision} | {r.decision} "
            f"| {r.conclusion}/{r.confidence} | {r.observed_event or '-'} "
            f"| {'WARN' if r.alignment_warn else 'ok'} |"
        )
    return "\n".join(g) + "\n"


# ---------- 自检 ----------


def selfcheck() -> int:
    """decide() 规则对齐 + 指标计算的 assert 自检（不连服务）。"""
    deny_kw = sorted(L2_ACTION_DENYLIST)[0]  # 用真实黑名单词构造，不硬编码假设
    cases = [
        # (conclusion, confidence, recs, expected, 说明)
        ("benign", 0.9, [{"action": "标记观察"}], "AUTO_PASS", "L1: 高置信benign+免审建议"),
        ("benign", 0.7, [{"action": "标记观察"}], "ESCALATE", "benign 低置信 -> 人工"),
        ("benign", 0.9, [], "ESCALATE", "benign 无建议 -> 人工"),
        ("benign", 0.9, [{"action": "解除遗留免密签约"}], "ESCALATE", "L1: 动作不在免审白名单 -> 人工"),
        ("malicious", 0.95, [{"action": f"{deny_kw}账号"}], "ESCALATE", "黑名单一票否决，高置信也不自动"),
        ("suspicious", 0.95, [{"action": "标记观察"}, {"action": "站内提醒"}], "ESCALATE", "L2: 仅通用伴随动作无处置级动作 -> 人工（处置深度锚点）"),
        ("suspicious", 0.85, [{"action": "标记观察"}], "ESCALATE", "低于 L2 阈值 -> 人工"),
        (
            "suspicious",
            0.95,
            [{"action": "标记观察", "requires_human_approval": True}, {"action": "限制领券功能"}],
            "AUTO_DISPOSE",
            "混合案分流：白名单命中子集自动 + 标true建议落审批，不整案卡死",
        ),
        ("inconclusive", 0.5, [{"action": "标记观察"}], "ESCALATE", "inconclusive -> 人工"),
        ("malicious", 0.95, [{"action": "标记观察"}, {"action": f"{deny_kw}卡"}], "ESCALATE", "多条建议任一命中黑名单即整案升级"),
        ("malicious", 0.95, [{"action": "标记观察"}, {"action": "发起风控处置工单"}], "AUTO_DISPOSE", "白名单命中 -> 自动"),
        ("malicious", 0.95, [{"action": "标记观察"}, {"action": "发起人工核查工单"}], "ESCALATE", "转人工语义动作不算自动处置（其语义即升级）"),
    ]
    for i, (c, conf, recs, exp, note) in enumerate(cases, 1):
        got = decide(c, conf, recs)
        assert got == exp, f"case{i} {note}: got {got}, want {exp}"
    assert len(L2_ACTION_DENYLIST) > 0, "L2_ACTION_DENYLIST 不应为空"
    # 指标计算小样例：手算对照
    recs = [
        _mk_record("s1", 1, "AUTO_PASS", "AUTO_PASS"),
        _mk_record("s2", 1, "ESCALATE", "AUTO_DISPOSE"),
        _mk_record("s3", 1, "AUTO_DISPOSE", "ESCALATE"),
        _mk_record("s4", 1, "ESCALATE", "ESCALATE"),
    ]
    mt = compute_metrics(recs, n_seeds=4, runs=1)
    assert mt["dispose_accuracy"] == 0.5, mt
    assert mt["risk_mal_dispose_rate"] == 0.5, mt
    assert mt["miss_release_rate"] == 0.0, mt
    assert mt["miss_dispose_rate"] == 1.0, mt
    assert mt["escalation_precision"] == 0.5, mt
    assert mt["escalation_recall"] == 0.5, mt
    assert mt["pass_k"] == 0.5, mt
    assert not mt["gates_pass"], mt
    print(f"selfcheck PASS: {len(cases)} decide-cases + metrics verified, denylist={list(L2_ACTION_DENYLIST)}")
    return 0


def _mk_record(name: str, run: int, golden: str, decision: str) -> DecisionResult:
    return DecisionResult(
        name=name, category="t", run=run, golden_decision=golden, decision=decision,
        observed_event="", alignment_warn="", conclusion="x", confidence=1.0,
        hit_conclusion=True, hit_confidence=True,
    )


# ---------- 主流程 ----------


async def amain(args: argparse.Namespace) -> int:
    seeds = load_seeds(Path(args.seed))
    if args.limit:
        seeds = seeds[: args.limit]
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    headers = _auth_headers(args.api_key, args.bearer)
    records: list[DecisionResult] = []
    t0 = time.perf_counter()
    async with httpx.AsyncClient() as client:
        for seed in seeds:
            for run in range(1, args.runs + 1):
                r = await run_one(client, args.base, args.api_key, args.bearer, seed, run, args.timeout)
                records.append(r)
                flag = "ok" if r.decision == r.golden_decision else "MISS"
                print(f"[{len(records)}/{len(seeds) * args.runs}] {seed.name} run{run} "
                      f"golden={r.golden_decision} decision={r.decision} ({flag}) {r.error or ''}")
    elapsed_s = time.perf_counter() - t0
    metrics = compute_metrics(records, n_seeds=len(seeds), runs=args.runs)
    report_json = out_dir / f"decision-eval-{ts}.json"
    report_json.write_text(
        json.dumps({"metrics": metrics, "records": [asdict(r) for r in records]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report_md = out_dir / f"decision-eval-{ts}.md"
    report_md.write_text(render_marketdown_report(metrics, records, elapsed_s), encoding="utf-8")
    print(f"\n报告: {report_json}\n      {report_md}")
    print(f"混淆矩阵: {metrics['confusion']}")
    print(f"dispose_accuracy={metrics['dispose_accuracy']} risk_mal={metrics['risk_mal_dispose_rate']} "
          f"miss_release={metrics['miss_release_rate']} miss_dispose={metrics['miss_dispose_rate']} "
          f"esc_recall={metrics['escalation_recall']} pass_{metrics['k']}={metrics['pass_k']}")
    print(f"门禁: {'PASS' if metrics['gates_pass'] else 'FAIL'} {metrics['gates']}")
    return 0 if metrics["gates_pass"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="处置决策评测：混淆矩阵 + pass^k")
    ap.add_argument("--base", default="http://localhost:8001")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--bearer", default="")
    ap.add_argument("--seed", default=str(DEFAULT_SEED))
    ap.add_argument("--output", default=str(Path(__file__).resolve().parents[2] / "evaluation" / "reports"))
    ap.add_argument("--runs", type=int, default=1, help="每 seed 独立重复次数（pass^k 的 k）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()
    if args.selfcheck:
        return selfcheck()
    _load_live_policy()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
