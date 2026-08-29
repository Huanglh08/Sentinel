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
    APPROVAL_STATUS_PENDING,
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
        """审批裁决（APPROVED / REJECTED）。重复裁决直接拒绝，返回 None。"""
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
                    if cur.rowcount == 0:
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
                        return dict(zip(cols, row))  # 已裁决过：原样返回供幂等提示
            except Exception:  # noqa: BLE001
                logger.exception("[case] 审批裁决失败: %s", approval_id)
        rec = self._mem_approvals.get(approval_id)
        if rec is None:
            return None
        if rec.get("status") != APPROVAL_STATUS_PENDING:
            return dict(rec)
        rec.update(
            status=status,
            operator=operator,
            opinion=opinion,
            update_time=now.isoformat(timespec="seconds"),
        )
        return dict(rec)
