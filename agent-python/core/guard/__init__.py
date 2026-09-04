"""红队防护（三层，对齐 LlamaFirewall 架构思路的轻量实现）：

第 1 层 input_guard  —— 输入确定性规则分类（PromptGuard 角色替代），命中即短路拒答；
第 2 层 hardening + trajectory —— 静态安全守则 + 轨迹审计（rewrite 子问题弱信号
       扫描 → 可疑时动态注入针对性守则，AlignmentCheck 角色的成本最优替代）；
第 3 层 output_guard —— 输出载荷检测 + 长度硬顶（CodeShield 角色替代），断流补拒答。
"""
from core.guard.hardening import HARDENING_RULES
from core.guard.input_guard import InputGuard, GuardVerdict
from core.guard.output_guard import OutputGuard
from core.guard.trajectory import (
    ANTI_BYPASS_CONSTRAINTS,
    SINGLE_INJECT_CONSTRAINTS,
    TrajectoryAuditor,
    TrajectoryAudit,
    TRAJECTORY_CONSTRAINTS,
)

__all__ = [
    "HARDENING_RULES",
    "InputGuard",
    "GuardVerdict",
    "OutputGuard",
    "TrajectoryAuditor",
    "TrajectoryAudit",
    "TRAJECTORY_CONSTRAINTS",
    "ANTI_BYPASS_CONSTRAINTS",
    "SINGLE_INJECT_CONSTRAINTS",
]
