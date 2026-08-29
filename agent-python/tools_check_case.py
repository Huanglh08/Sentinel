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
            # report
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
            json.dumps({"conclusion": "malicious", "confidence": 0.9, "summary": "但调查未完成", "key_findings": [], "recommendations": [], "knowledge_distillation": ""}, ensure_ascii=False),
        ]
    )
    agent = CaseInvestigationAgent(llm, FakeMCP(), store=_mem_store(), max_steps=2)
    events = [e async for e in agent.investigate("订单异常", user_id="tester")]
    end = next(e for e in events if e["type"] == "end")
    assert end["forced_stop_reason"] == "步数达到上限 2", end
    print("[PASS] 步数预算")


async def main() -> None:
    _extract_json_cases()
    await _happy_path()
    await _duplicate_breaker()
    await _step_budget()
    print("\nALL CASE SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
