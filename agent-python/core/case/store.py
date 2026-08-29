"""案件持久化（Phase 1-2）。

- 正常态：写入共享 PG 库（与 Java 共用），表见
  backend-java/resources/database/upgrades/v1.1.0/260829_case_investigation.sql
  与 260829_case_approval.sql
- 降级态：DB 不可用时退化为进程内字典（验证链路不依赖 PG），打 warning 不抛异常
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from core.case.types import (
    ACTION_STATUS_EXECUTED,
    APPROVAL_STATUS_PENDING,
    AUDIT_OPERATION_CLOSE,
    CASE_STATUS_CLOSED,
    CASE_STATUS_INVESTIGATING,
    CASE_STATUS_REPORTED,
    EvidenceStep,
)
from core.db import get_pool
from core.memory.store import next_message_id

logger = logging.getLogger(__name__)

_CASE_COLS = (
    "id, case_no, title, status, priority, severity, confidence, subject_type, subject_id, "
    "clue_summary, raw_input, triage, report, user_id, source, tta_ms, ttr_ms, "
    "create_by, create_time, update_time"
)


class CaseStore:
    """t_case / t_case_evidence 的极简 CRUD。DB 不可用时自动降级为内存模式。"""

    def __init__(self) -> None:
        self.enabled = True
        self._mem_cases: dict[str, dict] = {}
        self._mem_evidence: dict[str, list[dict]] = {}
        self._mem_approvals: dict[str, dict] = {}
        self._mem_actions: dict[str, dict] = {}
        self._mem_audits: dict[str, dict] = {}
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
    ) -> dict:
        case_id = next_message_id()
        case_no = f"CASE-{self._now().strftime('%Y%m%d')}-{case_id[-6:]}"
        status = CASE_STATUS_INVESTIGATING
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO t_case ({_CASE_COLS}) VALUES ("
                        "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
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
            "tta_ms": None,
            "ttr_ms": None,
            "create_time": self._now().isoformat(timespec="seconds"),
        }
        self._mem_cases[case_id] = record
        self._mem_evidence[case_id] = []
        return record

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
                        "tool_id, tool_args, observation, is_error, elapsed_ms, create_time) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
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
                            "is_error, elapsed_ms, create_time FROM t_case_evidence "
                            "WHERE case_id=%s ORDER BY step_no",
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
                        "SELECT id, case_id, case_no, action, reason, status, operator, "
                        "opinion, create_time, update_time FROM t_case_approval WHERE 1=1"
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
                    cols = (
                        "id", "case_id", "case_no", "action", "reason", "status",
                        "operator", "opinion", "create_time", "update_time",
                    )
                    return [dict(zip(cols, r)) for r in cur.fetchall()]
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

    def decide_approval(
        self, approval_id: str, *, status: str, operator: str = "", opinion: str = ""
    ) -> dict | None:
        """审批裁决（APPROVED / REJECTED）。

        返回 dict 额外携带 first_time 标记：
        - True：本次为首次裁决（PENDING -> 目标状态），上层可触发处置/审计/通知
        - False：重复裁决，幂等返回当前现状，上层应提示而非重复执行
        """
        from core.case.types import APPROVAL_STATUS_APPROVED, APPROVAL_STATUS_REJECTED

        if status not in (APPROVAL_STATUS_APPROVED, APPROVAL_STATUS_REJECTED):
            raise ValueError(f"非法审批状态: {status}")
        now = self._now()
        if self.enabled:
            try:
                with get_pool().connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE t_case_approval SET status=%s, operator=%s, opinion=%s, "
                        "update_time=%s WHERE id=%s AND status=%s",
                        (status, operator, opinion, now, approval_id, APPROVAL_STATUS_PENDING),
                    )
                    first_time = cur.rowcount > 0
                    cur.execute(
                        "SELECT id, case_id, case_no, action, reason, status, operator, "
                        "opinion, create_time, update_time FROM t_case_approval WHERE id=%s",
                        (approval_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        return None
                    cols = (
                        "id", "case_id", "case_no", "action", "reason", "status",
                        "operator", "opinion", "create_time", "update_time",
                    )
                    item = dict(zip(cols, row))
                    item["first_time"] = first_time
                    if first_time:
                        item.pop("first_time")
                        self._mem_approvals[approval_id] = dict(item)
                        item["first_time"] = True
                    return item
            except Exception:  # noqa: BLE001
                logger.exception("[case] 审批裁决失败: %s", approval_id)
        rec = self._mem_approvals.get(approval_id)
        if rec is None:
            return None
        if rec.get("status") != APPROVAL_STATUS_PENDING:
            item = dict(rec)
            item["first_time"] = False
            return item
        rec.update(
            status=status,
            operator=operator,
            opinion=opinion,
            update_time=now.isoformat(timespec="seconds"),
        )
        item = dict(rec)
        item["first_time"] = True
        return item

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
