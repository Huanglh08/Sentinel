# -*- coding: utf-8 -*-
"""LLM-as-a-Judge 校准门禁 v2（Cohen's κ + MAD 双指标，ARES 式思想延续）。

v2 变更：以三档分档（BAND_BOUNDS）上的 Cohen's κ 为主指标、MAD 为门禁指标，
替代 v1 的点级一致率门禁（后者保留为参考读数）。κ 是 chance-corrected 一致性
度量，可抵消分数堆积高档造成的虚高一致率；MAD 补充幅度信息。

三段工作流：
  # 阶段 1：构建 golden（从最近 e2e JSON 报告分层抽样，重跑 chat 固化三元组，judge 预打分）
  python -m eval.judge_calibration --stage build --samples 20

  # 人工复核：编辑 evaluation/seeds/judge-calibration.jsonl，
  # 逐条填写 human_faithfulness / human_answer_relevancy（0~1 分），
  # 判断标准见 rubrics.py 各 rubric；judge 分仅作参考，须独立判断。

  # 阶段 2：judge 用当前 rubric 重打分，输出 κ/MAD/点级一致率 + 仲裁清单
  python -m eval.judge_calibration --stage run

  # 阶段 3：regate（零 LLM 成本）——合并 N 轮 run 报告，golden human 覆盖快照后
  # 重算 κ 均值 / MAD 门禁 + 稳定分歧仲裁清单（修 golden / rubric 后必跑）
  python -m eval.judge_calibration --stage regate --reports A.json,B.json

门禁：κ >= 0.6 且 MAD <= 0.08；单轮 κ 受 judge 随机方差影响大，门禁判定以
regate 多轮均值为准。分歧 |Δ| > 0.20 列仲裁清单（ARBITER_MODEL 裁定），
仲裁结果用于修订 rubric 锚点或 golden 后重测。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
import urllib.parse
import warnings
from pathlib import Path

import httpx
from sklearn.metrics import cohen_kappa_score

from eval.e2e_eval import _is_refused, _login, _source_doc_names, _stream_chat
from eval.judge import (
    JudgeClient,
    JudgeConfig,
    SYSTEM_ANSWER_RELEVANCY,
    SYSTEM_FAITHFULNESS,
    build_user_prompt,
    normalize_result,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PATH = REPO_ROOT / "evaluation" / "seeds" / "judge-calibration.jsonl"
REPORT_DIR = REPO_ROOT / "evaluation" / "reports"

# 一致点容差与门禁（v2：κ 为主指标 + MAD 门禁；点级一致率保留为参考读数）
TOLERANCE = 0.10
# 分歧超此值进仲裁清单
ARBITER_DELTA = 0.20
# run 阶段报告用的一致率参考线（v2 门禁已改 κ+MAD，此值仅作单轮读数展示，不作门禁）
AGREEMENT_GATE = 0.90
# v2 门禁：三档 Cohen's κ 与 MAD（单轮读数；门禁判定以 --stage regate 多轮均值为准）
KAPPA_GATE = 0.60
MAD_GATE = 0.08
# κ 三档分界：low < 0.5 <= mid <= 0.85 < high
BAND_BOUNDS = (0.5, 0.85)


def _band(x: float) -> int:
    if x < BAND_BOUNDS[0]:
        return 0
    if x <= BAND_BOUNDS[1]:
        return 1
    return 2


def _cohen_kappa(js: list[float], hs: list[float]) -> float:
    """三档 Cohen's κ（chance-corrected）；完美一致/退化分布时 sklearn 告警或除零，退回 1.0。"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return float(cohen_kappa_score([_band(x) for x in js], [_band(x) for x in hs]))
        except Exception:  # noqa: BLE001
            return 1.0


def _calibration_metrics(points: list[dict]) -> dict:
    """单轮读数：κ / MAD / 点级一致率（参考）。"""
    js = [p["judge"] for p in points]
    hs = [p["human"] for p in points]
    n = len(points)
    return {
        "kappa": round(_cohen_kappa(js, hs), 4),
        "mad": round(sum(abs(j - h) for j, h in zip(js, hs)) / n, 4),
        "agreement_rate": round(sum(1 for j, h in zip(js, hs) if abs(j - h) <= TOLERANCE) / n, 4),
        "n": n,
    }


