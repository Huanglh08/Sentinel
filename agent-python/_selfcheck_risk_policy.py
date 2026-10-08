# -*- coding: utf-8 -*-
"""P2 风险评级配置化自检：真库读写 + 双消费点联动 + DB 降级。

用法: python _selfcheck_risk_policy.py
覆盖: get/save_risk_policy 真库链路、_needs_second_approval 配置化生效、
      DB 异常降级出厂默认/旧缓存。
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from core.case import store as case_store  # noqa: E402
from core.case.store import (  # noqa: E402
    _needs_second_approval,
    get_risk_policy,
    save_risk_policy,
)

PASS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    assert cond, f"FAIL {name} {detail}"
    PASS += 1
    print(f"  ok {PASS:02d} {name}")


def main() -> None:
    # 0) 幂等前置：恢复基线（上次运行中断也可能留下脏策略）
    save_risk_policy(
        enabled=False, confidence=0.90,
        whitelist=["观察", "提醒", "工单", "通知", "标记", "升级", "放行", "解除", "限制"],
        denylist=["封禁", "冻结", "止付", "清退", "拦截", "拉黑", "关停", "下架", "提现"],
        operator="system",
    )
    # 1) 初始 = 出厂基线
    p0 = get_risk_policy(ttl_s=0)
    check("初始 autopilot 关", p0["l2_autopilot_enabled"] is False)
    check("初始阈值 0.90", abs(p0["l2_confidence"] - 0.90) < 1e-9)
    check("初始黑名单含冻结", "冻结" in p0["l2_action_denylist"])
    check("双人复核: 冻结命中", _needs_second_approval("冻结涉案账户"))
    check("双人复核: 观察不命中", not _needs_second_approval("观察账户动态"))

    # 2) 保存新策略：开 autopilot + 黑名单加「强拆」
    p1 = save_risk_policy(
        enabled=True, confidence=0.85,
        whitelist=["观察", "提醒", "工单"],
        denylist=["封禁", "冻结", "强拆"],
        operator="_selfcheck",
    )
    check("保存后 enabled=True", p1["l2_autopilot_enabled"] is True)
    check("保存后阈值 0.85", abs(p1["l2_confidence"] - 0.85) < 1e-9)
    check("保存后 operator 留痕", p1["updated_by"] == "_selfcheck", p1["updated_by"])
    check("配置化生效: 强拆命中", _needs_second_approval("强拆其账号"))
    check("配置化生效: 清退已移出", not _needs_second_approval("清退涉案账号"))
    check("TTL 缓存命中一致", get_risk_policy()["l2_action_denylist"] == ["封禁", "冻结", "强拆"])

    # 3) DB 异常降级：沿用旧缓存
    with patch.object(case_store, "get_pool", side_effect=RuntimeError("db down")):
        p2 = case_store.get_risk_policy(ttl_s=0)
    check("DB 挂沿用旧缓存", p2["l2_autopilot_enabled"] is True and p2["updated_by"] == "_selfcheck")

    # 4) 无缓存 + DB 挂 -> 出厂默认（env/硬编码）
    case_store._risk_policy_cache = (0.0, None)
    with patch.object(case_store, "get_pool", side_effect=RuntimeError("db down")):
        p3 = case_store.get_risk_policy(ttl_s=0)
    check("降级出厂: autopilot 关", p3["l2_autopilot_enabled"] is False)
    check("降级出厂: 黑名单恢复硬编码", "清退" in p3["l2_action_denylist"])
    check("DB 恢复: 复核跟随真库当前值", _needs_second_approval("强拆其账号"))

    # 5) 恢复基线
    save_risk_policy(
        enabled=False, confidence=0.90,
        whitelist=["观察", "提醒", "工单", "通知", "标记", "升级", "放行", "解除", "限制"],
        denylist=["封禁", "冻结", "止付", "清退", "拦截", "拉黑", "关停", "下架", "提现"],
        operator="system",
    )
    pf = get_risk_policy(ttl_s=0)
    check("恢复基线", pf["l2_autopilot_enabled"] is False and abs(pf["l2_confidence"] - 0.90) < 1e-9)

    print(f"\n{PASS} checks passed")


if __name__ == "__main__":
    main()
