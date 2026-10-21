"""五段流水线自检（不调 LLM、不连 DB）：断言阶段顺序与执行模式开关。

运行: python _selfcheck_stages.py

覆盖:
  1) 阶段顺序 DETECT -> ENRICH -> CORRELATE -> RECOMMEND -> ACT
  2) 关联产物（signals）确实传给建议阶段
  3) CASE_EXECUTION_MODE 默认 approval：全部建议落审批，不自动执行
  4) CASE_EXECUTION_MODE=auto：白名单子集走 L1 自动放行
"""

import asyncio
import os
import sys

import core.case.agent as agent_mod
from core.case.agent import (
    STAGE_ACT,
    STAGE_CORRELATE,
    STAGE_DETECT,
    STAGE_ENRICH,
    STAGE_RECOMMEND,
    CaseInvestigationAgent,
)
from core.case.types import EvidenceStep, InvestigationReport, SEVERITY_BENIGN, TriageResult

EXPECTED = [STAGE_DETECT, STAGE_ENRICH, STAGE_CORRELATE, STAGE_RECOMMEND, STAGE_ACT]

# 不连 DB：风险策略用固定桩替换
agent_mod.get_risk_policy = lambda: {
    "l2_action_denylist": ["冻结", "封禁"],
    "l2_action_whitelist": ["通知", "观察"],
    "l2_autopilot_enabled": 1,
    "l2_confidence": 0.9,
}


class FakeStore:
    def __init__(self):
        self.actions = []

    def create_case(self, **kw):
        return {"id": "c1", "case_no": "CASE-1", "title": kw.get("title", "")}

    def append_evidence(self, case_id, step):
        pass

    def finish_case(self, case_id, **kw):
        pass

    def create_approvals(self, case_id, case_no, pending):
        return [{"id": "ap1", "action": p.get("action", "")} for p in pending]

    def create_actions(self, case_id, case_no, _extra, recs, operator=""):
        self.actions.extend(recs)
        return [{"id": f"act{i}", "operator": operator} for i, _ in enumerate(recs)]

    def record_audit(self, **kw):
        pass


async def run(mode: str):
    os.environ["CASE_EXECUTION_MODE"] = mode
    agent = object.__new__(CaseInvestigationAgent)
    agent.store = FakeStore()
    agent.responder = None
    captured = {}

    async def _triage(clue):
        return TriageResult(title="t", input_quality={"sufficient": True}), []

    async def _investigate(clue, triage, alert_id=""):
        step = EvidenceStep(step_no=1, phase="INVESTIGATE", tool_id="t.query", observation="ok")
        return [step], ""

    async def _extract_signals(clue, steps):
        return [{"kind": "anomaly", "id": "a1", "text": "异地登录", "severity": "normal", "source": "clue"}]

    async def _report(clue, triage, steps, forced_stop, signals=None):
        captured["signals"] = signals
        return InvestigationReport(
            conclusion=SEVERITY_BENIGN,
            confidence=0.9,
            recommendations=[{"action": "通知", "requires_human_approval": True, "reason": "r"}],
        )

    agent._triage = _triage
    agent._investigate = _investigate
    agent._extract_signals = _extract_signals
    agent._report = _report

    if mode == "auto":
        agent._split_auto = lambda report, recs, policy: (recs, [])
        agent._l1_autopass = lambda case_id, case, report, recs: agent.store.create_actions(
            case_id, case.get("case_no", ""), "", recs, operator="L1:auto"
        )

    events = [e async for e in agent.investigate("测试线索")]
    return events, captured


def check(cond: bool, msg: str):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


async def main():
    events, captured = await run("approval")
    starts = [e["stage"] for e in events if e["type"] == "stage" and e["status"] == "start"]
    check(starts == EXPECTED, f"阶段顺序 {starts}")

    check(bool(captured.get("signals")), "关联产物已传入建议阶段")

    types = {e["type"] for e in events}
    act = next(e for e in events if e["type"] == "stage" and e["stage"] == STAGE_ACT and e["status"] == "done")
    check("approval.created" in types, "默认 approval：产生审批待办")
    check("l1.autopass" not in types and "l2.autopilot" not in types, "默认 approval：未自动执行")
    check(act["mode"] == "approval" and act["auto"] == 0 and act["pending"] == 1, f"ACT 汇总 {act}")

    events2, _ = await run("auto")
    types2 = {e["type"] for e in events2}
    act2 = next(e for e in events2 if e["type"] == "stage" and e["stage"] == STAGE_ACT and e["status"] == "done")
    check("l1.autopass" in types2, "auto 模式：L1 自动放行触发")
    check("approval.created" not in types2, "auto 模式：不再落审批")
    check(act2["mode"] == "auto" and act2["auto"] == 1 and act2["pending"] == 0, f"ACT 汇总 {act2}")

    print("全部通过")


if __name__ == "__main__":
    asyncio.run(main())
