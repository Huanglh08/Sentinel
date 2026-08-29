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

# 调查阶段
PHASE_TRIAGE = "TRIAGE"
PHASE_INVESTIGATE = "INVESTIGATE"
PHASE_REPORT = "REPORT"

# 结论档位（inconclusive 用于调查被中断/证据不足无法判定的退化场景）
SEVERITY_BENIGN = "benign"
SEVERITY_SUSPICIOUS = "suspicious"
SEVERITY_MALICIOUS = "malicious"
SEVERITY_INCONCLUSIVE = "inconclusive"


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