def _latest_e2e_report(reports_dir: Path) -> Path:
    cands = [p for p in reports_dir.glob("e2e*.json") if "mini" not in p.name and "replay" not in p.name]
    if not cands:
        raise FileNotFoundError(f"{reports_dir} 下无 e2e*.json 报告，先跑一次 e2e_eval")
    return max(cands, key=lambda p: p.stat().st_mtime)


def _stratified_sample(cases: list[dict], n: int, seed: int = 42) -> list[dict]:
    """分层抽样：按 min(rel, faith) 分高/中/低三层，目标 10/6/4，不足自动从相邻层补齐。"""
    pool = [
        c for c in cases
        if not c.get("error") and not c.get("expected_refusal")
        and (c.get("answer_relevancy") is not None or c.get("faithfulness") is not None)
    ]
    scored = []
    for c in pool:
        vals = [v for v in (c.get("answer_relevancy"), c.get("faithfulness")) if v is not None]
        m = min(vals) if vals else 0.0
        scored.append((c, m))
    high = [c for c, m in scored if m >= 0.9]
    mid = [c for c, m in scored if 0.5 <= m < 0.9]
    low = [c for c, m in scored if m < 0.5]
    rng = random.Random(seed)
    for layer in (high, mid, low):
        rng.shuffle(layer)
    quota = {"high": max(0, int(n * 0.5)), "mid": max(0, int(n * 0.3)), "low": max(0, n - int(n * 0.5) - int(n * 0.3))}
    picked: list[dict] = []
    for name in ("high", "mid", "low"):
        layer = {"high": high, "mid": mid, "low": low}[name]
        take = min(quota[name], len(layer))
        picked.extend(layer[:take])
        layer[:] = layer[take:]
    # 配额不足时逐层回填
    rest = high + mid + low
    while len(picked) < n and rest:
        picked.append(rest.pop(0))
    return picked[:n]


async def _fetch_contexts(
    client: httpx.AsyncClient, agent_base: str, question: str
) -> list[dict[str, str]]:
    """调 agent-python /debug/retrieval 取完整检索 chunk 全文（与 chat 主链同构，与 e2e_eval 同源）。"""
    url = f"{agent_base}/debug/retrieval?{urllib.parse.urlencode({'question': question, 'rewrite': 'true'})}"
    try:
        resp = await client.get(url, timeout=60.0)
        resp.raise_for_status()
        payload = resp.json()
        ctxs = []
        for i, c in enumerate(payload.get("final") or []):
            text = str(c.get("text") or "")
            if not text.strip():
                continue
            doc = str(c.get("doc_name") or c.get("doc_id") or f"chunk_{i}")
            ctxs.append({"doc_id": doc, "text": text})
        return ctxs
    except Exception as e:  # noqa: BLE001
        print(f"[judge_calibration] debug/retrieval 取上下文失败: {e}", file=sys.stderr)
        return []


