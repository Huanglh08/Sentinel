"""补偿式撤销（自动放行撤回）自检：内存模式直驱 store+responder，断言式，无框架。

覆盖：窗口内撤回 / 超窗拒绝 / 重复撤回拒绝 / 人工动作不可撤回 /
补偿登记匹配（限权冻结命中、观察类不挂补偿）/ 存量 await-of-sync 修复回归。
运行：python _selfcheck_revoke.py
"""

import asyncio
import sys
from datetime import datetime, timedelta

from core.case.responder import CaseResponder
from core.case.store import CaseStore
from core.case.types import ACTION_STATUS_REVOKED


def main() -> int:
    store = CaseStore()
    store.enabled = False  # 强制内存模式（链路逻辑与 DB 一致，DB 路径由 FOR UPDATE+rowcount 兜底）
    responder = CaseResponder(mcp=None)

    # 造案 + 造自动放行动作（一条留痕型、一条绑定管控型、一条人工、一条过期）
    now = datetime.now()
    store._mem_cases["c1"] = {"id": "c1", "case_no": "CASE-RVK-001", "status": "REPORTED"}
    acts = store.create_actions(
        "c1", "CASE-RVK-001", "",
        [
            {"action": "放行并持续观察", "detail": "benign 自动放行"},
            {"action": "限权涉事账号", "detail": "L2 自动限权", "subject_id": "A100001"},
        ],
        operator="L1:auto",
    )
    manual = store.create_actions(
        "c1", "CASE-RVK-001", "ap-manual",
        [{"action": "冻结账户", "detail": "人工裁决执行"}],
        operator="张三",
    )
    stale = store.create_actions(
        "c1", "CASE-RVK-001", "",
        [{"action": "标记低风险", "detail": "过期自动动作"}],
        operator="L1:auto",
    )
    for a in store._mem_actions.values():  # 伪造过期动作
        if a["id"] == stale[0]["id"]:
            a["create_time"] = (now - timedelta(minutes=90)).isoformat(timespec="seconds")
    release_id, control_id = acts[0]["id"], acts[1]["id"]
    manual_id, stale_id = manual[0]["id"], stale[0]["id"]

    # 1) 补偿登记：绑定管控的动作挂补偿，留痕型不挂（BPMN compensation handler 语义）
    assert responder.control_type_for("限权涉事账号") == "LIMITED", "限权应挂 LIMITED 补偿"
    assert responder.control_type_for("放行并持续观察") is None, "观察类不应挂补偿"

    # 2) 窗口内撤回成功：状态回退 REVOKED
    rec = store.revoke_auto_action(release_id, window_minutes=30)
    assert rec and rec["status"] == ACTION_STATUS_REVOKED, "窗口内撤回应成功"
    assert store.list_actions("c1")[0]["status"] == ACTION_STATUS_REVOKED, "列表应反映撤回态"

    # 3) 重复撤回拒绝
    try:
        store.revoke_auto_action(release_id, window_minutes=30)
        raise SystemExit("FAIL: 重复撤回未被拒绝")
    except ValueError as e:
        assert "无法重复撤回" in str(e), f"重复撤回提示不符: {e}"

    # 4) 人工动作不可撤回
    try:
        store.revoke_auto_action(manual_id, window_minutes=30)
        raise SystemExit("FAIL: 人工动作未被拒绝")
    except ValueError as e:
        assert "仅系统自动放行" in str(e), f"人工动作提示不符: {e}"

    # 5) 超窗拒绝
    try:
        store.revoke_auto_action(stale_id, window_minutes=30)
        raise SystemExit("FAIL: 超窗未被拒绝")
    except ValueError as e:
        assert "超可撤回窗口" in str(e), f"超窗提示不符: {e}"

    # 6) 存量修复回归：builtin 执行路径不再 await-of-sync（缺 PG 应返回 FAILED 而非 TypeError）
    outcome = asyncio.run(
        responder.execute(
            {"action": "限权涉事账号", "subject_id": "A100001", "reason": "自检"},
            {"case_id": "c1", "case_no": "CASE-RVK-001"},
        )
    )
    assert isinstance(outcome, dict) and outcome.get("status") in {"EXECUTED", "FAILED"}, (
        f"builtin 路径应返回结果而非抛 TypeError: {outcome}"
    )
    assert outcome.get("mode") == "builtin", f"应走 builtin 路径: {outcome}"

    print("selfcheck_revoke: 6/6 PASS")
    print(f"  撤回终态={ACTION_STATUS_REVOKED} 人工={manual_id[:8]}… 过期={stale_id[:8]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
