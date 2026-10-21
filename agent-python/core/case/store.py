"""案件持久化（Phase 1-2）。

- 正常态：写入共享 PG 库（与 Java 共用），表见
  backend-java/resources/database/upgrades/v1.1.0/260829_case_investigation.sql
  与 260829_case_approval.sql
- 降级态：DB 不可用时退化为进程内字典（验证链路不依赖 PG），打 warning 不抛异常
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime

from core.case.types import (
    ACTION_STATUS_EXECUTED,
    APPROVAL_STATUS_PENDING,
    AUDIT_OPERATION_CLOSE,
    AUDIT_OPERATION_KB_DISTILL,
    CASE_STATUS_CLOSED,
    CASE_STATUS_INVESTIGATING,
    CASE_STATUS_REPORTED,
    CASE_STATUS_RESOLVED,
    L2_ACTION_DENYLIST,
    RULE_STATUS_APPROVED,
    RULE_STATUS_PENDING,
    RULE_STATUS_REJECTED,
    EvidenceStep,
)
from core.db import get_pool
from core.guard.indirect import check_kb_distillation
from core.case.ids import next_message_id

logger = logging.getLogger(__name__)

# 知识沉淀最大入库长度（与 t_case_kb_distillation.distillation VARCHAR(2048) 对齐）
KB_DISTILLATION_MAX_CHARS = 2048

_CASE_COLS = (
    "id, case_no, title, status, priority, severity, confidence, subject_type, subject_id, "
    "clue_summary, raw_input, triage, report, user_id, source, fingerprint, tta_ms, ttr_ms, "
    "create_by, create_time, update_time"
)

_APPROVAL_COLS = (
    "id", "case_id", "case_no", "action", "reason", "status", "operator", "opinion",
    "first_operator", "first_opinion", "first_decide_time", "create_time", "update_time",
)


_RISK_POLICY_TTL_S = 30
_risk_policy_cache: tuple[float, dict | None] = (0.0, None)


def _split_keywords(raw: str) -> list[str]:
    return [w.strip() for w in (raw or "").split(",") if w.strip()]


def _policy_defaults() -> dict:
    """出厂默认 = 上线前现状（CASE_L2_* env + L2_ACTION_DENYLIST 硬编码）。"""
    try:
        conf = float(os.getenv("CASE_L2_CONFIDENCE", "0.90"))
    except ValueError:
        conf = 0.9
    return {
        "l2_autopilot_enabled": os.getenv("CASE_L2_AUTOPILOT", "0").strip().lower() in {"1", "true", "on"},
        "l2_confidence": conf,
        "l2_action_whitelist": _split_keywords(
            os.getenv(
                "CASE_L2_ACTION_WHITELIST",
                "观察,提醒,工单,通知,标记,升级,放行,解除,限制",
            )
        ),
        "l2_action_denylist": list(L2_ACTION_DENYLIST),
        "updated_by": "system",
        "update_time": "",
    }


def get_risk_policy(ttl_s: int = _RISK_POLICY_TTL_S) -> dict:
    """读取 L2 风险策略；ttl_s=0 绕缓存。DB 失败沿用旧值/出厂默认。"""
    global _risk_policy_cache
    ts, cached = _risk_policy_cache
    now = time.monotonic()
    if now - ts < ttl_s and cached is not None:
        return cached
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT l2_autopilot_enabled, l2_confidence, l2_action_whitelist, "
                "l2_action_denylist, updated_by, update_time FROM t_risk_policy WHERE id = 1"
            ).fetchone()
    except Exception as e:  # noqa: BLE001
        logger.warning("[case] t_risk_policy 读取失败，降级出厂默认: %s", e)
        return cached if cached is not None else _policy_defaults()
    if row:
        policy = {
            "l2_autopilot_enabled": bool(row[0]),
            "l2_confidence": float(row[1]),
            "l2_action_whitelist": _split_keywords(row[2]),
            "l2_action_denylist": _split_keywords(row[3]),
            "updated_by": row[4] or "system",
            "update_time": str(row[5]) if row[5] else "",
        }
    else:
        policy = _policy_defaults()
    _risk_policy_cache = (now, policy)
    return policy


def save_risk_policy(
    enabled: bool,
    confidence: float,
    whitelist: list[str],
    denylist: list[str],
    operator: str,
) -> dict:
    """更新策略并失效缓存，返回最新值。"""
    global _risk_policy_cache
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE t_risk_policy SET l2_autopilot_enabled = %s, l2_confidence = %s, "
            "l2_action_whitelist = %s, l2_action_denylist = %s, updated_by = %s, "
            "update_time = CURRENT_TIMESTAMP WHERE id = 1",
            (1 if enabled else 0, confidence, ",".join(whitelist), ",".join(denylist), operator),
        )
        conn.commit()
    _risk_policy_cache = (0.0, None)
    return get_risk_policy(ttl_s=0)


def _needs_second_approval(action: str) -> bool:
    """高危动作（命中硬黑名单，如冻结/封禁）需双人复核（C10 手册 R4/R5）。"""
    return any(k in (action or "") for k in get_risk_policy()["l2_action_denylist"])


class CaseStore:
    """t_case / t_case_evidence 的极简 CRUD。DB 不可用时自动降级为内存模式。"""

    def __init__(self) -> None:
        self.enabled = True
        self._mem_cases: dict[str, dict] = {}
        self._mem_evidence: dict[str, list[dict]] = {}
        self._mem_approvals: dict[str, dict] = {}
        self._mem_actions: dict[str, dict] = {}
        self._mem_audits: dict[str, dict] = {}
        self._mem_kb_distillations: dict[str, dict] = {}  # key=case_id（唯一约束）
        self._mem_rule_candidates: dict[str, dict] = {}  # Phase 4 Rule Miner 候选规则
        try:
            get_pool()
        except Exception as e:  # noqa: BLE001
            self.enabled = False
            logger.warning("[case] DB 池不可用，案件持久化降级为内存模式: %s", e)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _now(self) -> datetime:
        return datetime.now()

    # ------------------------------------------------------------------
    # 案件
    # ------------------------------------------------------------------
    def create_case(
        self,
        *,
        title: str,
        raw_input: str,
        clue_summary: str,
        triage: dict,
        user_id: str = "",
        source: str = "MANUAL",
        fingerprint: str = "",
    ) -> dict:
        case_id = next_message_id()
        case_no = f"CASE-{self._now().strftime('%Y%m%d')}-{case_id[-6:]}"
        status = CASE_STATUS_INVESTIGATING
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO t_case ({_CASE_COLS}) VALUES ("
                        "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            case_id,
                            case_no,
                            title,
                            status,
                            triage.get("priority", "P2"),
                            None,
                            None,
                            triage.get("subject_type", "other"),
                            triage.get("subject_id", ""),
                            clue_summary,
                            raw_input,
                            json.dumps(triage, ensure_ascii=False),
                            None,
                            user_id,
                            source,
                            fingerprint,
                            None,
                            None,
                            user_id,
                            self._now(),
                            self._now(),
                        ),
                    )
            except Exception:  # noqa: BLE001
                logger.exception("[case] 案件入库失败，降级内存: %s", case_id)
                self.enabled = False
        record = {
            "id": case_id,
            "case_no": case_no,
            "title": title,
            "status": status,
            "priority": triage.get("priority", "P2"),
            "severity": None,
            "confidence": None,
            "subject_type": triage.get("subject_type", "other"),
            "subject_id": triage.get("subject_id", ""),
            "clue_summary": clue_summary,
            "raw_input": raw_input,
            "triage": triage,
            "report": None,
            "user_id": user_id,
            "source": source,
            "fingerprint": fingerprint,
            "tta_ms": None,
            "ttr_ms": None,
            "create_time": self._now().isoformat(timespec="seconds"),
        }
        self._mem_cases[case_id] = record
        self._mem_evidence[case_id] = []
        return record

    def case_exists_by_fingerprint(self, fingerprint: str) -> bool:
        """巡检幂等（Phase 4）：同指纹线索是否已建案。

        指纹 = sha256(source + clue)，由 patrol 层计算；DB 侧另有部分唯一索引
        uk_case_fingerprint 兜底防并发重复建案。
        """
        if not fingerprint:
            return False
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM t_case WHERE fingerprint=%s AND deleted=0 LIMIT 1",
                        (fingerprint,),
                    )
                    return cur.fetchone() is not None
            except Exception:  # noqa: BLE001
                logger.exception("[case] 线索指纹查重失败 fp=%s", fingerprint)
        return any(c.get("fingerprint") == fingerprint for c in self._mem_cases.values())

    def finish_case(
        self,
        case_id: str,
        *,
        report: dict,
        tta_ms: int,
        ttr_ms: int,
    ) -> None:
        severity = report.get("conclusion")
        confidence = report.get("confidence")
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE t_case SET status=%s, severity=%s, confidence=%s, "
                        "report=%s, tta_ms=%s, ttr_ms=%s, update_time=%s WHERE id=%s",
                        (
                            CASE_STATUS_REPORTED,
                            severity,
                            confidence,
                            json.dumps(report, ensure_ascii=False),
                            tta_ms,
                            ttr_ms,
                            self._now(),
                            case_id,
                        ),
                    )
            except Exception:  # noqa: BLE001
                logger.exception("[case] 案件完结更新失败: %s", case_id)
        record = self._mem_cases.get(case_id)
        if record is not None:
            record.update(
                status=CASE_STATUS_REPORTED,
                severity=severity,
                confidence=confidence,
                report=report,
                tta_ms=tta_ms,
                ttr_ms=ttr_ms,
            )

    # ------------------------------------------------------------------
    # 证据链
    # ------------------------------------------------------------------
    def append_evidence(self, case_id: str, step: EvidenceStep) -> None:
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO t_case_evidence (id, case_id, step_no, phase, thought, "
                        "tool_id, tool_args, observation, is_error, elapsed_ms, tool_status, "
                        "record_count, create_time) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            next_message_id(),
                            case_id,
                            step.step_no,
                            step.phase,
                            step.thought,
                            step.tool_id,
                            json.dumps(step.tool_args, ensure_ascii=False),
                            step.observation,
                            1 if step.is_error else 0,
                            step.elapsed_ms,
                            step.tool_status or "",
                            int(step.record_count or 0),
                            self._now(),
                        ),
                    )
            except Exception:  # noqa: BLE001
                logger.exception("[case] 证据写入失败 case=%s step=%s", case_id, step.step_no)
        self._mem_evidence.setdefault(case_id, []).append(step.to_dict())

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_case(self, case_id: str) -> dict | None:
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        f"SELECT {_CASE_COLS} FROM t_case WHERE id=%s AND deleted=0",
                        (case_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        case = dict(zip(_CASE_COLS.split(", "), row))
                        cur.execute(
                            "SELECT step_no, phase, thought, tool_id, tool_args, observation, "
                            "is_error, elapsed_ms, tool_status, record_count, create_time "
                            "FROM t_case_evidence WHERE case_id=%s ORDER BY step_no",
                            (case_id,),
                        )
                        evidence = []
                        for r in cur.fetchall():
                            item = dict(
                                zip(
                                    (
                                        "step_no",
                                        "phase",
                                        "thought",
                                        "tool_id",
                                        "tool_args",
                                        "observation",
                                        "is_error",
                                        "elapsed_ms",
                                        "tool_status",
                                        "record_count",
                                        "create_time",
                                    ),
                                    r,
                                )
                            )
                            if isinstance(item["tool_args"], str):
                                try:
                                    item["tool_args"] = json.loads(item["tool_args"])
                                except Exception:  # noqa: BLE001
                                    pass
                            evidence.append(item)
                        case["evidence"] = evidence
                        return case
            except Exception:  # noqa: BLE001
                logger.exception("[case] 案件查询失败: %s", case_id)
        case = self._mem_cases.get(case_id)
        if case is not None:
            case = dict(case)
            case["evidence"] = list(self._mem_evidence.get(case_id, []))
        return case

    def list_cases(self, user_id: str = "", limit: int = 20) -> list[dict]:
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    if user_id:
                        cur.execute(
                            "SELECT id, case_no, title, status, priority, severity, "
                            "subject_type, subject_id, create_time FROM t_case "
                            "WHERE deleted=0 AND user_id=%s ORDER BY create_time DESC LIMIT %s",
                            (user_id, limit),
                        )
                    else:
                        cur.execute(
                            "SELECT id, case_no, title, status, priority, severity, "
                            "subject_type, subject_id, create_time FROM t_case "
                            "WHERE deleted=0 ORDER BY create_time DESC LIMIT %s",
                            (limit,),
                        )
                    cols = (
                        "id",
                        "case_no",
                        "title",
                        "status",
                        "priority",
                        "severity",
                        "subject_type",
                        "subject_id",
                        "create_time",
                    )
                    return [dict(zip(cols, r)) for r in cur.fetchall()]
            except Exception:  # noqa: BLE001
                logger.exception("[case] 案件列表查询失败")
        rows = sorted(
            self._mem_cases.values(),
            key=lambda c: c.get("create_time", ""),
            reverse=True,
        )
        if user_id:
            rows = [c for c in rows if c.get("user_id") == user_id]
        return [
            {k: c.get(k) for k in ("id", "case_no", "title", "status", "priority", "severity", "subject_type", "subject_id", "create_time")}
            for c in rows[:limit]
        ]

    # ------------------------------------------------------------------
    # 审批待办（Phase 2 审批闭环）
    # ------------------------------------------------------------------
    def create_approvals(self, case_id: str, case_no: str, items: list[dict]) -> list[dict]:
        """把报告中 requires_human_approval=true 的建议批量落为 PENDING 待办。"""
        created: list[dict] = []
        rows: list[tuple] = []
        now = self._now()
        for it in items:
            aid = next_message_id()
            rec = {
                "id": aid,
                "case_id": case_id,
                "case_no": case_no,
                "action": str(it.get("action", ""))[:255],
                "reason": str(it.get("reason", ""))[:1024],
                "status": APPROVAL_STATUS_PENDING,
                "operator": "",
                "opinion": "",
                "create_time": now.isoformat(timespec="seconds"),
                "update_time": now.isoformat(timespec="seconds"),
            }
            created.append(rec)
            rows.append(
                (
                    rec["id"], rec["case_id"], rec["case_no"], rec["action"], rec["reason"],
                    rec["status"], rec["operator"], rec["opinion"], now, now,
                )
            )
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO t_case_approval (id, case_id, case_no, action, reason, "
                        "status, operator, opinion, create_time, update_time) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        rows,
                    )
            except Exception:  # noqa: BLE001
                logger.exception("[case] 审批待办写入失败 case=%s（降级内存）", case_id)
                self.enabled = False
        for rec in created:
            self._mem_approvals[rec["id"]] = dict(rec)
        return created

    def list_approvals(self, case_id: str = "", status: str = "", limit: int = 100) -> list[dict]:
        """审批待办列表；case_id 为空时返回全部（管理端视角），按创建时间倒序。"""
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    sql = (
                        "SELECT id, case_id, case_no, action, reason, status, operator, opinion, "
                        "first_operator, first_opinion, first_decide_time, create_time, update_time "
                        "FROM t_case_approval WHERE 1=1"
                    )
                    params: list = []
                    if case_id:
                        sql += " AND case_id=%s"
                        params.append(case_id)
                    if status:
                        sql += " AND status=%s"
                        params.append(status)
                    sql += " ORDER BY create_time DESC LIMIT %s"
                    params.append(limit)
                    cur.execute(sql, tuple(params))
                    rows = [
                        dict(zip(_APPROVAL_COLS, r))
                        for r in cur.fetchall()
                    ]
                return rows
            except Exception:  # noqa: BLE001
                logger.exception("[case] 审批列表查询失败")
        rows = sorted(
            self._mem_approvals.values(),
            key=lambda a: a.get("create_time", ""),
            reverse=True,
        )
        if case_id:
            rows = [a for a in rows if a.get("case_id") == case_id]
        if status:
            rows = [a for a in rows if a.get("status") == status]
        return [dict(a) for a in rows[:limit]]

    def approval_stats(self, case_id: str | None = None) -> dict:
        """审批两率（ROADMAP #13，EU AI Act Art.14 4(b) automation bias 监控口径）：

        - rejection_rate：终态审批中 REJECTED 占比（人工否决率，autopilot 落审批前的基线带）
        - admin_override_rate：一级已批的高危审批被终审否决的占比（admin 推翻率）
        分母为 0 时该率为 None（区分"无样本"与"0%"）。
        """
        from core.case.types import (
            APPROVAL_STATUS_APPROVED,
            APPROVAL_STATUS_REJECTED,
        )

        def _assemble(
            approved: int,
            rejected: int,
            pending: int,
            first_confirmed: int,
            admin_overridden: int,
        ) -> dict:
            final_total = approved + rejected
            return {
                "total": final_total + pending,
                "approved": approved,
                "rejected": rejected,
                "pending": pending,
                "first_confirmed": first_confirmed,
                "admin_overridden": admin_overridden,
                "rejection_rate": round(rejected / final_total, 4) if final_total else None,
                "admin_override_rate": round(admin_overridden / first_confirmed, 4) if first_confirmed else None,
            }

        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    sql = (
                        "SELECT COUNT(*) FILTER (WHERE status='APPROVED'), COUNT(*) FILTER (WHERE status='REJECTED'), COUNT(*) FILTER (WHERE status='PENDING'), COUNT(*) FILTER (WHERE first_operator IS NOT NULL AND first_operator<>''), COUNT(*) FILTER (WHERE status='REJECTED'   AND first_operator IS NOT NULL AND first_operator<>'') FROM t_case_approval"
                    )
                    params = []
                    if case_id:
                        sql += " WHERE case_id=%s"
                        params.append(case_id)
                    cur.execute(sql, tuple(params))
                    row = cur.fetchone() or (0, 0, 0, 0, 0)
                    return _assemble(*(int(x) for x in row))
            except Exception:  # noqa: BLE001
                logger.exception("[case] 审批统计查询失败（降级内存）")
        rows = [a for a in self._mem_approvals.values() if not case_id or a.get("case_id") == case_id]
        return _assemble(
            sum(1 for a in rows if a.get("status") == APPROVAL_STATUS_APPROVED),
            sum(1 for a in rows if a.get("status") == APPROVAL_STATUS_REJECTED),
            sum(1 for a in rows if a.get("status") == APPROVAL_STATUS_PENDING),
            sum(1 for a in rows if a.get("first_operator")),
            sum(1 for a in rows if a.get("status") == APPROVAL_STATUS_REJECTED and a.get("first_operator")),
        )

    def decide_approval(
        self,
        approval_id: str,
        *,
        status: str,
        operator: str = "",
        opinion: str = "",
        operator_role: str = "",
    ) -> dict | None:
        """审批裁决（APPROVED / REJECTED），高危动作权限分层复核（C10 R4/R5）。

        返回 dict 额外携带 first_time / pending_second 标记：
        - first_time=True：本次裁决使审批落为终态（PENDING -> 目标状态），
          上层才执行处置/审计/通知；False 为重复裁决，幂等返回现状；
        - pending_second=True：高危动作一级已批（记 first_*，状态保持 PENDING），
          待管理员（admin）终审再次 APPROVED；REJECTED 一票即否决终态。
        终审（二级复核）要求 operator_role == admin：user 只能一级确认申请；
        admin 可自批（共机单管理员场景，审批语义 = 权限分层 + 留痕）。
        角色缺失（operator_role 为空）视为无法判定身份，拒绝终审（fail-closed）。
        """
        from core.case.types import APPROVAL_STATUS_APPROVED, APPROVAL_STATUS_REJECTED

        if status not in (APPROVAL_STATUS_APPROVED, APPROVAL_STATUS_REJECTED):
            raise ValueError(f"非法审批状态: {status}")
        now = self._now()

        def _flags(item: dict, first_time: bool, pending_second: bool) -> dict:
            item["first_time"] = first_time
            item["pending_second"] = pending_second
            return item

        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, case_id, case_no, action, reason, status, operator, opinion, "
                        "first_operator, first_opinion, first_decide_time, create_time, update_time "
                        "FROM t_case_approval WHERE id=%s",
                        (approval_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        return None
                    item = dict(zip(_APPROVAL_COLS, row))
                    if item["status"] != APPROVAL_STATUS_PENDING:
                        return _flags(item, False, False)
                    if status == APPROVAL_STATUS_REJECTED or not _needs_second_approval(
                        item["action"]
                    ):
                        cur.execute(
                            "UPDATE t_case_approval SET status=%s, operator=%s, opinion=%s, "
                            "update_time=%s WHERE id=%s AND status=%s",
                            (status, operator, opinion, now, approval_id, APPROVAL_STATUS_PENDING),
                        )
                        item.update(
                            status=status,
                            operator=operator,
                            opinion=opinion,
                            update_time=now.isoformat(timespec="seconds"),
                        )
                    else:
                        first_op = item.get("first_operator") or ""
                        if not first_op:
                            cur.execute(
                                "UPDATE t_case_approval SET first_operator=%s, first_opinion=%s, "
                                "first_decide_time=%s, update_time=%s WHERE id=%s AND status=%s",
                                (operator, opinion, now, now, approval_id, APPROVAL_STATUS_PENDING),
                            )
                            item.update(
                                first_operator=operator,
                                first_opinion=opinion,
                                first_decide_time=now.isoformat(timespec="seconds"),
                                update_time=now.isoformat(timespec="seconds"),
                            )
                            self._mem_approvals[approval_id] = {
                                k: v for k, v in item.items()
                                if k not in ("first_time", "pending_second")
                            }
                            return _flags(item, False, cur.rowcount > 0)
                        if str(operator_role).strip().lower() != "admin":
                            raise ValueError(
                                f"高危动作需管理员终审（当前角色: {operator_role or '未携带'}）"
                            )
                        cur.execute(
                            "UPDATE t_case_approval SET status=%s, operator=%s, opinion=%s, "
                            "update_time=%s WHERE id=%s AND status=%s",
                            (status, operator, opinion, now, approval_id, APPROVAL_STATUS_PENDING),
                        )
                        item.update(
                            status=status,
                            operator=operator,
                            opinion=opinion,
                            update_time=now.isoformat(timespec="seconds"),
                        )
                    first_time = cur.rowcount > 0
                    self._mem_approvals[approval_id] = {
                        k: v for k, v in item.items()
                        if k not in ("first_time", "pending_second")
                    }
                    return _flags(item, first_time, False)
            except ValueError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("[case] 审批裁决失败: %s", approval_id)
        rec = self._mem_approvals.get(approval_id)
        if rec is None:
            return None
        if rec.get("status") != APPROVAL_STATUS_PENDING:
            return _flags(dict(rec), False, False)
        if status == APPROVAL_STATUS_REJECTED or not _needs_second_approval(
            rec.get("action", "")
        ):
            rec.update(
                status=status,
                operator=operator,
                opinion=opinion,
                update_time=now.isoformat(timespec="seconds"),
            )
            return _flags(dict(rec), True, False)
        first_op = rec.get("first_operator") or ""
        if not first_op:
            rec.update(
                first_operator=operator,
                first_opinion=opinion,
                first_decide_time=now.isoformat(timespec="seconds"),
                update_time=now.isoformat(timespec="seconds"),
            )
            return _flags(dict(rec), False, True)
        if str(operator_role).strip().lower() != "admin":
            raise ValueError(f"高危动作需管理员终审（当前角色: {operator_role or '未携带'}）")
        rec.update(
            status=status,
            operator=operator,
            opinion=opinion,
            update_time=now.isoformat(timespec="seconds"),
        )
        return _flags(dict(rec), True, False)

    def mark_case_resolved(self, case_id: str) -> bool:
        """处置执行成功后推进案件至 RESOLVED（非 RESOLVED 均可推进，幂等，重复推进返回 False）。"""
        now = self._now()
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE t_case SET status=%s, update_time=%s WHERE id=%s AND status <> %s",
                        (CASE_STATUS_RESOLVED, now, case_id, CASE_STATUS_RESOLVED),
                    )
                    if cur.rowcount > 0:
                        rec = self._mem_cases.get(case_id)
                        if rec is not None:
                            rec["status"] = CASE_STATUS_RESOLVED
                            rec["update_time"] = now.isoformat(timespec="seconds")
                        return True
                    return False
            except Exception:  # noqa: BLE001
                logger.exception("[case] 案件状态推进失败: %s", case_id)
        rec = self._mem_cases.get(case_id)
        if rec is None:
            return False
        if rec.get("status") != CASE_STATUS_RESOLVED:
            rec["status"] = CASE_STATUS_RESOLVED
            rec["update_time"] = now.isoformat(timespec="seconds")
            return True
        return False

    # ------------------------------------------------------------------
    # 处置动作 + 审计日志 + 归档（Phase 3）
    # ------------------------------------------------------------------
    def create_actions(
        self,
        case_id: str,
        case_no: str,
        approval_id: str,
        items: list[dict],
        operator: str = "",
    ) -> list[dict]:
        """审批 APPROVED 后按建议批量落处置动作记录（EXECUTED，可追溯）。"""
        created: list[dict] = []
        rows: list[tuple] = []
        now = self._now()
        for it in items:
            aid = next_message_id()
            rec = {
                "id": aid,
                "case_id": case_id,
                "case_no": case_no,
                "approval_id": approval_id,
                "action": str(it.get("action", ""))[:255],
                "detail": str(it.get("detail") or it.get("reason", ""))[:1024],
                "status": ACTION_STATUS_EXECUTED,
                "operator": operator,
                "create_time": now.isoformat(timespec="seconds"),
            }
            created.append(rec)
            rows.append(
                (rec["id"], rec["case_id"], rec["case_no"], rec["approval_id"],
                 rec["action"], rec["detail"], rec["status"], rec["operator"], rec["create_time"])
            )
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO t_case_action (id, case_id, case_no, approval_id, "
                        "action, detail, status, operator, create_time) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        rows,
                    )
            except Exception:  # noqa: BLE001
                logger.exception("[case] 处置动作写入失败 case=%s（降级内存）", case_id)
                self.enabled = False
        for rec in created:
            self._mem_actions[rec["id"]] = dict(rec)
        return created

    def list_actions(self, case_id: str, limit: int = 100) -> list[dict]:
        """案件处置动作列表，按创建时间倒序。"""
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, case_id, case_no, approval_id, action, detail, status, "
                        "operator, create_time FROM t_case_action WHERE case_id=%s "
                        "ORDER BY create_time DESC LIMIT %s",
                        (case_id, limit),
                    )
                    cols = (
                        "id", "case_id", "case_no", "approval_id", "action", "detail",
                        "status", "operator", "create_time",
                    )
                    return [dict(zip(cols, r)) for r in cur.fetchall()]
            except Exception:  # noqa: BLE001
                logger.exception("[case] 处置动作查询失败 case=%s", case_id)
        rows = sorted(
            self._mem_actions.values(),
            key=lambda a: a.get("create_time", ""),
            reverse=True,
        )
        return [dict(a) for a in rows if a.get("case_id") == case_id][:limit]

    def record_audit(
        self,
        *,
        case_id: str = "",
        approval_id: str = "",
        operation: str,
        target: str = "",
        operator: str = "",
        detail: str = "",
    ) -> dict:
        """敏感操作审计留痕（裁决 / 归档），失败仅告警不影响主流程。"""
        aid = next_message_id()
        rec = {
            "id": aid,
            "case_id": case_id,
            "approval_id": approval_id,
            "operation": operation,
            "target": target,
            "operator": operator,
            "detail": str(detail)[:1024],
            "create_time": self._now().isoformat(timespec="seconds"),
        }
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO t_case_audit (id, case_id, approval_id, operation, "
                        "target, operator, detail, create_time) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (rec["id"], rec["case_id"], rec["approval_id"], rec["operation"],
                         rec["target"], rec["operator"], rec["detail"], rec["create_time"]),
                    )
            except Exception:  # noqa: BLE001
                logger.exception("[case] 审计日志写入失败 op=%s", operation)
                self.enabled = False
        self._mem_audits[aid] = rec
        return dict(rec)

    def list_audit(self, case_id: str, limit: int = 100) -> list[dict]:
        """案件审计日志列表，按创建时间倒序。"""
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, case_id, approval_id, operation, target, operator, "
                        "detail, create_time FROM t_case_audit WHERE case_id=%s "
                        "ORDER BY create_time DESC LIMIT %s",
                        (case_id, limit),
                    )
                    cols = (
                        "id", "case_id", "approval_id", "operation", "target",
                        "operator", "detail", "create_time",
                    )
                    return [dict(zip(cols, r)) for r in cur.fetchall()]
            except Exception:  # noqa: BLE001
                logger.exception("[case] 审计日志查询失败 case=%s", case_id)
        rows = sorted(
            self._mem_audits.values(),
            key=lambda a: a.get("create_time", ""),
            reverse=True,
        )
        return [dict(a) for a in rows if a.get("case_id") == case_id][:limit]

    def close_case(self, case_id: str, operator: str = "") -> dict | None:
        """归档关闭案件（REPORTED/INVESTIGATING -> CLOSED）。重复关闭幂等返回。"""
        now = self._now()
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE t_case SET status=%s, update_time=%s "
                        "WHERE id=%s AND status <> %s",
                        (CASE_STATUS_CLOSED, now, case_id, CASE_STATUS_CLOSED),
                    )
                    closed = cur.rowcount > 0
            except Exception:  # noqa: BLE001
                logger.exception("[case] 案件归档失败: %s", case_id)
                closed = None  # DB 异常：转入内存路径
        else:
            closed = None
        if closed is None:
            rec = self._mem_cases.get(case_id)
            if rec is None:
                return None
            if rec.get("status") != CASE_STATUS_CLOSED:
                rec["status"] = CASE_STATUS_CLOSED
                rec["update_time"] = now.isoformat(timespec="seconds")
                closed = True
            else:
                closed = False
            case = dict(rec)
        else:
            case = self.get_case(case_id)
            if case is None:
                return None
        if closed:
            self.record_audit(
                case_id=case_id,
                operation=AUDIT_OPERATION_CLOSE,
                target=case_id,
                operator=operator,
                detail=f"案件归档：{case.get('case_no', '')}",
            )
        return {"case": case, "closed": closed}

    # ------------------------------------------------------------------
    # 知识库飞轮（Phase 3：报告经验回写候选池，后续 Rule Miner 消费）
    # ------------------------------------------------------------------
    _KB_COLS = (
        "id, case_id, case_no, title, conclusion, confidence, "
        "distillation, operator, create_time"
    )

    @staticmethod
    def _parse_report(case: dict) -> dict:
        report = case.get("report")
        if isinstance(report, str):
            try:
                report = json.loads(report)
            except Exception:  # noqa: BLE001
                report = {}
        return report if isinstance(report, dict) else {}

    def distill_knowledge(self, case_id: str, operator: str = "") -> dict | None:
        """案件归档时把报告 knowledge_distillation 回写知识库候选池（幂等）。

        - 无经验可沉淀（字段为空）返回 None；
        - 同一案件重复归档不重复沉淀（case_id 唯一约束）；
        - 落库成功后写 KB_DISTILL 审计留痕。

        写入护栏（ASI04 知识库/记忆投毒）：沉淀文本命中注入模式时**拒绝写入**。
        知识沉淀会经 Rule Miner 拼进 LLM prompt 参与规则挖掘并回注分诊提示词，
        影响面覆盖后续所有案件，故此处口径比报告消毒更严格——只阻断不消毒，
        避免占位符替换产出残缺经验污染规则挖掘输入。
        """
        case = self.get_case(case_id)
        if case is None:
            return None
        report = self._parse_report(case)
        distillation = str(report.get("knowledge_distillation", "")).strip()
        if not distillation:
            return None

        # 先截断再校验：入库内容是截断后的，护栏必须覆盖真正落库的文本
        distillation = distillation[:KB_DISTILLATION_MAX_CHARS]
        safe, inj_hits = check_kb_distillation(distillation)
        if not safe:
            logger.warning(
                "[case] 知识沉淀写入被护栏拦截 case=%s 命中注入模式=%s（内容未入库，见案件审计）",
                case.get("case_no", ""), inj_hits,
            )
            self.record_audit(
                case_id=case_id,
                operation=AUDIT_OPERATION_KB_DISTILL,
                target=case_id,
                operator=operator,
                detail=f"知识飞轮写入被拦截：命中注入模式 {','.join(inj_hits)}，内容未入库",
            )
            return {"distilled": False, "blocked": True, "injection_hits": inj_hits, "record": None}

        now = self._now()
        rec = {
            "id": next_message_id(),
            "case_id": case_id,
            "case_no": case.get("case_no", "") or "",
            "title": str(case.get("title", ""))[:128],
            "conclusion": str(report.get("conclusion", ""))[:16],
            "confidence": report.get("confidence"),
            "distillation": distillation[:2048],
            "operator": operator,
            "create_time": now.isoformat(timespec="seconds"),
        }
        inserted = True
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO t_case_kb_distillation (id, case_id, case_no, title, "
                        "conclusion, confidence, distillation, operator, create_time) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (case_id) DO NOTHING",
                        (
                            rec["id"], rec["case_id"], rec["case_no"], rec["title"],
                            rec["conclusion"], rec["confidence"], rec["distillation"],
                            rec["operator"], now,
                        ),
                    )
                    inserted = cur.rowcount > 0
            except Exception:  # noqa: BLE001
                logger.exception("[case] 知识沉淀回写失败 case=%s（降级内存）", case_id)
                self.enabled = False
        elif case_id in self._mem_kb_distillations:
            inserted = False  # 内存模式幂等：该案件已沉淀过
        if inserted:
            # 内存模式以 case_id 为 key 天然幂等；重复回写不覆盖首条
            self._mem_kb_distillations.setdefault(case_id, dict(rec))
            self.record_audit(
                case_id=case_id,
                operation=AUDIT_OPERATION_KB_DISTILL,
                target=case_id,
                operator=operator,
                detail=f"知识库飞轮回写：{case.get('case_no', '')} -> 候选池",
            )
            logger.info("[case] 知识飞轮回写成功 case=%s", case.get("case_no", ""))
        else:
            logger.info("[case] 知识沉淀已存在，跳过 case=%s", case_id)
        return {"distilled": inserted, "record": self._mem_kb_distillations.get(case_id, dict(rec))}

    def list_kb_distillations(self, conclusion: str = "", limit: int = 100) -> list[dict]:
        """知识库候选池列表（管理端/Rule Miner 消费），按创建时间倒序。"""
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    sql = f"SELECT {self._KB_COLS} FROM t_case_kb_distillation WHERE 1=1"
                    params: list = []
                    if conclusion:
                        sql += " AND conclusion=%s"
                        params.append(conclusion)
                    sql += " ORDER BY create_time DESC LIMIT %s"
                    params.append(limit)
                    cur.execute(sql, tuple(params))
                    return [dict(zip(self._KB_COLS.split(", "), r)) for r in cur.fetchall()]
            except Exception:  # noqa: BLE001
                logger.exception("[case] 知识沉淀列表查询失败")
        rows = sorted(
            self._mem_kb_distillations.values(),
            key=lambda d: d.get("create_time", ""),
            reverse=True,
        )
        if conclusion:
            rows = [d for d in rows if d.get("conclusion") == conclusion]
        return [dict(d) for d in rows[:limit]]

    # ------------------------------------------------------------------
    # 规则候选（Phase 4 Rule Miner）：蒸馏经验 -> 结构化候选规则 -> HITL 审核
    # ------------------------------------------------------------------
    _RULE_COLS = (
        "id, rule_name, condition_text, action_suggestion, severity, confidence, "
        "condition_hash, status, source_case_ids, source_distill_ids, "
        "validation_hit_rate, validation_sample, validation_note, "
        "hit_count, operator, review_operator, review_opinion, create_time, update_time"
    )

    def create_rule_candidates(self, rows: list[dict], operator: str = "") -> tuple[list[dict], int]:
        """批量落候选规则（PENDING）。

        - condition_hash 唯一约束去重（护栏：重复经验不重复产规则）；
        - 返回 (实际新建记录列表, 因重复被跳过的数量)。
        """
        created: list[dict] = []
        rows_sql: list[tuple] = []
        now = self._now()
        for it in rows:
            rid = next_message_id()
            rec = {
                "id": rid,
                "rule_name": str(it.get("rule_name", ""))[:128],
                "condition_text": str(it.get("condition_text", ""))[:1024],
                "action_suggestion": str(it.get("action_suggestion", ""))[:512],
                "severity": str(it.get("severity", ""))[:16],
                "confidence": it.get("confidence"),
                "condition_hash": str(it.get("condition_hash", ""))[:64],
                "status": RULE_STATUS_PENDING,
                "source_case_ids": str(it.get("source_case_ids", ""))[:512],
                "source_distill_ids": str(it.get("source_distill_ids", ""))[:512],
                "validation_hit_rate": None,
                "validation_sample": 0,
                "validation_note": "",
                "hit_count": 0,
                "operator": operator,
                "review_operator": "",
                "review_opinion": "",
                "create_time": now.isoformat(timespec="seconds"),
                "update_time": now.isoformat(timespec="seconds"),
            }
            if not rec["condition_hash"] or not rec["condition_text"]:
                continue
            created.append(rec)
            rows_sql.append(
                (
                    rec["id"], rec["rule_name"], rec["condition_text"], rec["action_suggestion"],
                    rec["severity"], rec["confidence"], rec["condition_hash"], rec["status"],
                    rec["source_case_ids"], rec["source_distill_ids"], rec["operator"], now, now,
                )
            )
        inserted = 0
        skipped = 0
        final: list[dict] = []
        if rows_sql and self.enabled:
            insert_sql = (
                "INSERT INTO t_case_rule_candidate (id, rule_name, condition_text, "
                "action_suggestion, severity, confidence, condition_hash, status, "
                "source_case_ids, source_distill_ids, operator, create_time, update_time) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (condition_hash) DO NOTHING RETURNING id"
            )
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    inserted_ids: set[str] = set()
                    for row in rows_sql:  # 每批 ≤10 条，逐条 RETURNING 语义最稳
                        cur.execute(insert_sql, row)
                        got = cur.fetchone()
                        if got:
                            inserted_ids.add(str(got[0]))
                    inserted = len(inserted_ids)
                    final = [r for r in created if r["id"] in inserted_ids]
                    skipped = len(created) - inserted
            except Exception:  # noqa: BLE001
                logger.exception("[case] 候选规则写入失败（降级内存）")
                self.enabled = False
        if not self.enabled:
            for rec in created:
                existing = next(
                    (r for r in self._mem_rule_candidates.values() if r["condition_hash"] == rec["condition_hash"]),
                    None,
                )
                if existing is not None:
                    skipped += 1
                    continue
                self._mem_rule_candidates[rec["id"]] = rec
                final.append(dict(rec))
        if final:
            logger.info("[case] Rule Miner 候选规则落库 %d 条（重复跳过 %d）", len(final), skipped)
        return final, skipped

    def list_rule_candidates(self, status: str = "", limit: int = 100) -> list[dict]:
        """候选规则列表（管理端/审核视角），按创建时间倒序。"""
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    sql = f"SELECT {self._RULE_COLS} FROM t_case_rule_candidate WHERE 1=1"
                    params: list = []
                    if status:
                        sql += " AND status=%s"
                        params.append(status)
                    sql += " ORDER BY create_time DESC LIMIT %s"
                    params.append(limit)
                    cur.execute(sql, tuple(params))
                    return [dict(zip(self._RULE_COLS.split(", "), r)) for r in cur.fetchall()]
            except Exception:  # noqa: BLE001
                logger.exception("[case] 候选规则列表查询失败")
        rows = sorted(
            self._mem_rule_candidates.values(),
            key=lambda r: r.get("create_time", ""),
            reverse=True,
        )
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return [dict(r) for r in rows[:limit]]

    def get_rule_candidate(self, rule_id: str) -> dict | None:
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        f"SELECT {self._RULE_COLS} FROM t_case_rule_candidate WHERE id=%s",
                        (rule_id,),
                    )
                    row = cur.fetchone()
                    return dict(zip(self._RULE_COLS.split(", "), row)) if row else None
            except Exception:  # noqa: BLE001
                logger.exception("[case] 候选规则查询失败: %s", rule_id)
        return self._mem_rule_candidates.get(rule_id)

    def review_rule_candidate(
        self, rule_id: str, *, status: str, operator: str = "", opinion: str = ""
    ) -> dict | None:
        """候选规则 HITL 审核（APPROVED / REJECTED），first_time 幂等语义同 decide_approval。"""
        if status not in (RULE_STATUS_APPROVED, RULE_STATUS_REJECTED):
            raise ValueError(f"非法规则状态: {status}")
        now = self._now()
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE t_case_rule_candidate SET status=%s, review_operator=%s, "
                        "review_opinion=%s, update_time=%s WHERE id=%s AND status=%s",
                        (status, operator, opinion, now, rule_id, RULE_STATUS_PENDING),
                    )
                    first_time = cur.rowcount > 0
                    cur.execute(
                        f"SELECT {self._RULE_COLS} FROM t_case_rule_candidate WHERE id=%s",
                        (rule_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        return None
                    item = dict(zip(self._RULE_COLS.split(", "), row))
                    item["first_time"] = first_time
                    return item
            except Exception:  # noqa: BLE001
                logger.exception("[case] 候选规则审核失败: %s", rule_id)
        rec = self._mem_rule_candidates.get(rule_id)
        if rec is None:
            return None
        if rec.get("status") != RULE_STATUS_PENDING:
            item = dict(rec)
            item["first_time"] = False
            return item
        rec.update(
            status=status,
            review_operator=operator,
            review_opinion=opinion,
            update_time=now.isoformat(timespec="seconds"),
        )
        item = dict(rec)
        item["first_time"] = True
        return item

    def update_rule_validation(
        self, rule_id: str, *, hit_rate: float, sample: int, note: str = ""
    ) -> dict | None:
        """历史案件回放验证结果回写（Google 检测工程 Agent 的合成遥测验证环节，
        Sentinel 对应物：规则在历史案件集上的命中率回放）。"""
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE t_case_rule_candidate SET validation_hit_rate=%s, "
                        "validation_sample=%s, validation_note=%s, update_time=%s WHERE id=%s",
                        (hit_rate, sample, note[:512], self._now(), rule_id),
                    )
                    if cur.rowcount <= 0:
                        return None
            except Exception:  # noqa: BLE001
                logger.exception("[case] 规则验证回写失败: %s", rule_id)
        rec = self._mem_rule_candidates.get(rule_id)
        if rec is None:
            return None
        rec.update(
            validation_hit_rate=hit_rate,
            validation_sample=sample,
            validation_note=note[:512],
            update_time=self._now().isoformat(timespec="seconds"),
        )
        return dict(rec)

    def record_rule_hits(self, rule_ids: list[str]) -> None:
        """规则被分诊引用时 hit_count +1（生命周期数据：后续淘汰策略消费）。"""
        if not rule_ids:
            return
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    cur.executemany(
                        "UPDATE t_case_rule_candidate SET hit_count=hit_count+1, "
                        "update_time=%s WHERE id=%s",
                        [(self._now(), rid) for rid in rule_ids],
                    )
            except Exception:  # noqa: BLE001
                logger.exception("[case] 规则命中计数失败")
        for rid in rule_ids:
            rec = self._mem_rule_candidates.get(rid)
            if rec is not None:
                rec["hit_count"] = int(rec.get("hit_count") or 0) + 1

    def list_rule_hints(self, limit: int = 5) -> list[dict]:
        """分诊注入用：APPROVED 规则 top-N（confidence 优先、hit_count 次之）。"""
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        f"SELECT {self._RULE_COLS} FROM t_case_rule_candidate "
                        "WHERE status=%s ORDER BY confidence DESC, hit_count DESC, "
                        "create_time DESC LIMIT %s",
                        (RULE_STATUS_APPROVED, limit),
                    )
                    return [dict(zip(self._RULE_COLS.split(", "), r)) for r in cur.fetchall()]
            except Exception:  # noqa: BLE001
                logger.exception("[case] 规则提示查询失败")
        rows = [r for r in self._mem_rule_candidates.values() if r.get("status") == RULE_STATUS_APPROVED]
        rows.sort(key=lambda r: (float(r.get("confidence") or 0), int(r.get("hit_count") or 0)), reverse=True)
        return [dict(r) for r in rows[:limit]]