async def _stage_build(args) -> int:
    report_path = Path(args.report) if args.report else _latest_e2e_report(Path(args.output))
    data = json.loads(report_path.read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    picked = _stratified_sample(cases, args.samples)
    print(f"[judge_calibration] 从 {report_path.name} 分层抽取 {len(picked)} 条", file=sys.stderr)
    if len(picked) < args.samples:
        # 报告池不足时从 retrieval-seeds 补齐（in-scope、与已抽题去重、固定 seed 打乱以均匀覆盖类目）。
        # 补充题无 report 分数（置 None），其余流程一致：现跑 chat + rag/eval + judge 预打分。
        need = args.samples - len(picked)
        extra_path = report_path.parent.parent / "seeds" / "retrieval-seeds.jsonl"
        used_q = {c["question"].strip() for c in picked}
        extras: list[dict] = []
        if extra_path.exists():
            for line in extra_path.read_text(encoding="utf-8-sig").splitlines():
                if not line.strip():
                    continue
                r = json.loads(line)
                if r.get("why_out_of_scope") or r["q"].strip() in used_q:
                    continue
                extras.append({"question": r["q"], "answer_relevancy": None, "faithfulness": None})
        random.Random(42).shuffle(extras)
        # ponytail: 预留跳过余量（补充题约 15% 召不回上下文被跳），超采 20 条，
        # 跑完若超出 samples 手动截断 golden；升级路径=循环内按落盘数停
        picked.extend(extras[: need + 20])
        print(
            f"[judge_calibration] 报告池不足，从 retrieval-seeds 补 {min(need, len(extras))} 条 → 总 {len(picked)} 条",
            file=sys.stderr,
        )

    judge_cfg = JudgeConfig.from_env()
    if not judge_cfg.api_key:
        print("[judge_calibration] 未配置 judge API key，无法预打分", file=sys.stderr)
        return 1
    judge = JudgeClient(judge_cfg)

    # 断点续跑：partial 文件里已完成的 question 直接跳过（ponytail: 按题面去重，
    # seed 内 question 唯一；换源报告重跑需手动删 .partial.jsonl）
    partial_path = GOLDEN_PATH.with_name(GOLDEN_PATH.stem + ".partial.jsonl")
    golden: list[dict] = []
    done_q: set[str] = set()
    if partial_path.exists():
        for line in partial_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                g = json.loads(line)
                golden.append(g)
                done_q.add(g["question"])
        print(f"[judge_calibration] 断点续跑: partial 已有 {len(golden)} 条", file=sys.stderr)

    def _s(x) -> str:
        return f"{x:.2f}" if x is not None else "-"

    async with httpx.AsyncClient() as client:
        token = await _login(client, args.gateway, args.username, args.password)
        for i, c in enumerate(picked, 1):
            q = c["question"]
            if q in done_q:
                continue
            stream = await _stream_chat(client, f"{args.gateway}/rag/v3/chat", token, q, args.timeout)
            full_text = stream["full_text"]
            if stream["error"] or not full_text.strip() or _is_refused(full_text, stream["sources"]):
                print(f"[judge_calibration] {i}/{len(picked)} 跳过（错误或拒答）: {q[:30]}", file=sys.stderr)
                continue
            contexts = await _fetch_contexts(client, args.agent, q)
            if not contexts:
                print(f"[judge_calibration] {i}/{len(picked)} 跳过（无上下文）: {q[:30]}", file=sys.stderr)
                continue
            # 单条 judge 失败只丢该条 machine 分（human 仍可打），不炸全程
            rel_res = {"score": None}
            try:
                rel_data = await judge.judge(
                    task="answer_relevancy",
                    system=SYSTEM_ANSWER_RELEVANCY,
                    user=build_user_prompt("answer_relevancy", question=q, answer=full_text),
                )
                rel_res = normalize_result("answer_relevancy", rel_data)
            except Exception as e:  # noqa: BLE001
                print(f"[judge_calibration] {i}/{len(picked)} judge rel 失败（容错继续）: {e}", file=sys.stderr)
            faith_res = {"score": None}
            try:
                f_data = await judge.judge(
                    task="faithfulness",
                    system=SYSTEM_FAITHFULNESS,
                    user=build_user_prompt("faithfulness", question=q, answer=full_text, contexts=contexts),
                )
                faith_res = normalize_result("faithfulness", f_data)
            except Exception as e:  # noqa: BLE001
                print(f"[judge_calibration] {i}/{len(picked)} judge faith 失败（容错继续）: {e}", file=sys.stderr)
            rec = {
                "id": f"JC{len(golden) + 1:02d}",
                "question": q,
                "answer": full_text,
                "contexts": contexts,
                "report_answer_relevancy": c.get("answer_relevancy"),
                "report_faithfulness": c.get("faithfulness"),
                "machine_answer_relevancy": rel_res["score"],
                "machine_faithfulness": faith_res["score"],
                "human_answer_relevancy": None,
                "human_faithfulness": None,
                "notes": "",
            }
            golden.append(rec)
            with partial_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(
                f"[judge_calibration] {i}/{len(picked)} {rec['id']} "
                f"judge(rel={_s(rel_res['score'])}, faith={_s(faith_res['score'])}) "
                f"q='{q[:30]}'",
                file=sys.stderr,
            )

    partial_path.replace(GOLDEN_PATH)
    print(f"[judge_calibration] golden 写入 {GOLDEN_PATH}（{len(golden)} 条）", file=sys.stderr)
    print(
        "\n=== 人工复核指南 ===\n"
        f"1. 打开 {GOLDEN_PATH}\n"
        "2. 逐条阅读 question / answer / contexts，独立判断后填写：\n"
        "   - human_answer_relevancy: 回答是否回应问题字面诉求（0~1，rubric 见 SYSTEM_ANSWER_RELEVANCY）\n"
        "   - human_faithfulness: 回答断言可被 contexts 支撑的比例（0~1，rubric 见 SYSTEM_FAITHFULNESS）\n"
        "   - notes: 可选，记分歧原因\n"
        "3. machine_* 是 judge 预打分，仅供参考，不要被锚定\n"
        "4. 填完后运行: python -m eval.judge_calibration --stage run",
        file=sys.stderr,
    )
    return 0


async def _stage_run(args) -> int:
    if not GOLDEN_PATH.exists():
        print(f"[judge_calibration] golden 不存在: {GOLDEN_PATH}，先跑 --stage build", file=sys.stderr)
        return 1
    golden = []
    for line in GOLDEN_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            golden.append(json.loads(line))
    missing = [
        g["id"] for g in golden
        if g.get("human_answer_relevancy") is None and g.get("human_faithfulness") is None
    ]
    if missing:
        print(
            f"[judge_calibration] {len(missing)} 条未填人工分（如 {missing[:5]}），golden 未完成复核",
            file=sys.stderr,
        )
        return 1

    subset_note = ""
    if args.ids:
        wanted = {s.strip().upper() for s in args.ids.split(",") if s.strip()}
        golden = [g for g in golden if g["id"].upper() in wanted]
        subset_note += f"--ids 指定，命中 {len(golden)} 条"
    if args.diff_only:
        ref = Path(args.diff_only)
        wanted = {p["id"] for p in json.loads(ref.read_text(encoding="utf-8"))["points"] if not p["agree"]}
        golden = [g for g in golden if g["id"] in wanted]
        subset_note += f"--diff-only（基线 {ref.stem}）不一致点涉及 {len(golden)} 条样本"
    if subset_note:
        print(f"[judge_calibration] 子集模式: {subset_note}", file=sys.stderr)

    metrics_run = args.metrics
    run_rel = metrics_run in ("all", "rel")
    run_faith = metrics_run in ("all", "faith")
    if metrics_run != "all":
        print(f"[judge_calibration] 单维度模式: 只跑 {'answer_relevancy' if run_rel else 'faithfulness'}", file=sys.stderr)

    judge_cfg = JudgeConfig.from_env()
    if not judge_cfg.api_key:
        print("[judge_calibration] 未配置 judge API key", file=sys.stderr)
        return 1
    judge = JudgeClient(judge_cfg)

    points: list[dict] = []  # 每个 = 一条样本 × 一个维度的判定点
    for gi, g in enumerate(golden, 1):
        if run_rel and g.get("human_answer_relevancy") is not None:
            try:
                rel_data = await judge.judge(
                    task="answer_relevancy",
                    system=SYSTEM_ANSWER_RELEVANCY,
                    user=build_user_prompt("answer_relevancy", question=g["question"], answer=g["answer"]),
                )
                rel_res = normalize_result("answer_relevancy", rel_data)
            except Exception as e:  # noqa: BLE001
                print(f"[judge_calibration] {g['id']} rel judge 失败（跳过该判定点）: {e}", file=sys.stderr)
                rel_res = None
            if rel_res is not None:
                points.append({
                    "id": g["id"], "metric": "answer_relevancy",
                    "judge": rel_res["score"], "human": float(g["human_answer_relevancy"]),
                    "reason": rel_res["reason"], "notes": g.get("notes", ""),
                    "coverage_answered": rel_res.get("answered"),
                    "coverage_partial": rel_res.get("partial"),
                    "coverage_total": rel_res.get("demand_total"),
                })
        if run_faith and g.get("human_faithfulness") is not None and g.get("contexts"):
            try:
                f_data = await judge.judge(
                    task="faithfulness",
                    system=SYSTEM_FAITHFULNESS,
                    user=build_user_prompt("faithfulness", question=g["question"], answer=g["answer"], contexts=g["contexts"]),
                )
                f_res = normalize_result("faithfulness", f_data)
            except Exception as e:  # noqa: BLE001
                print(f"[judge_calibration] {g['id']} faith judge 失败（跳过该判定点）: {e}", file=sys.stderr)
                f_res = None
            if f_res is not None:
                points.append({
                    "id": g["id"], "metric": "faithfulness",
                    "judge": f_res["score"], "human": float(g["human_faithfulness"]),
                    "reason": f_res["reason"], "notes": g.get("notes", ""),
                })
        # 逐点进度（stderr，供后台监控窗口解析；无判定点也输出，防监控饿死）
        print(f"[judge_calibration] point {gi}/{len(golden)} {g['id']}", file=sys.stderr, flush=True)

    for p in points:
        p["delta"] = round(abs(p["judge"] - p["human"]), 4)
        p["agree"] = p["delta"] <= TOLERANCE

    total = len(points)
    agree = sum(1 for p in points if p["agree"])
    rate = agree / total if total else 0.0
    by_metric: dict[str, list[dict]] = {}
    for p in points:
        by_metric.setdefault(p["metric"], []).append(p)
    disagreements = [p for p in points if p["delta"] > ARBITER_DELTA]
    passed = rate >= AGREEMENT_GATE

    # 相关性（ARES 亦报告 judge-human 相关；样本小时仅作参考）
    if len(points) >= 3:
        xs = [p["human"] for p in points]
        ys = [p["judge"] for p in points]
        try:
            pearson = statistics.correlation(xs, ys)
        except statistics.StatisticsError:
            pearson = None
    else:
        pearson = None

    ts = time.strftime("%Y%m%d-%H%M%S")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORT_DIR / f"judge-calibration-{ts}.json"
    json_path.write_text(
        json.dumps(
            {
                "agreement_rate": round(rate, 4),
                "gate": AGREEMENT_GATE,
                "tolerance": TOLERANCE,
                "passed": passed,
                "total_points": total,
                "agree_points": agree,
                "pearson": round(pearson, 4) if pearson is not None else None,
                "by_metric": {
                    m: {
                        "rate": round(sum(1 for p in ps if p["agree"]) / len(ps), 4),
                        "mean_delta": round(statistics.mean(p["delta"] for p in ps), 4),
                    }
                    for m, ps in by_metric.items()
                },
                "arbitration_list": disagreements,
                "points": points,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    md = ["# Judge Calibration Report（ARES 式 golden 一致率）", ""]
    md.append(f"- Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    md.append(f"- Golden: `{GOLDEN_PATH.name}`（{len(golden)} 条样本 / {total} 个判定点）")
    md.append(f"- 判定模型: `{judge_cfg.model}`")
    md.append("")
    md.append("## 一致率门禁")
    md.append("")
    md.append("| 维度 | 一致率 | 门禁 | 状态 |")
    md.append("|---|---|---|---|")
    md.append(f"| **总体** | **{rate:.2%}** ({agree}/{total}) | >= {AGREEMENT_GATE:.0%} | {'**PASS**' if passed else '**FAIL**'} |")
    for m, ps in by_metric.items():
        r = sum(1 for p in ps if p["agree"]) / len(ps)
        md.append(f"| {m} | {r:.2%} ({sum(1 for p in ps if p['agree'])}/{len(ps)}) | - | {'PASS' if r >= AGREEMENT_GATE else 'FAIL'} |")
    if pearson is not None:
        md.append("")
        md.append(f"- Pearson(judge, human) = {pearson:.4f}（参考）")
    if disagreements:
        md.append("")
        md.append(f"## 仲裁清单（|Δ| > {ARBITER_DELTA}，{len(disagreements)} 条）")
        md.append("")
        md.append("| id | metric | judge | human | Δ | notes |")
        md.append("|---|---|---|---|---|---|")
        for p in disagreements:
            md.append(f"| {p['id']} | {p['metric']} | {p['judge']:.2f} | {p['human']:.2f} | {p['delta']:.2f} | {p['notes']} |")
        md.append("")
        md.append(f"仲裁方式：用 ARBITER_MODEL 对上述条目三方裁定（judge / human / arbiter 少数服从多数），据结果修订 rubrics.py 锚点后重测。")
    md.append("")
    md_path = REPORT_DIR / f"judge-calibration-{ts}.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    print(f"[judge_calibration] 报告: {md_path}", file=sys.stderr)
    print(f"[judge_calibration] JSON: {json_path}", file=sys.stderr)
    print(f"[judge_calibration] 一致率 {rate:.2%} ({agree}/{total}) -> {'PASS' if passed else 'FAIL'}", file=sys.stderr)
    return 0 if passed else 1


def _stage_regate(args: argparse.Namespace) -> int:
    """离线重判门禁（零 LLM 成本）：合并 N 份全量报告 -> κ 均值 / MAD 全过 判定 + 稳定分歧仲裁清单。"""
    if not args.reports:
        print("[judge_calibration] --stage regate 需要 --reports A.json,B.json", file=sys.stderr)
        return 1
    paths = [Path(p.strip()) for p in args.reports.split(",") if p.strip()]
    if len(paths) < 2:
        print("[judge_calibration] --reports 至少给两份全量报告（单轮 κ 受 judge 方差影响，不作门禁）", file=sys.stderr)
        return 1
    rounds = []
    for p in paths:
        if not p.exists():
            print(f"[judge_calibration] 报告不存在: {p}", file=sys.stderr)
            return 1
        pts = json.loads(p.read_text(encoding="utf-8")).get("points", [])
        rounds.append((p, pts, _calibration_metrics(pts)))

    if not GOLDEN_PATH.exists():
        print(f"[judge_calibration] golden 不存在: {GOLDEN_PATH}", file=sys.stderr)
        return 1
    gold = {}
    for line in GOLDEN_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        gold[o["id"]] = o

    overridden = 0
    for (p, pts, _) in rounds:
        for p_point in pts:
            g = gold.get(p_point["id"])
            if not g:
                continue
            h = g.get("human_answer_relevancy") if p_point["metric"] == "answer_relevancy" else g.get("human_faithfulness")
            if h is None:
                continue
            p_point["human"] = float(h)
            p_point["delta"] = p_point["judge"] - h
            p_point["agree"] = abs(p_point["delta"]) <= TOLERANCE
            overridden += 1

    rounds = [(p, pts, _calibration_metrics(pts)) for (p, pts, _) in rounds]

    print(f"[judge_calibration] human 分以 {GOLDEN_PATH.name} 为准，覆盖报告快照 {overridden} 点", file=sys.stderr)

    mean_kappa = round(statistics.mean(r[2]["kappa"] for r in rounds), 4)
    max_mad = max(r[2]["mad"] for r in rounds)
    passed = (mean_kappa >= KAPPA_GATE) and (max_mad <= MAD_GATE)

    base_keys = {(p["id"], p["metric"]) for p in rounds[0][1]}
    for (_, pts, _) in rounds[1:]:
        base_keys &= {(p["id"], p["metric"]) for p in pts}

    def _d(pts, k):
        return next(p for p in pts if p["id"] == k[0] and p["metric"] == k[1])

    stable: list = []
    flapping: list = []
    for k in base_keys:
        diss = [not _d(pts, k)["agree"] for (_, pts, _) in rounds]
        h = _d(rounds[0][1], k)["human"]
        jr = [_d(pts, k)["judge"] for (_, pts, _) in rounds]
        dr = [_d(pts, k)["delta"] for (_, pts, _) in rounds]
        notes = _d(rounds[0][1], k).get("notes", "")
        entry = {"id": k[0], "metric": k[1], "human": h, "judge_rounds": jr, "deltas": dr, "notes": notes}
        if all(diss):
            stable.append(entry)
        elif any(diss):
            flapping.append(entry)

    stable.sort(key=lambda e: (e["metric"], e["id"]))
    flapping.sort(key=lambda e: (e["metric"], e["id"]))

    ts = time.strftime("%Y%m%d-%H%M%S")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORT_DIR / f"judge-regate-{ts}.json"
    json_path.write_text(
        json.dumps(
            {
                "gate_version": 2,
                "rounds": [{"report": p.name, "metrics": m} for (p, pts, m) in rounds],
                "mean_kappa": mean_kappa,
                "max_mad": max_mad,
                "kappa_gate": KAPPA_GATE,
                "mad_gate": MAD_GATE,
                "band_bounds": list(BAND_BOUNDS),
                "passed": passed,
                "stable_disagreements": stable,
                "flapping_points": flapping,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    md = [
        "# Judge Regate Report v2（κ 均值 + MAD 多轮门禁，零 LLM 成本）",
        f"- Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 输入报告: {', '.join(str(p) for p in paths)}",
        "",
        "| 轮次 | κ | MAD | 点级一致率(参考) | 判定点数 |",
        "|---|---|---|---|---|",
    ]
    for (p, pts, m) in rounds:
        md.append(f"| {p.name} | {m['kappa']:.4f} | {m['mad']:.4f} | {m['agreement_rate']:.2%} | {m['n']} |")
    md.append(f"| **均值/最大** | **{mean_kappa:.4f}** | **{max_mad:.4f}** | - | - |")
    md.append("")
    md.append("| 门禁 | 读数 | 阈值 | 状态 |")
    md.append("|---|---|---|---|")
    md.append(f"| Cohen's κ（均值） | {mean_kappa:.4f} | >= {KAPPA_GATE} | {'**PASS**' if mean_kappa >= KAPPA_GATE else '**FAIL**'} |")
    md.append(f"| MAD（每轮） | {max_mad:.4f} | <= {MAD_GATE} | {'**PASS**' if max_mad <= MAD_GATE else '**FAIL**'} |")
    md.append("")
    md.append(f"**门禁判定：{'PASS' if passed else 'FAIL'}**")
    md.append("")
    md.append(f"## 稳定分歧仲裁清单（每轮 |Δ|>{TOLERANCE}，{len(stable)} 点）")
    md.append("")
    md.append("> 人工三方对照（judge / 仲裁意见 / golden）：golden 错则修订 golden（版本化留痕）；judge 对则留档为已知边界。")
    md.append("")
    md.append("| id | metric | judge 各轮 | human | Δ 各轮 | notes |")
    md.append("|---|---|---|---|---|---|")
    for e in stable:
        jr = "/".join(f"{x:.2f}" for x in e["judge_rounds"])
        dr = "/".join(f"{x:.2f}" for x in e["deltas"])
        md.append(f"| {e['id']} | {e['metric']} | {jr} | {e['human']:.2f} | {dr} | {e['notes']} |")
    md.append("")
    md.append(f"## 单轮翻面点（judge 方差，不进仲裁，{len(flapping)} 点）")
    md.append("")
    for e in flapping:
        jr = "/".join(f"{x:.2f}" for x in e["judge_rounds"])
        md.append(f"- {e['id']} ({e['metric']}): judge={jr} human={e['human']:.2f}")
    md_path = REPORT_DIR / f"judge-regate-{ts}.md"
    md_path.write_text("\n".join(md), encoding="utf-8")

    print(f"[judge_calibration] regate 报告: {md_path}", file=sys.stderr)
    print(
        f"[judge_calibration] κ 均值={mean_kappa:.4f} (gate>={KAPPA_GATE})  MAD 最大={max_mad:.4f} (gate<={MAD_GATE})  稳定分歧 {len(stable)} 点 / 翻面 {len(flapping)} 点 -> {'PASS' if passed else 'FAIL'}",
        file=sys.stderr,
    )
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM judge 校准门禁 v2（Cohen's κ + MAD 双指标）")
    parser.add_argument("--stage", default="run", choices=["build", "run", "regate"], help="阶段（默认 run）")
    parser.add_argument("--report", default=None, help="build 阶段源 e2e JSON 报告（默认取最新）")
    parser.add_argument("--reports", default=None, help="regate 阶段：逗号分隔的多份全量报告路径")
    parser.add_argument("--samples", type=int, default=20, help="build 抽样条数（默认 20）")
    parser.add_argument("--ids", default=None, help="run 阶段：仅跑指定 id（逗号分隔，大写）")
    parser.add_argument("--diff-only", default=None, help="run 阶段：仅跑基线报告里不一致点（传基线报告路径）")
    parser.add_argument("--metrics", default="all", choices=["all", "rel", "faith"], help="run 阶段维度过滤")
    parser.add_argument("--gateway", default="http://localhost:9090/api/sentinel")
    parser.add_argument("--agent", default="http://localhost:8001", help="agent-python 基址（证据源）")
    parser.add_argument("--output", default=str(REPO_ROOT / "evaluation" / "reports"), help="报告目录")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    if args.stage == "build":
        return asyncio.run(_stage_build(args))
    if args.stage == "regate":
        return _stage_regate(args)
    return asyncio.run(_stage_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
