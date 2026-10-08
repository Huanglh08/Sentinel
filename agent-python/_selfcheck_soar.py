"""SOAR 处置闭环自检：权限分层审批状态机 + RESOLVED 推进（内存模式，无需 DB）。

规则（P0 层级审批）：高危动作一级确认任意角色可做；终审（二级复核）必须
admin 角色；角色缺失 fail-closed；admin 可自批（共机单管理员场景）。

运行（agent-python 目录）：
  C:/Users/30970/AppData/Local/Programs/Python/Python312/python.exe _selfcheck_soar.py
"""
from __future__ import annotations

from core.case.store import CaseStore
from core.case.types import (
    APPROVAL_STATUS_APPROVED,
    APPROVAL_STATUS_PENDING,
    APPROVAL_STATUS_REJECTED,
)


def main() -> int:
    store = CaseStore()
    store.enabled = False  # 强制内存模式

    case = store.create_case(
        title="SOAR 自检案件",
        raw_input="selfcheck",
        clue_summary="权限分层审批状态机自检",
        triage={"risk_level": "R4"},
    )
    case_id, case_no = case["id"], case["case_no"]
    low, freeze, ban, self_batch = store.create_approvals(
        case_id,
        case_no,
        [
            {"action": "发送提醒短信", "reason": "低危动作：单人裁决即终态"},
            {"action": "冻结涉案账户 acct-1001", "reason": "高危动作（命中 DENYLIST 冻结）：需管理员终审"},
            {"action": "封禁涉案设备 d-9", "reason": "高危动作（命中 DENYLIST 封禁）：一票否决"},
            {"action": "冻结涉案账户 acct-1002", "reason": "高危动作：admin 自批场景"},
        ],
    )

    # 1) 低危动作：单人裁决即终态（无需角色）；重复裁决幂等
    r1 = store.decide_approval(low["id"], status="APPROVED", operator="op1", opinion="ok")
    assert r1["first_time"] and r1["status"] == APPROVAL_STATUS_APPROVED and not r1["pending_second"], r1
    r1b = store.decide_approval(low["id"], status="APPROVED", operator="op2")
    assert not r1b["first_time"], r1b

    # 2) 高危动作：员工（user 角色）一级确认申请，状态保持 PENDING
    r2 = store.decide_approval(freeze["id"], status="APPROVED", operator="op1", operator_role="user")
    assert r2["pending_second"] and not r2["first_time"], r2
    assert r2["status"] == APPROVAL_STATUS_PENDING and r2["first_operator"] == "op1", r2

    # 3) user 终审被拒（权限分层）
    try:
        store.decide_approval(freeze["id"], status="APPROVED", operator="op2", operator_role="user")
        raise AssertionError("user 终审未抛 ValueError")
    except ValueError:
        pass

    # 3b) 角色缺失 fail-closed
    try:
        store.decide_approval(freeze["id"], status="APPROVED", operator="op2")
        raise AssertionError("无角色终审未抛 ValueError")
    except ValueError:
        pass

    # 4) 管理员 APPROVED：落终态，operator=终审人
    r4 = store.decide_approval(freeze["id"], status="APPROVED", operator="op2", operator_role="admin", opinion="同意冻结")
    assert r4["first_time"] and r4["status"] == APPROVAL_STATUS_APPROVED, r4
    assert r4["operator"] == "op2" and r4["first_operator"] == "op1", r4

    # 4b) admin 自批通过：同一 admin 一级确认 + 终审（共机单管理员场景）
    rb1 = store.decide_approval(self_batch["id"], status="APPROVED", operator="boss", operator_role="admin")
    assert rb1["pending_second"], rb1
    rb2 = store.decide_approval(self_batch["id"], status="APPROVED", operator="boss", operator_role="admin", opinion="管理员单人执行")
    assert rb2["first_time"] and rb2["status"] == APPROVAL_STATUS_APPROVED, rb2

    # 5) 高危一票否决：REJECTED 是保守动作，任意角色直接终态
    store.decide_approval(ban["id"], status="APPROVED", operator="op1")
    r5 = store.decide_approval(ban["id"], status="REJECTED", operator="op2")
    assert r5["first_time"] and r5["status"] == APPROVAL_STATUS_REJECTED, r5

    # 6) RESOLVED 推进幂等
    assert store.mark_case_resolved(case_id) is True
    assert store.mark_case_resolved(case_id) is False
    assert store.get_case(case_id)["status"] == "RESOLVED"

    # 7) approval_stats 两率（#13 override rate）：approved=3/rejected=1/
    #    first_confirmed=3（freeze/ban/self_batch）/admin_overridden=1（ban 终审否决一级已批）
    stats = store.approval_stats()
    assert stats["approved"] == 3 and stats["rejected"] == 1, stats
    assert stats["first_confirmed"] == 3 and stats["admin_overridden"] == 1, stats
    assert abs(stats["rejection_rate"] - 0.25) < 1e-9, stats
    assert abs(stats["admin_override_rate"] - round(1 / 3, 4)) < 1e-9, stats
    # 无样本分母保护：无审批的案件返回 None 而非 0
    empty = store.create_case(title="空", raw_input="x", clue_summary="", triage={})
    assert store.approval_stats(case_id=empty["id"])["rejection_rate"] is None
    assert store.approval_stats(case_id=empty["id"])["admin_override_rate"] is None

    print("SOAR self-check: 8/8 passed (role-based approval + admin self-batch + RESOLVED idempotent)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
