"""自检：案件调查图引擎（内存模式，不依赖 Postgres；需已安装 langgraph 与项目依赖）。

验证三件事：
  1. 图引擎正常路径：分诊 -> 逐步调查（每步即时落库）-> 关联 -> 报告+行动，事件序列与旧引擎对齐；
  2. 单步幂等落库：证据行数 == 图状态步数（无重复行）；
  3. 检查点未启用时续跑给出明确错误事件（降级语义正确）。

运行: py -3 tools_selfcheck_case_graph.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

if os.name == "nt":
    # 与 server.py 同因：异步 psycopg 只支持 SelectorEventLoop（Windows 默认 Proactor）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    import langgraph  # noqa: F401
except Exception:  # noqa: BLE001
    print("SKIP: langgraph 未安装（pip install langgraph langgraph-checkpoint-postgres）")
    sys.exit(2)

from core.case.agent import CaseInvestigationAgent  # noqa: E402
from core.case.graph_engine import CaseGraphEngine  # noqa: E402
from core.case.store import CaseStore  # noqa: E402

# ---- 桩件：LLM 按调用次序返回 canned JSON；MCP 返回结构化信封 ----
_TRIAGE = {
    "title": "某商户交易异常",
    "priority": "P1",
    "subject_type": "merchant",
    "subject_id": "M-1001",
    "key_questions": ["交易是否异常", "设备是否被改"],
    "input_quality": {"sufficient": True},
    "skill": "",
}
_TOOL_ACTION = {
    "thought": "先查关联工单",
    "action": "tool",
    "tool_id": "ticket_search",
    "args": {"keyword": "M-1001"},
}
_FINISH_ACTION = {
    "thought": "证据足够，收尾",
    "action": "finish",
    "conclusion": "suspicious",
}
_REPORT = {
    "conclusion": "suspicious",
    "confidence": 0.9,
    "summary": "工单与交易数据交叉验证存在异常",
    "reasons": ["交易趋势突增", "关联工单密集"],
    "signals": [{"name": "交易激增", "severity": "high"}],
    "recommendations": [
        {"action": "发送核实提醒", "reason": "低风险通知", "risk_tier": "benign"},
    ],
}


class _StubLLM:
    """按 (max_tokens, 次数) 分发 canned JSON：500 分诊 / 600 ReAct / 其余报告。"""

    def __init__(self) -> None:
        self._react_calls = 0

    async def chat(self, messages, *, max_tokens=600, temperature=0.0, response_format=None):
        del messages, temperature, response_format
        if max_tokens <= 500:
            return json.dumps(_TRIAGE, ensure_ascii=False)
        if max_tokens <= 700:
            self._react_calls += 1
            if self._react_calls == 1:
                return json.dumps(_TOOL_ACTION, ensure_ascii=False)
            return json.dumps(_FINISH_ACTION, ensure_ascii=False)
        return json.dumps(_REPORT, ensure_ascii=False)


class _StubMCP:
    async def call(self, tool_id: str, args: dict) -> tuple[bool, str]:
        del args
        return False, f"[status=hit][count=3][summary={{}}] {tool_id} 桩数据"


async def main() -> int:
    store = CaseStore()  # 无环境变量 -> 内存模式（enabled=False）
    agent = CaseInvestigationAgent(_StubLLM(), _StubMCP(), store=store)
    engine = CaseGraphEngine(agent)

    events: list[dict] = []
    async for ev in engine.investigate("商户 M-1001 交易异常，请调查", user_id="tester"):
        events.append(ev)

    types = [e.get("type") for e in events]
    problems: list[str] = []

    # 1) 事件序列关键节点齐全且有序
    i_created = types.index("case.created") if "case.created" in types else -1
    i_step = types.index("step") if "step" in types else -1
    i_report = types.index("report") if "report" in types else -1
    i_end = types.index("end") if "end" in types else -1
    i_phase_first = types.index("phase") if "phase" in types else -1
    if not (0 <= i_phase_first < i_created < i_step < i_report < i_end):
        problems.append(f"事件顺序异常: phase@{i_phase_first} created@{i_created} step@{i_step} report@{i_report} end@{i_end}")
    if types.count("step") < 2:
        problems.append(f"step 事件数量不足: {types.count('step')}")
    end = next((e for e in reversed(events) if e.get("type") == "end"), None)
    created = next((e for e in events if e.get("type") == "case.created"), None)
    if not end or not created:
        problems.append("缺少 case.created 或 end 事件")
    else:
        if end.get("case_id") != created.get("case_id"):
            problems.append("end.case_id 与 case.created 不一致")
        if end.get("status") != "REPORTED":
            problems.append(f"终态非 REPORTED: {end.get('status')}")

    case_id = created.get("case_id") if created else ""
    # 2) 每步即时落库且无重复行（内存模式直接数）
    rows = store._mem_evidence.get(case_id, [])
    step_nos = [r.get("step_no") for r in rows if r.get("phase") == "INVESTIGATE"]
    if len(step_nos) != len(set(step_nos)):
        problems.append(f"证据行重复: {step_nos}")
    if end and len(rows) < int(end.get("steps") or 0):
        problems.append("落库证据行少于终态步数")
    # 3) 案件终态与报告落库
    case = store.get_case(case_id)
    if not case or case.get("status") != "REPORTED" or not case.get("report"):
        problems.append(f"案件终态异常: {case and case.get('status')}")
    # 4) 检查点未启用：续跑必须给出明确错误事件（而非静默成功）
    resume_events = [e async for e in engine.resume(case_id, user_id="tester")]
    if not (resume_events and resume_events[0].get("type") == "error"):
        problems.append(f"无检查点续跑应返回 error 事件: {resume_events[:1]}")

    if problems:
        print("FAIL")
        for p in problems:
            print(" -", p)
        return 1
    print(
        f"OK: 图引擎正常路径通过 events={len(events)} steps={end.get('steps')} "
        f"evidence_rows={len(rows)} act_mode={next(e.get('mode') for e in events if e.get('type') == 'stage' and e.get('stage') == 'ACT')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
