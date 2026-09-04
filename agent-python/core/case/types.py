"""案件调查领域模型（Phase 1-2）。

定义案件（Case）、证据链步骤（EvidenceStep）、分诊结果（TriageResult）、
调查报告（InvestigationReport）、审批待办（ApprovalItem）等核心类型。字段设计与
backend-java/resources/database/upgrades/v1.1.0/260829_case_investigation.sql
及 260829_case_approval.sql 对齐。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

# ---------------------------------------------------------------------------
# 案件状态机: OPEN -> INVESTIGATING -> REPORTED -> (APPROVED -> RESOLVED | REJECTED) -> CLOSED
# Phase 1 走到 REPORTED；Phase 2 增加审批待办（t_case_approval，独立于案件状态机）。
# ---------------------------------------------------------------------------
CASE_STATUS_OPEN = "OPEN"
CASE_STATUS_INVESTIGATING = "INVESTIGATING"
CASE_STATUS_REPORTED = "REPORTED"
CASE_STATUS_CLOSED = "CLOSED"

# 审批待办状态
APPROVAL_STATUS_PENDING = "PENDING"
APPROVAL_STATUS_APPROVED = "APPROVED"
APPROVAL_STATUS_REJECTED = "REJECTED"

# 处置动作状态（Phase 3：审批通过后自动落处置记录）
ACTION_STATUS_EXECUTED = "EXECUTED"

# 审计操作类型（Phase 3：裁决/归档全量留痕）
AUDIT_OPERATION_APPROVE = "APPROVE"
AUDIT_OPERATION_REJECT = "REJECT"
AUDIT_OPERATION_CLOSE = "CLOSE"
AUDIT_OPERATION_AUTO_APPROVE = "AUTO_APPROVE"  # L1 放权：高置信 benign 自动放行
AUDIT_OPERATION_KB_DISTILL = "KB_DISTILL"  # 知识库飞轮：报告经验回写候选池
# Phase 4：L2 置信阈值自动处置（业界依据：Vigil 置信 >=0.90 自动执行遏制，
# HolmesGPT/Tracecat 动作分级 approval_required_tools —— 置信阈值 x 确定性动作白名单双闸门）
AUDIT_OPERATION_L2_AUTO_APPROVE = "L2_AUTO_APPROVE"  # L2 放权：高置信恶意/可疑 + 低危动作白名单
AUDIT_OPERATION_RULE_MINED = "RULE_MINED"  # Rule Miner：候选规则产出
AUDIT_OPERATION_RULE_APPROVED = "RULE_APPROVED"  # 规则审核通过（HITL）
AUDIT_OPERATION_RULE_REJECTED = "RULE_REJECTED"  # 规则审核驳回（HITL）

# 规则候选状态机（Phase 4 Rule Miner）：PENDING -> (APPROVED | REJECTED)
RULE_STATUS_PENDING = "PENDING"
RULE_STATUS_APPROVED = "APPROVED"
RULE_STATUS_REJECTED = "REJECTED"

# 调查阶段
PHASE_TRIAGE = "TRIAGE"
PHASE_INVESTIGATE = "INVESTIGATE"
PHASE_REPORT = "REPORT"

# 结论档位（inconclusive 用于调查被中断/证据不足无法判定的退化场景）
SEVERITY_BENIGN = "benign"
SEVERITY_SUSPICIOUS = "suspicious"
SEVERITY_MALICIOUS = "malicious"
SEVERITY_INCONCLUSIVE = "inconclusive"

# L2 自动处置允许的结论档位（benign 走 L1 放行；inconclusive 必须人工）
L2_SEVERITY_SET = {SEVERITY_SUSPICIOUS, SEVERITY_MALICIOUS}
# 高危动作硬黑名单（不可配置）：命中任一关键词的建议无条件留人工审批。
# 业界依据：Tracecat 将 falcon.isolate_host 列为敏感动作必须审批 —— 金融风控
# 误封/误冻的客诉与资金代价高于 SOC 隔离主机，取保守端（Vigil/Tracecat 光谱的保守侧）。
L2_ACTION_DENYLIST = ("封禁", "冻结", "止付", "清退", "拦截", "拉黑", "关停", "下架")


@dataclass
class EvidenceStep:
    """一步证据：一次推理/一次工具调用及其观察结果。"""

    step_no: int
    phase: str  # TRIAGE / INVESTIGATE / REPORT
    thought: str = ""
    tool_id: str = ""
    tool_args: dict = field(default_factory=dict)
    observation: str = ""
    is_error: bool = False
    injection_suspected: bool = False  # 工具观察值中检测到间接注入指令（已消毒，留痕审计）
    elapsed_ms: int = 0
    create_time: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TriageResult:
    """Orchestrator/Triage 阶段输出。"""

    title: str = ""
    subject_type: str = "other"  # account/device/ip/order/merchant/other
    subject_id: str = ""
    priority: str = "P2"  # P0/P1/P2/P3
    severity_guess: str = SEVERITY_SUSPICIOUS
    key_questions: list[str] = field(default_factory=list)
    skill: str = ""  # 匹配到的 SOP 技能名（可为空）
    plan: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class InvestigationReport:
    """Reporter 阶段输出的结构化调查报告。"""

    conclusion: str = SEVERITY_INCONCLUSIVE
    confidence: float = 0.0
    summary: str = ""
    key_findings: list[dict] = field(default_factory=list)  # [{finding, evidence_step}]
    recommendations: list[dict] = field(default_factory=list)  # [{action, requires_human_approval, reason}]
    knowledge_distillation: str = ""  # 可沉淀回知识库的经验
    raw: str = ""  # LLM 原始输出（审计用）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ApprovalItem:
    """人工审批待办：报告中 requires_human_approval=true 的处置建议落为待办。"""

    id: str = ""
    case_id: str = ""
    case_no: str = ""
    action: str = ""  # 建议动作（如 冻结账户）
    reason: str = ""  # 建议理由
    status: str = APPROVAL_STATUS_PENDING  # PENDING / APPROVED / REJECTED
    operator: str = ""
    opinion: str = ""
    create_time: str = ""
    update_time: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CaseAction:
    """处置动作记录（Phase 3）：审批 APPROVED 后按建议自动落一条可追溯的处置记录。"""

    id: str = ""
    case_id: str = ""
    case_no: str = ""
    approval_id: str = ""
    action: str = ""
    detail: str = ""
    status: str = ACTION_STATUS_EXECUTED
    operator: str = ""
    create_time: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CaseAudit:
    """审计日志（Phase 3）：裁决/归档等敏感操作全量留痕。"""

    id: str = ""
    case_id: str = ""
    approval_id: str = ""
    operation: str = ""  # APPROVE / REJECT / CLOSE
    target: str = ""  # 操作对象标识（approval_id / case_id）
    operator: str = ""
    detail: str = ""
    create_time: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
