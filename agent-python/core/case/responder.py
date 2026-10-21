"""P3 Responder：审批通过后的受控处置执行器。

职责边界：
- Investigator/工具链路只读（白名单保证）；一切"写动作"必须经人工审批，
  由 Responder 在 APPROVED 后统一执行并留痕 —— 这是从"只读调查"到"闭环处置"的分界。
- 执行模式三选一：
  1) builtin：动作命中 TOOL_BINDING 且绑定的是内置执行器 -> 直接落 PG 管控表
     （t_risk_account_control，260904_soar_execution.sql）。写工具刻意不注册进
     MCP server：该进程与调查 Agent 共享，注册进去会让 LLM 调查链路物理可达
     写动作，破坏"写动作必须审批"的安全边界；
  2) online ：绑定 MCP 写工具且目录确有 -> 真调用（预留，当前无此绑定）；
  3) offline：无绑定 -> 线下人工执行留痕（不假装成功，状态记 OFFLINE）。
- 幂等由审批裁决接口的 first_time 分支保证：每张审批单只执行一次；
  管控表再以 (subject_id, control_type) 唯一生效索引兜底去重。
"""

from __future__ import annotations

import logging

from core.db import get_pool
from core.case.ids import next_message_id

logger = logging.getLogger(__name__)

# 动作关键词 -> 执行器绑定。值命中 _BUILTIN_CONTROLS 的走内置执行器（直落 PG 管控表）；
# 其他值视为 MCP 写工具名（online 路径，预留，当前无此绑定）。
TOOL_BINDING: dict[str, str] = {
    "限权": "risk_limit_account",
    "冻结": "risk_freeze_account",
}

# 内置执行器：tool_id -> 管控类型（写动作不经 MCP，见模块 docstring 安全边界说明）
_BUILTIN_CONTROLS: dict[str, str] = {
    "risk_limit_account": "LIMITED",
    "risk_freeze_account": "FROZEN",
}

# 唯一生效索引 (subject_id, control_type) WHERE status='ACTIVE' 兜底幂等；
# RETURNING xmax = 0 区分"新建"与"冲突更新"（xmax 仅在 UPDATE 时变化）。
_UPSERT_SQL = (
    "INSERT INTO t_risk_account_control (id, case_id, case_no, approval_id, subject_id, "
    "control_type, status, operator, detail,  create_time, update_time) "
    "VALUES (%s, %s, %s, %s, %s, %s, 'ACTIVE', %s, %s, now(), now()) "
    "ON CONFLICT (subject_id, control_type) WHERE status = 'ACTIVE' DO UPDATE SET "
    "case_id = EXCLUDED.case_id, case_no = EXCLUDED.case_no, "
    "approval_id = EXCLUDED.approval_id, operator = EXCLUDED.operator, "
    "detail = EXCLUDED.detail, update_time = now() "
    "RETURNING xmax = 0 AS inserted"
)


class CaseResponder:
    """审批通过后的处置执行器（与调查 Agent 共享 mcp 实例；写动作不经 MCP）。"""

    def __init__(self, mcp, tool_binding: dict[str, str] | None = None) -> None:
        self.mcp = mcp
        self.tool_binding = dict(tool_binding if tool_binding is not None else TOOL_BINDING)

    async def execute(self, item: dict, case: dict) -> dict:
        """执行单条已批准的处置建议，返回执行结果（写回处置记录 detail）。"""
        action = str(item.get("action", ""))
        tool_id = self._match_tool(action)
        if tool_id in _BUILTIN_CONTROLS:
            return await self._execute_builtin(tool_id, action, item, case)
        if tool_id:
            try:
                tools = await self.mcp.list_tools()
                available = {str(t.get("name", "")) for t in tools or []}
                if tool_id in available:
                    is_error, text = await self.mcp.call(tool_id, {"action": action})
                    if is_error:
                        detail = f"Responder online 执行失败: tool={tool_id}, err={str(text)[:500]}"
                        logger.warning("[responder] online 失败降级留痕 case=%s action=%s",
                                       case.get("case_no", ""), action)
                        return {"status": "FAILED", "mode": "online", "tool_id": tool_id, "detail": detail}
                    detail = f"Responder online 执行成功: tool={tool_id}, result={str(text)[:500]}"
                    logger.info("[responder] online 执行 case=%s action=%s tool=%s",
                                case.get("case_no", ""), action, tool_id)
                    return {"status": "EXECUTED", "mode": "online", "tool_id": tool_id, "detail": detail}
                logger.warning("[responder] 绑定工具 %s 不在 MCP 目录，降级 offline: case=%s",
                               tool_id, case.get("case_no", ""))
            except Exception as e:  # noqa: BLE001
                logger.exception("[responder] online 执行失败，降级 offline: %s", e)
        detail = (
            f"Responder offline 留痕: 动作《{action}》已获审批（{case.get('case_no', '')}），"
            f"由值班人员线下执行并在工单回填结果"
        )
        return {"status": "OFFLINE", "mode": "offline", "tool_id": "", "detail": detail}

    def _execute_builtin(self, tool_id: str, action: str, item: dict, case: dict) -> dict:
        """限权/冻结落管控表（同步短操作；PG 不可用即 FAILED，不假装成功）。"""
        subject_id = str(item.get("subject_id") or case.get("subject_id") or "").strip()
        if not subject_id:
            return {
                "status": "FAILED",
                "mode": "builtin",
                "tool_id": tool_id,
                "detail": f"管控对象缺失（subject_id 为空），拒绝盲执行《{action}》",
            }
        control_type = _BUILTIN_CONTROLS[tool_id]
        try:
            with get_pool().connection() as conn, conn.cursor() as cur:
                cur.execute(
                    _UPSERT_SQL,
                    (
                        next_message_id(),
                        str(case.get("case_id") or case.get("id") or ""),
                        str(case.get("case_no") or ""),
                        str(item.get("approval_id") or case.get("id") or ""),
                        subject_id,
                        control_type,
                        str(item.get("operator") or case.get("operator") or ""),
                        str(item.get("reason") or action)[:1024],
                    ),
                )
                row = cur.fetchone()
                created = bool(row and row[0])
        except Exception as e:  # noqa: BLE001
            logger.exception("[responder] builtin 执行失败 case=%s action=%s",
                             case.get("case_no", ""), action)
            return {
                "status": "FAILED",
                "mode": "builtin",
                "tool_id": tool_id,
                "detail": f"管控表写入失败（PG 不可用？）: {str(e)[:300]}",
            }
        verb = "新建管控" if created else "已存在生效管控（幂等更新归属）"
        detail = (
            f"Responder builtin 执行成功: {verb} subject={subject_id} "
            f"type={control_type} tool={tool_id}（t_risk_account_control）"
        )
        logger.info(
            "[responder] builtin 执行 case=%s action=%s tool=%s subject=%s",
            case.get("case_no", ""), action, tool_id, subject_id,
        )
        return {"status": "EXECUTED", "mode": "builtin", "tool_id": tool_id, "detail": detail}

    def _match_tool(self, action: str) -> str:
        """按动作描述关键词匹配绑定的执行器；未命中返回空串。"""
        if not action:
            return ""
        for keyword, tool_id in self.tool_binding.items():
            if keyword and keyword in action:
                return tool_id
        return ""
