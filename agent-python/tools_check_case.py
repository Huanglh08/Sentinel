"""案件调查 Agent 离线冒烟测试（无网络/无 PG 依赖）。

验证点：
1. _extract_json 对围栏/杂文字的稳健性
2. 三段式全链：线索 -> Triage -> Investigate(多步工具循环) -> Report
3. 护栏：白名单拒绝、重复调用熔断、非法 JSON 纠偏、观察截断
4. CaseStore 内存降级模式（DB 不可用时链路不中断）

运行：cd agent-python && python tools_check_case.py
"""

from __future__ import annotations

import asyncio
import json
import sys

sys.path.insert(0, ".")

from core.case.agent import CaseInvestigationAgent, _extract_json, _truncate  # noqa: E402
from core.case import verdict as verdict_rules  # noqa: E402
from core.case.store import CaseStore  # noqa: E402
from core.case.types import SEVERITY_MALICIOUS  # noqa: E402


def _mem_store() -> CaseStore:
    """强制内存模式：冒烟测试不依赖 PG（本机有无 psycopg/PG 均确定性通过）。"""
    store = CaseStore()
    store.enabled = False
    return store


class FakeLLM:
    """按调用序次返回预设响应的假 LLM。"""

    def __init__(self, scripts: list[str]) -> None:
        self.scripts = list(scripts)
        self.calls: list[dict] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self.scripts.pop(0) if self.scripts else '{"thought": "收尾", "action": "finish", "conclusion": "证据充分"}'


