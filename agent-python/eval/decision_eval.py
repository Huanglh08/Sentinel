"""处置决策评测（ROADMAP 第四批 #12，2026-10-15 扩为两层）。

评测对象是**处置决策**（该自动 / 该升级 / 该放行），不是调查结论（那归 case_eval）。

两层架构:
- live 模式（默认，12 seed × runs）: 真跑 /case/investigate 全链，验证 decide() 复算与
  生产编排一致（alignment_warn）+ τ-bench pass^k。样本小、成本高，职责是**对齐校准**。
- 离线模式（--from-case-report）: 吃 case_eval 报告 JSON（需含 recommendations 字段，
  case_eval 2026-10-15 起保存），对全部 148 条零成本复算决策层指标。职责是**能力度量**，
  指标口径对齐 SIR-Bench（arXiv:2604.12040）+ SOC-bench 设计原则。

安全动作集 safe action set（spec，独立于实现；SOC-bench DP3 仅结果评分 + DP4 允许不行动）:
    malicious    -> {AUTO_DISPOSE, ESCALATE};  AUTO_PASS   = 恶性放行（灾难）
    benign       -> {AUTO_PASS, ESCALATE};     AUTO_DISPOSE = 误杀（灾难）
    inconclusive -> {ESCALATE};                AUTO_PASS / AUTO_DISPOSE = 灾难
ESCALATE 永不出灾难——不确定时交人工是安全侧（SOC-bench DP4"有据不行动"）。

SIR-Bench 口径指标（对照其自研 agent 基线 TP 97.1% / FP 排除 73.4%）:
- malicious_control_rate  恶性管控率（≈TP detection，门禁 >= 0.95）
- fp_rejection_rate       良性安全率（benign 不被误杀，门禁 >= 0.95）
- inconclusive_hold_rate  不确定留存率（必须升级人工，门禁 >= 0.9）
- harm_rate               灾难错误率（放走恶性 + 误杀良性/不确定）——硬门禁 = 0
- --oracle: 用 expect_conclusion 替代 agent 结论复算，隔离"结论错误传导"，
  与端到端口径相减即结论层缺陷贡献（对应 SIR-Bench M1 分解）。

live 指标（保留）:
- dispose_accuracy / risk_mal_dispose_rate(=0) / risk_ben_dispose_rate /
  miss_release_rate(=0) / miss_dispose_rate / escalation_precision /
  escalation_recall(>=0.9) / pass_k(>=0.8, arXiv:2406.12045)

运行:
    python -m eval.decision_eval --selfcheck
    python -m eval.decision_eval --runs 3                # live: 12 seed × 3
    python -m eval.decision_eval --from-case-report <case报告.json> [--oracle]
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
from core.case.types import L2_ACTION_DENYLIST
from core.case._action_rights import default_config as rights_default_config, split_recs

DECISIONS = ("AUTO_PASS", "AUTO_DISPOSE", "ESCALATE")
DEFAULT_SEED = Path(__file__).resolve().parents[2] / "evaluation" / "seeds" / "decision-seeds.jsonl"
DEFAULT_CASE_SEEDS = Path(__file__).resolve().parents[2] / "evaluation" / "seeds" / "case-seeds.jsonl"

# 安全动作集（spec，见模块 docstring；独立于 decide() 实现，调参不改 spec）
SAFE_SETS: dict[str, set[str]] = {
    "malicious": {"AUTO_DISPOSE", "ESCALATE"},
    "benign": {"AUTO_PASS", "ESCALATE"},
    "inconclusive": {"ESCALATE"},
}
CONCLUSIONS = ("malicious", "benign", "inconclusive")

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
    """复算处置决策。golden 语义 = 规则正确决策，与两端 autopilot 开关无关。

    分流直接调用生产同一个函数 `_action_rights.split_recs`（档位 × 动作域 × 触发器），
    不再在这里复刻白名单逻辑——两处各写一份是此前"评测口径与生产漂移"的来源。
    管理员白名单仍作为额外收紧（与 agent._split_auto 一致）。
    """
    recs = [r for r in (recommendations or []) if isinstance(r, dict)]
    actions = [str(r["action"]) for r in recs if r.get("action")]
    # denylist 一票否决：高危案零自动处置，整案人工（对齐编排 deny_hit 分支）
    if any(kw in a for a in actions for kw in denylist):
        return "ESCALATE"
    if not actions:
        return "ESCALATE"
    cfg = rights_default_config()
    cfg["triggers"]["benign_confidence"] = l1_threshold
    cfg["triggers"]["l2_confidence"] = l2_threshold
    cfg["triggers"]["l2_autopilot_enabled"] = True  # golden 只判策略能力，不看开关
    auto, _pending = split_recs(recs, conclusion, confidence, cfg)
    if not auto:
        return "ESCALATE"
    wl = l1_whitelist if conclusion == "benign" else whitelist
    if not any(any(kw in str(r["action"]) for kw in wl) for r in auto):
        return "ESCALATE"
    return "AUTO_PASS" if conclusion == "benign" else "AUTO_DISPOSE"


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


# ---------- 离线决策层评测（--from-case-report，口径对齐 SIR-Bench） ----------


def load_case_report(report_path: Path, seeds_path: Path) -> list[dict]:
    """吃 case_eval 报告 JSON + case-seeds.jsonl，产出决策层输入记录。

    仅收无 error 且命中过调查报告的案；coverage 标签从 seeds 原始行取（分桶用）。
    """
    data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    seeds: dict[str, dict] = {}
    for line in Path(seeds_path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            s = json.loads(line)
            seeds[str(s["name"]).strip()] = s
    out: list[dict] = []
    skipped = 0
    for c in data.get("cases", []):
        s = seeds.get(str(c.get("name", "")))
        # "recommendations" 键缺失 = 报告生成自旧版 case_eval，决策输入不完整，
        # 静默复算会产出"全 ESCALATE 全安全"的假报告——必须整批拒绝
        if s is None or c.get("error") or "recommendations" not in c:
            skipped += 1
            continue
        out.append(
            {
                "name": str(c["name"]),
                "category": str(c.get("category", "general")),
                "conclusion": str(c.get("predicted_conclusion", "")).lower(),
                "confidence": float(c.get("confidence") or 0.0),
                "recommendations": [r for r in (c.get("recommendations") or []) if isinstance(r, dict)],
                "expect_conclusion": str(s["expect_conclusion"]).strip().lower(),
                "coverage": dict(s.get("coverage") or {}),
            }
        )
    if not out:
        raise ValueError(
            f"无可用记录: {report_path}（共 {len(data.get('cases', []))} 条, 跳过 {skipped} 条）。"
            "离线模式需要 case_eval >= 2026-10-15 的报告（含 recommendations 字段）"
        )
    return out


def decide_records(records: list[dict], *, oracle: bool = False) -> list[dict]:
    """对记录批量复算决策。oracle=True 用 expect_conclusion 替代 agent 结论（隔离结论错误传导）。"""
    out = []
    for r in records:
        r = dict(r)
        if oracle:
            r["conclusion"] = r["expect_conclusion"]
        r["decision"] = decide(r["conclusion"], r["confidence"], r["recommendations"], **_LIVE)
        out.append(r)
    return out


def compute_sir_metrics(records: list[dict], *, oracle: bool = False) -> dict:
    """SIR-Bench 口径指标：安全集判定 + 灾难错误 + coverage 分桶。

    记录须已经 decide_records() 处理（带 decision 字段）。
    """
    confusion = {g: {d: 0 for d in DECISIONS} for g in CONCLUSIONS}
    harms: list[dict] = []
    for r in records:
        gc, dec = r["expect_conclusion"], r["decision"]
        confusion[gc][dec] += 1
        if dec not in SAFE_SETS.get(gc, set()):
            harms.append(
                {
                    "name": r["name"],
                    "expect_conclusion": gc,
                    "decision": dec,
                    "kind": "release" if dec == "AUTO_PASS" else "dispose",
                    "agent_conclusion": r["conclusion"],
                    "category": r["category"],
                }
            )
    n_mal = sum(confusion["malicious"].values())
    n_ben = sum(confusion["benign"].values())
    n_inc = sum(confusion["inconclusive"].values())
    total = n_mal + n_ben + n_inc or 1

    def _rate(numer: int, denom: int) -> float | None:
        return round(numer / denom, 4) if denom else None

    metrics = {
        "samples": total,
        "oracle": oracle,
        "confusion": confusion,
        "n_malicious": n_mal,
        "n_benign": n_ben,
        "n_inconclusive": n_inc,
        # SIR-Bench M1 口径：TP detection 基线 97.1%
        "malicious_control_rate": _rate(n_mal - confusion["malicious"]["AUTO_PASS"], n_mal),
        # SIR-Bench FP rejection 口径（其告警分诊基线 73.4%；benign 案要求更高）
        "fp_rejection_rate": _rate(n_ben - confusion["benign"]["AUTO_DISPOSE"], n_ben),
        # SOC-bench DP4：不确定必须留存人工
        "inconclusive_hold_rate": _rate(confusion["inconclusive"]["ESCALATE"], n_inc),
        # 灾难错误（放走恶性 + 误杀良性/不确定），硬门禁 = 0
        "harm_rate": _rate(len(harms), total),
        "harms": harms,
    }
    by_bucket: dict[str, dict[str, list]] = {}
    for r in records:
        cov = r.get("coverage") or {}
        for key, val in (
            ("evidence", str(cov.get("evidence_strength", "-"))),
            ("disturbance", str(cov.get("disturbance", "-"))),
            ("subject", str(r.get("category", "-"))),
        ):
            cell = by_bucket.setdefault(key, {}).setdefault(str(val), [0, 0])  # [safe, total]
            cell[0] += 1 if r["decision"] in SAFE_SETS.get(r["expect_conclusion"], set()) else 0
            cell[1] += 1
    metrics["buckets"] = {
        k: {v: {"safe": c[0], "total": c[1], "safe_rate": round(c[0] / c[1], 4) if c[1] else None} for v, c in vs.items()}
        for k, vs in by_bucket.items()
    }
    gates = {
        "harm_zero": metrics["harm_rate"] == 0.0,
        # None = 该结论组无样本（N/A 不裁），全量 seeds 三组均非空
        "malicious_control_ge_0.95": metrics["malicious_control_rate"] is None or metrics["malicious_control_rate"] >= 0.95,
        "fp_rejection_ge_0.95": metrics["fp_rejection_rate"] is None or metrics["fp_rejection_rate"] >= 0.95,
        "inconclusive_hold_ge_0.9": metrics["inconclusive_hold_rate"] is None or metrics["inconclusive_hold_rate"] >= 0.9,
    }
    metrics["gates"] = gates
    metrics["gates_pass"] = all(gates.values())
    return metrics


def render_sir_report(m_e2e: dict, m_oracle: dict, elapsed_s: float, report_src: str) -> str:
    g = ["# 离线决策层评测报告（decision_eval --from-case-report）", ""]
    g.append(
        f"- 来源: `{report_src}`，可用记录 {m_e2e['samples']} 条"
        f"（malicious {m_e2e['n_malicious']} / benign {m_e2e['n_benign']} / inconclusive {m_e2e['n_inconclusive']}），耗时 {elapsed_s:.0f}s"
    )
    g.append("- 口径: 安全动作集 safe action set（SOC-bench DP3/DP4）+ SIR-Bench M1 指标（其自研 agent 基线 TP 97.1% / FP 排除 73.4%）")
    g.append(f"- 端到端门禁: {'**PASS**' if m_e2e['gates_pass'] else '**FAIL**'} | oracle 门禁: {'**PASS**' if m_oracle['gates_pass'] else '**FAIL**'}")
    g.append("")
    g.append("| 指标 | 端到端 | oracle(结论判对) | 门禁 | SIR-Bench 基线 |")
    g.append("|---|---|---|---|---|")
    gate_key = {
        "malicious_control_rate": "malicious_control_ge_0.95",
        "fp_rejection_rate": "fp_rejection_ge_0.95",
        "inconclusive_hold_rate": "inconclusive_hold_ge_0.9",
        "harm_rate": "harm_zero",
    }
    for key, label, gate, ref in (
        ("malicious_control_rate", "恶性管控率", ">= 0.95", "TP 97.1%"),
        ("fp_rejection_rate", "良性安全率", ">= 0.95", "FP 排除 73.4%"),
        ("inconclusive_hold_rate", "不确定留存率", ">= 0.9", "-"),
        ("harm_rate", "灾难错误率", "= 0", "-"),
    ):
        g.append(f"| {label} | {m_e2e[key]} | {m_oracle[key]} | {gate} | {ref} |")
    g.append("")
    if m_e2e["harms"]:
        g.append(f"## 灾难明细（端到端 {len(m_e2e['harms'])} 条）")
        g.append("")
        g.append("| 案 | 期望结论 | 决策 | 类型 | agent 实际结论 |")
        g.append("|---|---|---|---|---|")
        for h in m_e2e["harms"]:
            g.append(f"| {h['name']} | {h['expect_conclusion']} | {h['decision']} | {h['kind']} | {h['agent_conclusion']} |")
        g.append("")
    o_delta = set(h["name"] for h in m_oracle["harms"]) - set(h["name"] for h in m_e2e["harms"])
    g.append("## 结论错误传导分析")
    g.append("")
    g.append(
        f"- oracle 模式（结论判对后复算）灾难 {len(m_oracle['harms'])} 条 vs 端到端 {len(m_e2e['harms'])} 条"
        f" → 决策映射自身缺陷 {len(m_oracle['harms'])} 条，结论错误传导 {len(m_e2e['harms']) - len(m_oracle['harms'])} 条"
    )
    if o_delta:
        g.append(f"- 仅 oracle 出灾难（决策映射自身缺陷，需修映射/建议质量）: {', '.join(sorted(o_delta))}")
    g.append("")
    g.append("## coverage 分桶安全率（端到端口径）")
    g.append("")
    for key, buckets in m_e2e["buckets"].items():
        parts = "、".join(f"{v}={c['safe_rate']:.0%}({c['safe']}/{c['total']})" for v, c in sorted(buckets.items(), key=lambda kv: -kv[1]["total"]))
        g.append(f"- **{key}**: {parts}")
    return "\n".join(g) + "\n"


def run_offline(report_path: Path, seeds_path: Path, out_dir: Path, oracle: bool = False) -> int:
    t0 = time.perf_counter()
    _load_live_policy()
    records = load_case_report(report_path, seeds_path)
    m_e2e = compute_sir_metrics(decide_records(records))
    m_oracle = compute_sir_metrics(decide_records(records, oracle=True), oracle=True)
    elapsed_s = time.perf_counter() - t0
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    tag = "decision-offline" + ("-oracle" if oracle else "")
    (out_dir / f"{tag}-{ts}.json").write_text(
        json.dumps({"e2e": m_e2e, "oracle": m_oracle}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md = out_dir / f"{tag}-{ts}.md"
    md.write_text(render_sir_report(m_e2e, m_oracle, elapsed_s, str(report_path)), encoding="utf-8")
    print(f"报告: {md}")
    print(
        f"e2e: mal_ctrl={m_e2e['malicious_control_rate']} fp_rej={m_e2e['fp_rejection_rate']} "
        f"inc_hold={m_e2e['inconclusive_hold_rate']} harm={m_e2e['harm_rate']} | "
        f"oracle: harm={m_oracle['harm_rate']}"
    )
    print(f"门禁: {'PASS' if m_e2e['gates_pass'] else 'FAIL'} {m_e2e['gates']}")
    return 0 if m_e2e["gates_pass"] else 1


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
    # SIR 安全集指标手算对照（ SOC-bench DP3/DP4 + SIR-Bench M1 口径）
    sir_records = [
        {"name": "m1", "category": "c", "conclusion": "malicious", "confidence": 0.95,
         "recommendations": [{"action": "冻结账号"}], "expect_conclusion": "malicious", "coverage": {}},
        {"name": "m2", "category": "c", "conclusion": "benign", "confidence": 0.90,
         "recommendations": [{"action": "标记观察"}], "expect_conclusion": "malicious", "coverage": {}},  # 放走恶性=灾难
        {"name": "b1", "category": "c", "conclusion": "benign", "confidence": 0.90,
         "recommendations": [{"action": "标记观察"}], "expect_conclusion": "benign", "coverage": {}},
        {"name": "b2", "category": "c", "conclusion": "malicious", "confidence": 0.95,
         "recommendations": [{"action": "限制领券功能"}], "expect_conclusion": "benign", "coverage": {}},  # 误杀=灾难
        {"name": "i1", "category": "c", "conclusion": "inconclusive", "confidence": 0.40,
         "recommendations": [{"action": "标记观察"}], "expect_conclusion": "inconclusive", "coverage": {}},
        {"name": "i2", "category": "c", "conclusion": "benign", "confidence": 0.90,
         "recommendations": [{"action": "标记观察"}], "expect_conclusion": "inconclusive", "coverage": {}},  # 放走不确定=灾难
    ]
    for r in sir_records:
        r["decision"] = decide(r["conclusion"], r["confidence"], r["recommendations"])
    sm = compute_sir_metrics(sir_records)
    assert sm["samples"] == 6 and sm["n_malicious"] == 2 and sm["n_benign"] == 2 and sm["n_inconclusive"] == 2, sm
    assert sm["malicious_control_rate"] == 0.5, sm   # m1 管控, m2 放走
    assert sm["fp_rejection_rate"] == 0.5, sm        # b1 安全, b2 误杀
    assert sm["inconclusive_hold_rate"] == 0.5, sm   # i1 留存, i2 放走
    assert sm["harm_rate"] == 0.5, sm                # 3 灾难 / 6
    assert {h["name"] for h in sm["harms"]} == {"m2", "b2", "i2"}, sm
    assert not sm["gates_pass"], sm
    # oracle 模式：结论替换后 m2/b2/i2 全部回到安全集，仅决策映射自身缺陷才出灾难
    oracle_records = [dict(r) for r in sir_records]
    sm_o = compute_sir_metrics(decide_records(oracle_records, oracle=True), oracle=True)
    assert sm_o["harm_rate"] == 0.0 and sm_o["gates_pass"], sm_o
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
    ap = argparse.ArgumentParser(description="处置决策评测：live 全链对齐 + 离线 SIR 口径能力度量")
    ap.add_argument("--base", default="http://localhost:8001")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--bearer", default="")
    ap.add_argument("--seed", default=str(DEFAULT_SEED))
    ap.add_argument("--from-case-report", default="", help="离线模式: case_eval 报告 JSON 路径（需含 recommendations）")
    ap.add_argument("--case-seeds", default=str(DEFAULT_CASE_SEEDS), help="离线模式: case seeds 路径（取 expect_conclusion/coverage）")
    ap.add_argument("--oracle", action="store_true", help="离线模式附加: 用 expect_conclusion 替代 agent 结论复算")
    ap.add_argument("--output", default=str(Path(__file__).resolve().parents[2] / "evaluation" / "reports"))
    ap.add_argument("--runs", type=int, default=1, help="每 seed 独立重复次数（pass^k 的 k）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()
    if args.selfcheck:
        return selfcheck()
    if args.from_case_report:
        return run_offline(Path(args.from_case_report), Path(args.case_seeds), Path(args.output), oracle=args.oracle)
    _load_live_policy()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
