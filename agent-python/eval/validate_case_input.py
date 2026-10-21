"""case_input v2 校验器：评测集输入 JSON 规范的守门员。

校验三层：
  1. 行内 schema：必填字段、枚举值、格式
  2. 外键：alert_id 必须在告警夹具中存在；subject 必须与告警 subject 一致
  3. 联动：线索 refs 里的凭证号必须能在业务事实库（case-facts v2）查到——
     「线索提到的证据可查」是决策可信的前提，查不到的引用就是悬空枚举

用法：
  python eval/validate_case_input.py --input evaluation/seeds/case-seeds-ops.v2.jsonl \
      --alerts evaluation/seeds/case-alerts-case-seeds-ops.jsonl \
      --facts evaluation/seeds/case-facts.jsonl
"""

from __future__ import annotations

import argparse
import io
import json
import re
from pathlib import Path

EXPECT_ENUM = {"benign", "malicious", "inconclusive", "suspicious"}
# 业务主体类型（业务事实库口径）；observables/老集 subject 的类型域更宽（amount/payment/im）
SUBJECT_TYPES = {"account", "merchant", "ip", "device", "order", "ticket", "phone", "other"}
WIDE_TYPES = SUBJECT_TYPES | {"amount", "payment", "im"}
_OBS_TYPES = WIDE_TYPES
# 与 agent._CLUE_REF_RE / upgrade_case_input._REF_RE 同口径；\b 会因中文 \w 回溯截断单号
_REF_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Z]{2,5}-\d{4,}(?:-\d+)?(?![A-Za-z0-9-])"
)
# 工单/订单类凭证在业务库有对应记录，查不到即为悬空；协议/合同/热点等
# 非库内凭证（SUB-/CN-/WIFI-…）只提示不阻塞
_IN_DB_REF = re.compile(r"^(?:WO|TK|TRV|BP|HZ|CD|OA|RC|ORD)-", re.IGNORECASE)


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in io.open(path, encoding="utf-8") if l.strip()]


def validate(rows: list[dict], alerts: dict[str, dict], fact_refs: set[str]) -> list[str]:
    errs: list[str] = []
    names: set[str] = set()
    for i, r in enumerate(rows, 1):
        tag = f"row{i}({r.get('name') or '?'})"
        if r.get("schema") != "sentinel.case_input.v2":
            errs.append(f"{tag}: schema 缺失或不是 sentinel.case_input.v2")
        for k in ("id", "name", "clue", "expect_conclusion"):
            if not r.get(k):
                errs.append(f"{tag}: 缺必填字段 {k}")
        if r.get("expect_conclusion") not in EXPECT_ENUM:
            errs.append(f"{tag}: expect_conclusion 非法 {r.get('expect_conclusion')!r}")
        mc = r.get("min_confidence")
        if not isinstance(mc, (int, float)) or not 0 <= float(mc) <= 1:
            errs.append(f"{tag}: min_confidence 非法 {mc!r}")
        name = str(r.get("name") or "")
        if name in names:
            errs.append(f"{tag}: name 重复")
        names.add(name)

        aid = r.get("alert_id")
        if not aid:
            errs.append(f"{tag}: alert_id 未回填")
        elif aid not in alerts:
            errs.append(f"{tag}: alert_id {aid} 不在告警夹具中")
        subj = r.get("subject") or {}
        # 缺主体类案件（inco_missing_*）以 id_present=false 表达「线索没给主体」，
        # 属设计内数据；有 id 时类型必须合法
        if not subj.get("id"):
            if subj.get("id_present") is not False:
                errs.append(f"{tag}: subject.id 为空但未标 id_present=false {subj!r}")
        elif subj.get("type") not in WIDE_TYPES:
            errs.append(f"{tag}: subject 类型非法 {subj!r}")
        elif aid in alerts and alerts[aid].get("subject") and alerts[aid]["subject"].get("id") != subj.get("id"):
            errs.append(f"{tag}: subject.id 与告警夹具不一致 ({subj.get('id')} vs {alerts[aid]['subject'].get('id')})")
        for ob in r.get("observables") or []:
            if ob.get("type") not in _OBS_TYPES or not ob.get("value"):
                errs.append(f"{tag}: observable 非法 {ob!r}")
        for ref in r.get("refs") or []:
            if not _REF_RE.fullmatch(str(ref)):
                errs.append(f"{tag}: refs 格式非法 {ref!r}")
            elif fact_refs and str(ref) not in fact_refs and _IN_DB_REF.match(str(ref)):
                # 工单/订单类凭证必须在业务事实库中可查；其余类型（协议/热点等）仅提示
                errs.append(f"{tag}: 工单类凭证号 {ref} 在业务事实库中查不到（悬空引用）")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--alerts", default="")
    ap.add_argument("--facts", default="")
    args = ap.parse_args()

    rows = load_jsonl(Path(args.input))
    # 告警夹具按 alert_id 索引（v2 行回填的 alert_id 对应夹具的 alert_id 字段）
    alerts: dict[str, dict] = {}
    if args.alerts:
        alerts = {a["alert_id"]: a for a in load_jsonl(Path(args.alerts)) if a.get("alert_id")}
    fact_refs: set[str] = set()
    if args.facts:
        for f in load_jsonl(Path(args.facts)):
            rid = str(f.get("record_id") or "")
            if rid:
                fact_refs.add(rid)

    errs = validate(rows, alerts, fact_refs)
    print(f"{args.input}: {len(rows)} rows, {len(errs)} errors")
    for e in errs[:40]:
        print("  -", e)
    return 1 if errs else 0


if __name__ == "__main__":
    raise SystemExit(main())