class FakeMCP:
    """记录调用并返回固定观察结果的假 MCP 客户端。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, tool_id, arguments, timeout=None):
        self.calls.append((tool_id, arguments))
        return False, json.dumps({"events": [{"id": i, "region": arguments.get("region", "-")} for i in range(3)]}, ensure_ascii=False)


# S6 独立信号抽取通道在 reporter 之前多调一次 LLM，每个到达报告阶段的用例都要补这一格
_SIGNALS = json.dumps({"signals": []})


def _extract_json_cases() -> None:
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('前置说明 {"thought": "x"} 后缀') == {"thought": "x"}
    assert _extract_json("not json at all") is None
    assert _truncate("a" * 100, 10) != "a" * 100 and len(_truncate("a" * 100, 10)) < 100
    print("[PASS] _extract_json / _truncate")


async def _happy_path() -> None:
    llm = FakeLLM(
        [
            # triage
            json.dumps(
                {
                    "title": "批量撞库线索",
                    "subject_type": "account",
                    "subject_id": "u1001",
                    "priority": "P1",
                    "severity_guess": "suspicious",
                    "key_questions": ["是否存在异地登录", "是否伴随资产变动"],
                    "skill": "credential-stuffing",
                    "plan": ["查工单", "查业务基线"],
                },
                ensure_ascii=False,
            ),
            # step1: 合法工具调用（SOP 第 1 步）
            json.dumps({"thought": "按 SOP 查工单", "action": "tool", "tool_id": "ticket_query", "args": {"customer_name": "u1001"}}, ensure_ascii=False),
            # step2: 非法 JSON -> 应被纠偏且计入步数
            "我想想……这个不是 JSON",
            # step3: 白名单外工具 -> 应被拒绝
            json.dumps({"thought": "试试别的", "action": "tool", "tool_id": "danger_action", "args": {}}),
            # finish
            '{"thought": "证据已足够", "action": "finish", "conclusion": "确认撞库"}',
            # signals -> report
            _SIGNALS,
            json.dumps(
                {
                    "conclusion": "malicious",
                    "confidence": 0.86,
                    "summary": "多源证据指向撞库后盗用。",
                    "key_findings": [{"finding": "异地登录后资产变动", "evidence_step": 1}],
                    "recommendations": [{"action": "冻结账户", "requires_human_approval": True, "reason": "高危"}],
                    "knowledge_distillation": "撞库活动伴随资产即时转移。",
                },
                ensure_ascii=False,
            ),
        ]
    )
    mcp = FakeMCP()
    agent = CaseInvestigationAgent(llm, mcp, store=_mem_store())
    events = [e async for e in agent.investigate("账号 u1001 今日凌晨 3 点出现 12 次异地登录失败后成功，随后积分异动", user_id="tester")]

    types = [e["type"] for e in events]
    assert "case.created" in types, types
    assert types.count("phase") == 6, types  # 三阶段各 start/done
    assert "report" in types and "end" in types, types
    steps = [e for e in events if e["type"] == "step"]
    assert len(steps) == 4, f"expect 4 steps (tool/badjson/notallowed/finish), got {len(steps)}"
    assert steps[0]["tool_id"] == "ticket_query" and not steps[0]["is_error"]
    assert steps[1]["is_error"], "非法 JSON 步应标记 error"
    assert steps[2]["is_error"] and "白名单" in steps[2]["observation"], "白名单外工具应被拒绝"
    assert steps[3]["tool_id"] == "llm.finish"

    report = next(e for e in events if e["type"] == "report")["report"]
    assert report["conclusion"] == SEVERITY_MALICIOUS
    assert report["confidence"] > 0.5
    assert report["recommendations"][0]["requires_human_approval"] is True

    end = next(e for e in events if e["type"] == "end")
    case = agent.store.get_case(end["case_id"])
    assert case is not None and case["status"] == "REPORTED"
    assert len(case["evidence"]) == 4
    assert case["severity"] == SEVERITY_MALICIOUS and case["ttr_ms"] >= 0
    print(f"[PASS] 全链: {len(steps)} 步, TTA={end['tta_ms']}ms, TTR={end['ttr_ms']}ms, conclusion={report['conclusion']}")


async def _duplicate_breaker() -> None:
    """同参数重复调用 3 次 -> 强制熔断收尾。"""
    same = json.dumps({"thought": "再查一次", "action": "tool", "tool_id": "ticket_query", "args": {"region": "华东"}}, ensure_ascii=False)
    llm = FakeLLM(
        [
            json.dumps({"title": "t", "subject_type": "ip", "subject_id": "1.2.3.4", "priority": "P2", "severity_guess": "suspicious", "key_questions": [], "skill": "", "plan": []}, ensure_ascii=False),
            same, same, same,  # 第 3 次触发熔断
            _SIGNALS,
            json.dumps({"conclusion": "inconclusive", "confidence": 0.1, "summary": "被熔断", "key_findings": [], "recommendations": [], "knowledge_distillation": ""}, ensure_ascii=False),
        ]
    )
    agent = CaseInvestigationAgent(llm, FakeMCP(), store=_mem_store(), max_steps=10)
    events = [e async for e in agent.investigate("某 IP 段批量注册", user_id="tester")]
    end = next(e for e in events if e["type"] == "end")
    assert end["forced_stop_reason"].startswith("重复调用熔断"), end
    steps = [e for e in events if e["type"] == "step"]
    assert len([s for s in steps if s["tool_id"] == "ticket_query"]) == 3
    print("[PASS] 重复调用熔断")


async def _step_budget() -> None:
    """max_steps=2 -> 步数上限强制收尾，报告标记 inconclusive。"""
    tool = json.dumps({"thought": "查", "action": "tool", "tool_id": "sales_query", "args": {}}, ensure_ascii=False)
    llm = FakeLLM(
        [
            json.dumps({"title": "t", "subject_type": "order", "subject_id": "o1", "priority": "P3", "severity_guess": "suspicious", "key_questions": [], "skill": "", "plan": []}, ensure_ascii=False),
            tool, tool,
            _SIGNALS,
            json.dumps({"conclusion": "malicious", "confidence": 0.9, "summary": "但调查未完成", "key_findings": [], "recommendations": [], "knowledge_distillation": ""}, ensure_ascii=False),
        ]
    )
    agent = CaseInvestigationAgent(llm, FakeMCP(), store=_mem_store(), max_steps=2)
    events = [e async for e in agent.investigate("订单异常", user_id="tester")]
    end = next(e for e in events if e["type"] == "end")
    assert end["forced_stop_reason"] == "步数达到上限 2", end
    print("[PASS] 步数预算")


async def _alert_isolation() -> None:
    """案件隔离：alert_id 由服务端注入取证工具，且覆盖模型自填值。"""
    tool = json.dumps(
        {"thought": "查工单", "action": "tool", "tool_id": "ticket_query",
         "args": {"customer_name": "A88721", "alert_id": "MODEL_FAKE"}},
        ensure_ascii=False,
    )
    llm = FakeLLM(
        [
            json.dumps({"title": "t", "subject_type": "account", "subject_id": "A88721", "priority": "P2", "severity_guess": "suspicious", "key_questions": [], "skill": "", "plan": []}, ensure_ascii=False),
            tool,
            '{"thought": "收尾", "action": "finish", "conclusion": "ok"}',
            _SIGNALS,
            json.dumps({"conclusion": "inconclusive", "confidence": 0.3, "summary": "s", "key_findings": [], "recommendations": [], "knowledge_distillation": ""}, ensure_ascii=False),
        ]
    )
    mcp = FakeMCP()
    agent = CaseInvestigationAgent(llm, mcp, store=_mem_store(), max_steps=3)
    async for _ in agent.investigate("账号 A88721 存在异常登录", user_id="tester", alert_id="AL202406120001"):
        pass
    assert mcp.calls, "工具未被调用"
    assert mcp.calls[0][1].get("alert_id") == "AL202406120001", mcp.calls[0]
    assert mcp.calls[0][1].get("customer_name") == "A88721", mcp.calls[0]

    # 未传 alert_id 时不注入（保持既有行为，避免给工具塞空值）
    llm2 = FakeLLM(
        [
            json.dumps({"title": "t", "subject_type": "account", "subject_id": "A88721", "priority": "P2", "severity_guess": "suspicious", "key_questions": [], "skill": "", "plan": []}, ensure_ascii=False),
            tool,
            '{"thought": "收尾", "action": "finish", "conclusion": "ok"}',
            _SIGNALS,
            json.dumps({"conclusion": "inconclusive", "confidence": 0.3, "summary": "s", "key_findings": [], "recommendations": [], "knowledge_distillation": ""}, ensure_ascii=False),
        ]
    )
    mcp2 = FakeMCP()
    agent2 = CaseInvestigationAgent(llm2, mcp2, store=_mem_store(), max_steps=3)
    async for _ in agent2.investigate("账号 A88721 存在异常登录", user_id="tester"):
        pass
    assert mcp2.calls[0][1].get("alert_id") == "MODEL_FAKE", mcp2.calls[0]
    print("[PASS] 案件隔离 alert_id 注入")


def _verdict_rules() -> None:
    """规则表口径：双高危动作互证要升 malicious；单信号/有解释时不得升。"""
    anomalies = [
        {"id": "a1", "kind": "anomaly", "text": "全新Android设备从江苏南京IP登录", "severity": "normal", "source": "clue"},
        {"id": "a2", "kind": "anomaly", "text": "登录后32分钟内改绑新手机号", "severity": "normal", "source": "clue"},
    ]
    absence = {"kind": "trusted", "text": "近7天无销售或订单记录", "covers": ["a1", "a2"], "tool_verified": True}

    # 空结果 trusted 被过滤 + 评分 High → R7 升 malicious
    d = verdict_rules.decide("suspicious", 0.65, anomalies + [absence], 85)
    assert d and d.conclusion == "malicious" and d.rule == "R7", d
    assert d.confidence >= 0.8, d

    # 评分 Low → R6 降 inconclusive
    d = verdict_rules.decide("suspicious", 0.6, anomalies[:1], 40)
    assert d and d.conclusion == "inconclusive" and d.rule == "R6", d

    # 可信解释只覆盖部分异常 → 不得降档（未解释的高危动作仍按行为链计数，High 仍升 malicious）
    partial = {"kind": "trusted", "text": "已核实差旅报备闭环", "covers": ["a1"], "tool_verified": True}
    d = verdict_rules.decide("suspicious", 0.6, anomalies + [partial], 85)
    assert d and d.conclusion == "malicious" and d.rule == "R7", d

    # 可信解释覆盖全部异常 → R3 降 inconclusive（评分不得越过它升档）
    trusted = {"kind": "trusted", "text": "已核实差旅报备闭环", "covers": ["a1", "a2"], "tool_verified": True}
    d = verdict_rules.decide("suspicious", 0.6, anomalies + [trusted], 85)
    assert d and d.conclusion == "inconclusive" and d.rule == "R3", d

    # R5：malicious 但评分 Low → 兜底 inconclusive
    d = verdict_rules.decide("malicious", 0.9, anomalies[1:2], 40)
    assert d and d.conclusion == "inconclusive", d

    # 模型没给评分 → 不做档位改写
    assert verdict_rules.decide("malicious", 0.9, anomalies, -1) is None

    # 线索凭证与工具证据同等：全覆盖的可信解释即使 tool_verified=false 也不降级
    clue_fact = {"kind": "trusted", "text": "公安实名核验通过（code:0，三要素一致）",
                 "covers": ["a1"], "tool_verified": False}
    d = verdict_rules.decide("benign", 0.9, anomalies[:1] + [clue_fact], 20)
    assert d is None, d

    # R8：benign 但评分 Medium → 不得结案
    d = verdict_rules.decide("benign", 0.9, anomalies[:1], 60)
    assert d and d.conclusion == "inconclusive" and d.rule == "R8", d
    print("[PASS] 规则表 R5/R6/R7/R8")


async def main() -> None:
    _extract_json_cases()
    _verdict_rules()
    await _happy_path()
    await _duplicate_breaker()
    await _step_budget()
    await _alert_isolation()
    print("\nALL CASE SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
