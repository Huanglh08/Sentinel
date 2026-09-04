"""P3 Responder：审批通过后的受控处置执行器。

职责边界：
- Investigator/工具链路只读（白名单保证）；一切"写动作"必须经人工审批，
  由 Responder 在 APPROVED 后统一执行并留痕 —— 这是从"只读调查"到"闭环处置"的分界。
- 执行模式二选一：
  1) online  ：动作命中 TOOL_BINDING 且 MCP 目录确有该写工具 -> 真调用；
  2) offline ：无绑定/工具缺失 -> 线下人工执行留痕（不假装成功，状态记 OFFLINE）。
- 幂等由审批裁决接口的 first_time 分支保证：每张审批单只执行一次。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 动作关键词 -> MCP 写工具绑定。当前 MCP server 仅暴露只读目录，
# 绑定表为空：所有动作走 offline 留痕。接入真实写工具后在此登记即可切到 online。
TOOL_BINDING: dict[str, str] = {}


class CaseResponder:
    """审批通过后的处置执行器（与调查 Agent 共享 mcp 实例）。"""

    def __init__(self, mcp, tool_binding: dict[str, str] | None = None) -> None:
        self.mcp = mcp
        self.tool_binding = dict(tool_binding if tool_binding is not None else TOOL_BINDING)

    async def execute(self, item: dict, case: dict) -> dict:
        """执行单条已批准的处置建议，返回执行结果（写回处置记录 detail）。"""
        action = str(item.get("action", ""))
        tool_id = self._match_tool(action)
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

    def _match_tool(self, action: str) -> str:
        """按动作描述关键词匹配绑定的写工具；未命中返回空串。"""
        if not action:
            return ""
        for keyword, tool_id in self.tool_binding.items():
            if keyword and keyword in action:
                return tool_id
        return ""
