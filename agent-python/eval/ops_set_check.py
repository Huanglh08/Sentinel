"""运营集种子自检：比例、主体覆盖、凭证、实体、重复度、工具数据可用率、商户交易基线窗口。

跑法（仓库根目录）:
    python agent-python/eval/ops_set_check.py
    python agent-python/eval/ops_set_check.py --seeds evaluation/seeds/case-seeds-ops.jsonl \
        --alerts evaluation/seeds/case-alerts.jsonl --facts evaluation/seeds/case-facts.jsonl

任一硬性检查失败时退出码为 1。
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_SEEDS = REPO / "evaluation" / "seeds" / "case-seeds-ops.jsonl"
DEFAULT_SPEC = REPO / "evaluation" / "seeds" / "case-seeds-ops.spec.json"
DEFAULT_ALERTS = REPO / "evaluation" / "seeds" / "case-alerts.jsonl"
DEFAULT_FACTS = REPO / "evaluation" / "seeds" / "case-facts.jsonl"
DEFAULT_COVERAGE = REPO / "evaluation" / "seeds" / "case-data-coverage.json"

IP_RE = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")
DEVICE_RE = re.compile(r"设备指纹为([A-Za-z0-9_.\-]{2,32})|(iPhone\s?\d+)|(Android\s?\d+)|(鸿蒙[^\s，；。]*)")
DATE_RE = re.compile(r"20\d{2}-\d{2}-\d{2}")

SUBJECT_PATTERNS = {
    "account": re.compile(r"账号\s*([A-Za-z]{0,6}\d[A-Za-z0-9_]*|[0-9*]{6,})"),
    "device": re.compile(
        r"设备指纹为([A-Za-z0-9_.\-]{2,32})|设备\s*(?:ID|id)\s*[:：]?\s*([A-Za-z0-9]{6,})"
    ),
    "ip": IP_RE,
    "order": re.compile(r"订单号\s*[:：]?\s*([A-Za-z0-9_]+)"),
    "merchant": re.compile(r"商户(?:账号|ID)\s*[:：]?\s*([A-Za-z0-9_]+)"),
    "payment": re.compile(r"(?:支付)?ID\s*[:：]\s*([A-Za-z0-9_]+)|账号\s*([A-Za-z]{0,6}\d[A-Za-z0-9_]*)"),
    "im": re.compile(r"IM账号\s*([0-9*]{6,})|(im_\d+)"),
}

CREDENTIAL_WORDS = ("核验", "名单未命中", "报备", "工单", "闭环", "回访", "实名", "历史", "一致", "关联")
GAP_WORDS = ("未提供", "缺少", "无法", "不可查", "无检索", "不能证明")
TIER_MAP = {"完整": "full", "部分缺失": "partial", "严重缺失": "sparse"}


def _subject_id(clue: str, stype: str) -> str:
    pat = SUBJECT_PATTERNS.get(stype)
    m = pat.search(clue) if pat else None
    if m:
        val = next((g for g in m.groups() if g), "") if m.groups() else m.group(0)
        if val:
            return val
    # 与 build_alerts.pick_subject 一致：主体写法不标准时退回通用实体（账号/订单号等）
    g = re.search(r"(?<![A-Za-z0-9_\-])([A-Za-z]{1,6}[_\-]?\d{4,})", clue)
    return g.group(1) if g else ""


def _check(seeds: list[dict], spec: dict, alerts: dict, facts: list[dict], coverage: dict) -> tuple[list[str], list[str]]:
    fails: list[str] = []
    notes: list[str] = []
    total = len(seeds)
    n = total or 1

    labels = Counter(s["expect_conclusion"] for s in seeds)
    expected = spec.get("labels") or {}
    for label, want in expected.items():
        got = labels.get(label, 0)
        want_now = round(want * n / spec.get("total", n))
        if abs(got - want_now) / n * 100 > spec.get("checks", {}).get("label_tolerance_pct", 2):
            fails.append(f"比例偏差: {label} 实际 {got}/{n}，规格 {want_now}/{n}（容差 2pp）")
    notes.append(f"结论分布: {dict(labels)}")

    subjects = Counter(s["coverage"].get("subject_type", "?") for s in seeds)
    missing = set(spec.get("subjects", {})) - set(subjects)
    if missing:
        fails.append(f"主体覆盖缺失: {sorted(missing)}")
    notes.append(f"主体分布: {dict(subjects)}")

    reviewed = Counter(s.get("review", "") for s in seeds)
    if reviewed.get("disputed", 0):
        fails.append(f"存在待议样本 {reviewed['disputed']} 条，应移出运营集")
    notes.append(f"复核状态: {dict(reviewed)}")

    for s in seeds:
        stype = s["coverage"].get("subject_type", "")
        clue = s["clue"]
        sid = _subject_id(clue, stype)
        has_time = bool(DATE_RE.search(clue))
        has_ip_dev = bool(IP_RE.search(clue) or DEVICE_RE.search(clue))
        if s["expect_conclusion"] in ("benign", "malicious"):
            if not sid:
                fails.append(f"{s['id']} 缺主体实体（{stype}）")
            if not has_time:
                fails.append(f"{s['id']} 缺时间")
            if not has_ip_dev:
                fails.append(f"{s['id']} 缺 IP/设备")
            if not any(w in clue for w in CREDENTIAL_WORDS):
                fails.append(f"{s['id']} 良性/恶意缺可核查凭证表述")
            for ref in s.get("rule_refs") or []:
                if not re.match(r"C\d{2}", ref):
                    fails.append(f"{s['id']} 条款引用格式异常: {ref}")
            if not (s.get("label_rationale") or "").strip():
                fails.append(f"{s['id']} 缺判定依据")
        else:
            if not any(w in clue for w in GAP_WORDS):
                fails.append(f"{s['id']} 证据不足但未写明缺口")
        if s["expect_conclusion"] == "malicious":
            if s["coverage"].get("evidence_strength") != "双信号印证":
                fails.append(f"{s['id']} 恶意样本不是双信号")
            if len(s.get("key_evidence") or []) < 2:
                fails.append(f"{s['id']} 恶意样本关键证据少于两条")

    benign = [s for s in seeds if s["expect_conclusion"] == "benign"]
    routine = sum(1 for s in benign if s.get("difficulty") == "routine")
    ratio = routine / len(benign) if benign else 0.0
    if ratio < spec.get("checks", {}).get("routine_ratio_of_benign", 0.6):
        fails.append(f"良性日常件占比 {ratio:.2%} 低于 60%")
    notes.append(f"良性日常件: {routine}/{len(benign)} = {ratio:.2%}")

    names = [s["name"] for s in seeds]
    ids = [s["id"] for s in seeds]
    if len(set(names)) != len(names):
        fails.append(f"重复 name: {[k for k, v in Counter(names).items() if v > 1][:3]}")
    if len(set(ids)) != len(ids):
        fails.append(f"重复 id: {[k for k, v in Counter(ids).items() if v > 1][:3]}")
    near = []
    for i in range(len(seeds)):
        for j in range(i + 1, len(seeds)):
            r = difflib.SequenceMatcher(None, seeds[i]["clue"], seeds[j]["clue"]).ratio()
            if r > 0.9:
                near.append((seeds[i]["id"], seeds[j]["id"], round(r, 2)))
    if near:
        fails.append(f"疑似重复线索 {len(near)} 对: {near[:3]}")
    notes.append(f"重复检查: 0 对（阈值 0.90）")

    missing_alert = [s["id"] for s in seeds if not alerts.get(s["name"])]
    if missing_alert:
        fails.append(f"{len(missing_alert)} 条无告警编号: {missing_alert[:5]}")

    full_tier = 0
    usable = 0
    eligible = 0
    unusable = []
    for s in seeds:
        cov = coverage.get(s["name"]) or {}
        tier = cov.get("tier") or TIER_MAP.get(s["coverage"].get("info_completeness", ""), "sparse")
        if tier == "full":
            full_tier += 1
        # 证据不足样本本身就以「查不到主体」为考点，单列不计入可用率
        if s["expect_conclusion"] == "inconclusive":
            continue
        eligible += 1
        sid = _subject_id(s["clue"], s["coverage"].get("subject_type", ""))
        rows = [f for f in facts if f.get("seed") == s["name"] and f.get("alert_id")]
        hit = any(sid and sid in (f.get("entities") or []) for f in rows)
        if hit:
            usable += 1
        else:
            unusable.append(s["id"])
    full_ratio = full_tier / n
    usable_ratio = usable / (eligible or 1)
    if full_ratio < spec.get("checks", {}).get("full_tier_ratio", 0.8):
        fails.append(f"完整档位占比 {full_ratio:.2%} 低于 80%")
    if unusable:
        fails.append(f"工具数据不可用 {len(unusable)} 条（主体不在 entities）: {unusable[:5]}")
    notes.append(f"完整档位: {full_tier}/{n} = {full_ratio:.2%}")
    notes.append(f"工具数据可用: {usable}/{eligible} = {usable_ratio:.2%}（证据不足样本单列）")

    # 商户历史基线：完整档位要能用 7 至 14 天曲线验证「正常波动」，否则只能靠单日数字猜
    covered: dict[str, int] = {}
    skipped: list[str] = []
    new_stores: list[str] = []
    for s in seeds:
        if s["coverage"].get("subject_type") != "merchant":
            continue
        cov = coverage.get(s["name"]) or {}
        tier = cov.get("tier") or TIER_MAP.get(s["coverage"].get("info_completeness", ""), "sparse")
        if tier != "full":
            continue
        rows = [f for f in facts if f.get("seed") == s["name"] and f.get("kind") == "sales"]
        # 只认「商户交易汇总」这条当日流水；单笔订单不能当每日交易额去反推
        today = [
            f for f in rows
            if not (f.get("payload") or {}).get("baseline") and "交易额" in (f.get("payload") or {})
        ]
        hist = [f for f in rows if (f.get("payload") or {}).get("baseline")]
        if not today:
            # 线索没写当日交易额时无法反推，宁可不补也不造一条与线索矛盾的流水
            skipped.append(s["id"])
            continue
        # 新开/新入驻的店没有历史经营，生成器刻意不造基线（造了会与「从 0 跃升」矛盾）
        if re.search(r"新开通|新入驻|新开分店|新开店|开业首日", s["clue"]):
            new_stores.append(s["id"])
            continue
        dates = sorted({f.get("date") for f in hist if f.get("date")})
        covered[s["id"]] = len(dates)
        if not 7 <= len(dates) <= 14:
            fails.append(f"{s['id']} 商户交易基线 {len(dates)} 天，应为 7 至 14 天")
        today_date = today[0].get("date") or ""
        if dates and today_date and max(dates) >= today_date:
            fails.append(f"{s['id']} 商户基线日期未早于案件日期")
        if int((today[0].get("payload") or {}).get("订单数") or 0) > 0:
            zero = [f.get("record_id") for f in hist if int((f.get("payload") or {}).get("订单数") or 0) <= 0]
            if zero:
                fails.append(f"{s['id']} 商户基线存在 0 订单日: {zero[:3]}")
        if any(float(f.get("amount") or 0) < 0 for f in hist):
            fails.append(f"{s['id']} 商户基线出现负交易额")
    if covered:
        vals = list(covered.values())
        notes.append(f"商户交易基线: 达标 {len(vals)} 条，窗口 {min(vals)} 至 {max(vals)} 天")
    if skipped:
        notes.append(f"商户无当日交易汇总、未生成基线 {len(skipped)} 条（不补无依据字段）: {skipped}")
    if new_stores:
        notes.append(f"新开店铺无历史经营、未生成基线 {len(new_stores)} 条（不造矛盾曲线）: {new_stores}")

    return fails, notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default=str(DEFAULT_SEEDS))
    ap.add_argument("--spec", default=str(DEFAULT_SPEC))
    ap.add_argument("--alerts", default=str(DEFAULT_ALERTS))
    ap.add_argument("--facts", default=str(DEFAULT_FACTS))
    ap.add_argument("--coverage", default=str(DEFAULT_COVERAGE))
    args = ap.parse_args()

    seeds = [json.loads(line) for line in Path(args.seeds).read_text(encoding="utf-8").splitlines() if line.strip()]
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    alerts = {}
    if Path(args.alerts).exists():
        for line in Path(args.alerts).read_text(encoding="utf-8").splitlines():
            if line.strip():
                a = json.loads(line)
                alerts[a["name"]] = a["alert_id"]
    facts = []
    if Path(args.facts).exists():
        facts = [json.loads(line) for line in Path(args.facts).read_text(encoding="utf-8").splitlines() if line.strip()]
    coverage = json.loads(Path(args.coverage).read_text(encoding="utf-8")) if Path(args.coverage).exists() else {}

    fails, notes = _check(seeds, spec, alerts, facts, coverage)
    print(f"运营集自检: {args.seeds}  共 {len(seeds)} 条")
    for n in notes:
        print("  -", n)
    if fails:
        print("\nFAIL:")
        for f in fails:
            print("  x", f)
        return 1
    print("\nPASS: 比例/主体/凭证/实体/重复/工具数据/商户基线七项全通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
