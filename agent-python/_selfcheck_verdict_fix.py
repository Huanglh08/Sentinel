"""R1/R10 修复的自检：零异常放行、凭证覆盖计数、数据比对升档。

跑法：python _selfcheck_verdict_fix.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.case import verdict as V  # noqa: E402
from core.case.agent import CaseInvestigationAgent  # noqa: E402
from core.case.types import EvidenceStep, InvestigationReport  # noqa: E402

CLUE_OK = "该商户今日交易额 4200 元、订单 196 笔，较日常增长 3 倍，无退款，设备指纹与近 90 天一致。"
CLUE_RISK = "该账号命中涉诈黑名单后尝试转账。"


def base_signals(**kw):
    s = [
        {"id": "a1", "kind": V.ANOMALY, "text": "交易额 4200 元，较日常增长 3 倍", "severity": "normal"},
        {"id": "a2", "kind": V.ANOMALY, "text": "订单数 196 笔，集中在放学时段", "severity": "normal"},
        {"id": "a3", "kind": V.ANOMALY, "text": "退款 0 笔", "severity": "normal"},
    ]
    s[0].update(kw)
    return s


R1_SIGNALS = [{"kind": V.TRUSTED, "text": "已确认商户正常经营", "covers": [], "tool_verified": False}]

# R1：零异常 + benign + 线索无硬风险词 → 放行（原来强制 inconclusive）
assert V.decide("benign", 0.9, R1_SIGNALS, 20, clue=CLUE_OK) is None, (
    "R1 不应把无风险线索的正常案降级"
)

# R1：线索含硬风险词 → 仍收紧
d = V.decide("benign", 0.9, R1_SIGNALS, 20, clue=CLUE_RISK)
assert d and d.conclusion == "inconclusive" and d.rule == "R1", "R1 对硬风险线索应保持收紧"

# R1：suspicious + 零异常 → 自相矛盾，保持降级
d = V.decide("suspicious", 0.8, R1_SIGNALS, 60, clue=CLUE_OK)
assert d and d.conclusion == "inconclusive" and d.rule == "R1"

# R10：三条异常全部被凭证覆盖 → 不触发
cred_full = {
    "id": "t1",
    "kind": V.TRUSTED,
    "text": "经营报备工单 WO-20241118-196，审批通过",
    "covers": ["a1", "a2", "a3"],
    "tool_verified": True,
}
assert V.decide("inconclusive", 0.45, base_signals() + [cred_full], 40, clue=CLUE_OK) is None, (
    "全覆盖时 R10 不应触发"
)

# R10：凭证只覆盖量值异常，剩 3 条动作型异常含高危 → 仍达下限（部分覆盖不得整体豁免）
sig = [
    {"id": "a1", "kind": V.ANOMALY, "text": "交易额 1.2 万元", "severity": "normal"},
    {"id": "a2", "kind": V.ANOMALY, "text": "改绑手机号", "severity": "high"},
    {"id": "a3", "kind": V.ANOMALY, "text": "向陌生账户转账", "severity": "high"},
    {"id": "a4", "kind": V.ANOMALY, "text": "新设备首次登录", "severity": "normal"},
]
cred_part = dict(cred_full, covers=["a1"])
d = V.decide("inconclusive", 0.45, sig + [cred_part], 40, clue=CLUE_OK)
assert d and d.rule == "R10" and d.conclusion == "malicious", "未覆盖的高危异常仍应触发 R10"

# R10：无凭证三条异常其中一条高危 → 触发
sig = base_signals()
sig[2].update({"text": "陌生收款账户转入 5 万元", "severity": "high"})
d = V.decide("inconclusive", 0.45, sig, 40, clue=CLUE_OK)
assert d and d.rule == "R10", "无凭证时应触发 R10"

# 数据比对升档：工具摘要 normal=true、样本 10 天，模型判 inconclusive、异常全是量值型 → benign
steps = [
    EvidenceStep(
        step_no=5,
        phase="INVESTIGATE",
        tool_id="sales_query",
        data_summary={"kind": "sales", "sample_days": 10, "normal": True},
    )
]
rep = InvestigationReport(
    conclusion="inconclusive",
    confidence=0.45,
    signals=base_signals(),
    coverage={"risk_signals": [], "benign_facts_verified": []},
)
out = CaseInvestigationAgent._apply_verdict_rule(rep, steps, CLUE_OK)
assert out.conclusion == "benign", f"数据比对应把量值型 inconclusive 升为 benign，实际 {out.conclusion}"

# 数据比对不能赦免动作型异常
rep2 = InvestigationReport(
    conclusion="inconclusive",
    confidence=0.45,
    signals=[{"id": "a1", "kind": V.ANOMALY, "text": "异地登录后 3 分钟改绑手机号", "severity": "high"}],
    coverage={"risk_signals": [], "benign_facts_verified": []},
)
out2 = CaseInvestigationAgent._apply_verdict_rule(rep2, steps, CLUE_OK)
assert out2.conclusion != "benign", "动作型异常不得被数据比对赦免"

# 数据比对也能收回模型保守给的 suspicious（异常仍限量值型、评分非 High）
rep3 = InvestigationReport(
    conclusion="suspicious",
    confidence=0.82,
    risk_score=90,
    signals=base_signals(),
    coverage={"risk_signals": [], "benign_facts_verified": []},
)
out3 = CaseInvestigationAgent._apply_verdict_rule(rep3, steps, CLUE_OK)
assert out3.conclusion == "benign", f"量值型 suspicious 应被数据比对收回，实际 {out3.conclusion}"

# 事件级覆盖：凭证覆盖一条量值异常，其余量值异常属同一事件 → 不触发 R3 降级
event_sig = [
    {"id": "a1", "kind": V.ANOMALY, "text": "群发 482 人，规模超出日常", "severity": "normal"},
    {"id": "a2", "kind": V.ANOMALY, "text": "每分钟 12 条，发送频率集中", "severity": "normal"},
    {"id": "a3", "kind": V.ANOMALY, "text": "交易额 1.2 万元", "severity": "normal"},
    {"id": "t1", "kind": V.TRUSTED, "text": "群发报备工单 WO-20240626-046，审批通过", "covers": ["a1"]},
]
d = V.decide("suspicious", 0.78, event_sig, 60, clue=CLUE_OK)
assert d and d.rule == "R3" and d.conclusion == "inconclusive", (
    "同事件量值参数应被报备一并覆盖，按审批降级只降一级"
)

# 同一份报备不覆盖独立的动作型异常 → 不触发降级（保持原档，不得整案降）
event_sig_2 = event_sig + [
    {"id": "a4", "kind": V.ANOMALY, "text": "改绑收款账户至陌生主体", "severity": "high"}
]
assert V.decide("suspicious", 0.78, event_sig_2, 60, clue=CLUE_OK) is None, (
    "存在未覆盖的动作型异常时不得整案降档"
)

# 时间/地域是事件参数：夜间经营报备、订阅协议可以覆盖，但境外支付不能被交易额基线证伪
assert V.is_event_parameter("深夜支付") and V.is_event_parameter("支付至境外商户")
assert not V.is_quantity_anomaly("支付至境外商户"), "境外支付不能被交易额基线证伪"
assert not V.is_event_parameter("改绑收款账户至陌生主体"), "动作型异常不属于事件参数"

# 新店：开业前无交易（pre_opening）视为「无可比基线」，量值型证据不足可升为良性
steps_pre = [
    EvidenceStep(
        step_no=1,
        phase="INVESTIGATE",
        tool_id="sales_query",
        data_summary={"kind": "sales", "sample_days": 0, "pre_opening": True, "normal": True},
    )
]
rep4 = InvestigationReport(
    conclusion="inconclusive",
    confidence=0.45,
    signals=[{"id": "a1", "kind": V.ANOMALY, "text": "交易额从 0 跃升至 5.8 万元", "severity": "normal"}],
    coverage={"risk_signals": [], "benign_facts_verified": []},
)
out4 = CaseInvestigationAgent._apply_verdict_rule(rep4, steps_pre, CLUE_OK)
assert out4.conclusion == "benign", f"新店开业首日应判良性，实际 {out4.conclusion}"

# 主体不可核查（只有 IP 与时间）→ 不能被零异常放行成良性
d = V.decide("benign", 0.9, R1_SIGNALS, 20, clue="IP 203.0.113.9 在 03:00 出现一批登录，未提供账号、设备，无法确定具体主体")
assert d and d.conclusion == "inconclusive" and d.rule == "R1", "主体不明不得判良性"

# R10 高危只认动作型：量值/时段即使标 high 也不进下限计数
sig_all_q = [
    {"id": "a1", "kind": V.ANOMALY, "text": "交易额 1.2 万元", "severity": "high"},
    {"id": "a2", "kind": V.ANOMALY, "text": "订单数 88 笔", "severity": "high"},
    {"id": "a3", "kind": V.ANOMALY, "text": "深夜 20:00 下单", "severity": "high"},
]
assert V.decide("inconclusive", 0.45, sig_all_q, 40, clue=CLUE_OK) is None, (
    "事件参数型高危不应触发恶意下限"
)

# 词表外置：协议/签约类凭据必须能判成凭证
assert V.trusted_basis("续费依据自动扣款协议（签约号 SUB-2024-1331）") == "credential"

# 结构化凭证引用：文本丢了单号，只要挂了 evidence_ref 仍算凭证，可触发 R3 降级
sig_ref = [
    {"id": "a1", "kind": V.ANOMALY, "text": "深夜支付境外云存储", "severity": "normal"},
    {"id": "t1", "kind": V.TRUSTED, "text": "已订阅两年，每月同类扣款", "covers": ["a1"], "evidence_ref": "SUB-2024-1331"},
]
d = V.decide("malicious", 0.9, sig_ref, 85, clue=CLUE_OK)
assert d and d.rule == "R3" and d.conclusion == "suspicious", "结构化凭证引用应参与降级"

print("PASS 18/18")
