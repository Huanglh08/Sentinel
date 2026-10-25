"""案件调查图引擎（LangGraph 最小子集，参考其检查点模型）。

只取三样能力：
  1. 线性图：分诊 -> 调查(ReAct 逐步) -> 关联 -> 报告+行动，每个图步完成即存档；
  2. 检查点：官方 Postgres 存档器，复用现有 Postgres 实例（独立连接池、自带表）；
  3. 恢复：线程号=案件号，按案件号从最近检查点回放已完成步骤，续跑未完成部分。

决策铁律（续跑不漂移的关键）：
  - 已存档步骤的观察与决策原样回放，绝不重新问模型；
  - 只有崩溃时正在飞行的那一步会重算（LangGraph 节点级原子性，与框架语义一致）；
  - 恢复后在行动阶段重读当前风险策略（get_risk_policy 30s 缓存，与旧行为一致）；
  - 自动处置执行前按 (案件, 动作) 查重 + 部分唯一索引兜底，任何路径不重复执行。

高危审批保持既有审批表异步闭环不变（审批本就是调查结束后的持久化暂停点）；
框架的 interrupt 暂停原语留给未来「调查中等待审批」的语义。

降级路径（不影响现有链路）：
  - 未安装 langgraph / 检查点库未就绪 -> 编排回落旧引擎（行为逐字一致）；
  - 开关 CASE_GRAPH_ENGINE 未开（默认关）-> 走旧引擎；
  - Postgres 不可用但开关已开 -> 图引擎无持久化运行（不可续跑，其余行为不变）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import time
from collections import Counter
from typing import AsyncIterator, TypedDict

from core.case.agent import (
    STAGE_ACT,
    STAGE_CORRELATE,
    STAGE_DETECT,
    STAGE_ENRICH,
    STAGE_RECOMMEND,
    CaseInvestigationAgent,
    _execution_mode,
)
from core.case.ids import next_message_id
from core.case.notify import notify_approval_created
from core.case.store import get_risk_policy
from core.case.types import (
    L2_SEVERITY_SET,
    PHASE_INVESTIGATE,
    PHASE_REPORT,
    PHASE_TRIAGE,
    TriageResult,
)
from core.db import get_dsn

logger = logging.getLogger("case.graph")


# --------------------------------------------------------------------------
# 检查点（官方 Postgres 存档器，异步版；模块级单例，进程内复用连接池）
# --------------------------------------------------------------------------
_checkpointer = None
_checkpointer_tried = False


async def _get_checkpointer():
    """返回 AsyncPostgresSaver 或 None（库缺失/PG 不可用时降级为无持久化图）。"""
    global _checkpointer, _checkpointer_tried
    if _checkpointer_tried:
        return _checkpointer
    _checkpointer_tried = True
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            get_dsn(), open=False, min_size=0, max_size=2, kwargs={"autocommit": True}, check=False
        )
        await pool.open()
        saver = AsyncPostgresSaver(pool)
        await saver.setup()  # 自管检查点表，一次建齐；业务表零改动
        _checkpointer = saver
        logger.info("[case-graph] Postgres 检查点就绪")
    except Exception:  # noqa: BLE001
        logger.exception("[case-graph] 检查点初始化失败，图引擎将以无持久化模式运行")
        _checkpointer = None
    return _checkpointer


# --------------------------------------------------------------------------
# 图状态（全部 JSON 可序列化；下划线开头键会被 LangGraph 排除，避免使用）
# --------------------------------------------------------------------------
class GraphState(TypedDict, total=False):
    clue: str
    alert_id: str
    user_id: str
    source: str
    fingerprint: str
    case_id: str          # 新跑时由引擎预生成，线程号=案件号，续跑按此寻址
    case: dict
    case_no: str
    triage: dict
    steps: list           # EvidenceStep.to_dict() 列表（已存档步骤的唯一事实源）
    messages: list        # ReAct 会话（回放即续跑，不重新问模型）
    call_counter: dict
    repeat_warned: bool
    forced_stop_reason: str
    budget_deadline_wall: float   # 墙钟预算；续跑前由引擎重置（重新计时）
    signals: list
    report: dict | None
    insufficient: bool
    started_wall: float
    tta_ms: int
    route: str            # 单步路由瞬态：continue/finish/break/done
    approvals: list
    l1_actions: list
    l2_actions: list
    act_mode: str
    act_auto: int
    act_pending: int


def _obj_from_dict(cls, data: dict):
    """按 dataclass 字段过滤重建对象（检查点里多出的键忽略，向前兼容）。"""
    known = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


# --------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------
class CaseGraphEngine:
    """把既有 CaseInvestigationAgent 的调查编排搬进线性图，事件协议与旧引擎逐字对齐。"""

    def __init__(self, agent: CaseInvestigationAgent) -> None:
        self._agent = agent
        self._graph = None
        self._graph_tried = False
        self._locks: dict[str, asyncio.Lock] = {}  # 进程内续跑互斥（当前单机部署）

    # -- 图构建 ------------------------------------------------------------
    async def _ensure_graph(self):
        if self._graph_tried:
            return self._graph
        self._graph_tried = True
        try:
            from langgraph.graph import END, START, StateGraph
        except Exception:  # noqa: BLE001
            logger.exception("[case-graph] langgraph 未安装，回落旧引擎")
            return None

        agent = self._agent

        async def node_triage(state: GraphState) -> dict:
            clue = state["clue"]
            triage, _triage_steps = await agent._triage(clue)
            started_wall = state.get("started_wall") or time.time()
            tta_ms = int((time.time() - started_wall) * 1000)
            case = agent.store.create_case(
                title=triage.title or (clue[:30] if clue else "未命名案件"),
                raw_input=clue,
                clue_summary="；".join(triage.key_questions) or clue[:200],
                triage=triage.to_dict(),
                user_id=state.get("user_id", ""),
                source=state.get("source", "MANUAL"),
                fingerprint=state.get("fingerprint", ""),
                case_id=state["case_id"],
            )
            update: dict = {
                "triage": triage.to_dict(),
                "case": dict(case),
                "case_no": case["case_no"],
                "tta_ms": tta_ms,
                "insufficient": False,
                # ReAct 初始消息随分诊落检查点（此前留空导致首个 react 步以空消息调 LLM）
                "messages": await agent._react_seed_messages(clue, triage),
            }
            # 输入完备性闸门：与旧引擎一致，缺输入不消耗调查预算，直接出可操作报告
            if (triage.input_quality or {}).get("sufficient") is False:
                report = agent._build_insufficient_report(triage)
                agent.store.finish_case(
                    state["case_id"], report=report.to_dict(), tta_ms=tta_ms, ttr_ms=tta_ms
                )
                report_dict = report.to_dict()
                report_dict.pop("raw", None)
                update.update({"insufficient": True, "report": report_dict})
            return update

        async def node_react_step(state: GraphState) -> dict:
            obj_steps = [_steps_obj(d) for d in (state.get("steps") or [])]
            counter = Counter(state.get("call_counter") or {})
            messages = list(state.get("messages") or [])
            catalog = await agent._load_catalog()
            allowed = {t["tool_id"] for t in catalog}
            # 预算是墙钟存档（可跨进程持久），比较前换算成单调钟剩余量再传给共用原子操作
            remaining = (state.get("budget_deadline_wall") or (time.time() + agent.max_total_sec)) - time.time()
            route, repeat_warned, forced = await agent._react_once(
                clue=state["clue"],
                triage=_triage_obj(state["triage"]),
                alert_id=state.get("alert_id", ""),
                allowed=allowed,
                messages=messages,
                steps=obj_steps,
                call_counter=counter,
                repeat_warned=bool(state.get("repeat_warned")),
                step_no=len(obj_steps) + 1,
                deadline=time.monotonic() + max(0.0, remaining),
            )
            # 每步即时幂等落库：断点续跑重放同一步覆盖写，证据表不产生重复行
            persisted = len(state.get("steps") or [])
            for s in obj_steps[persisted:]:
                agent.store.replace_evidence_step(state["case_id"], s)
            return {
                "steps": [s.to_dict() for s in obj_steps],
                "messages": messages,
                "call_counter": dict(counter),
                "repeat_warned": repeat_warned,
                "route": route,
                "forced_stop_reason": forced or state.get("forced_stop_reason", ""),
            }

        def route_react(state: GraphState) -> str:
            if state.get("route") in {"finish", "break"}:
                return "wrap"
            if len(state.get("steps") or []) >= agent.max_steps:
                return "wrap"
            return "react"

        async def node_supplement(state: GraphState) -> dict:
            obj_steps = [_steps_obj(d) for d in (state.get("steps") or [])]
            forced = state.get("forced_stop_reason") or ""
            if not forced and state.get("route") not in {"finish", "break"}:
                forced = f"步数达到上限 {agent.max_steps}"
            await agent._supplement_sales(_triage_obj(state["triage"]), obj_steps, state.get("alert_id", ""))
            # 补充步同样即时幂等落库（与旧引擎「steps 全量落证据」语义对齐）
            persisted = len(state.get("steps") or [])
            for s in obj_steps[persisted:]:
                agent.store.replace_evidence_step(state["case_id"], s)
            return {
                "steps": [s.to_dict() for s in obj_steps],
                "forced_stop_reason": forced,
            }

        async def node_correlate(state: GraphState) -> dict:
            obj_steps = [_steps_obj(d) for d in (state.get("steps") or [])]
            try:
                signals = await agent._extract_signals(state["clue"], obj_steps)
            except Exception as e:  # noqa: BLE001
                logger.warning("[case-graph] 关联信号抽取失败: %s", e)
                signals = []
            return {"signals": signals}

        async def node_report_act(state: GraphState) -> dict:
            case = dict(state.get("case") or {})
            triage_obj = _triage_obj(state["triage"])
            obj_steps = [_steps_obj(d) for d in (state.get("steps") or [])]
            report = await agent._report(
                state["clue"], triage_obj, obj_steps,
                state.get("forced_stop_reason", ""), state.get("signals") or [],
            )
            started_wall = state.get("started_wall") or time.time()
            ttr_ms = int((time.time() - started_wall) * 1000)
            tta_ms = int(state.get("tta_ms") or 0)
            agent.store.finish_case(
                case["id"], report=report.to_dict(), tta_ms=tta_ms, ttr_ms=ttr_ms
            )
            report_dict = report.to_dict()
            report_dict.pop("raw", None)  # raw 仅供落库审计，不下发 SSE 前端

            # 行动阶段（对齐旧引擎 ⑤）：恢复重放前先查重，防自动动作重复执行
            mode = _execution_mode()
            policy = get_risk_policy()  # 恢复即重读当前策略（30s 内缓存与旧行为一致）
            recs = [r for r in report.recommendations if isinstance(r, dict) and r.get("action")]
            deny_hit = any(kw in str(r["action"]) for r in recs for kw in policy["l2_action_denylist"])
            if mode != "auto" or deny_hit:
                auto_recs, pending = [], recs
            else:
                auto_recs, pending = agent._split_auto(report, recs, policy)
            approvals: list[dict] = []
            if pending:
                # 防重放：恢复重跑行动阶段时，同文本审批已存在则不再重复建单
                already = {a.get("action", "") for a in agent.store.list_approvals(case["id"])}
                fresh_pending = [r for r in pending if str(r.get("action", "")) not in already]
                if fresh_pending:
                    approvals = agent.store.create_approvals(case["id"], case["case_no"], fresh_pending)
                    notify_approval_created(approvals, case.get("case_no", ""))
            l2_actions: list[dict] = []
            l1_actions: list[dict] = []
            if auto_recs and report.conclusion in L2_SEVERITY_SET:
                fresh = _fresh_recs(agent.store, case["id"], "L2:auto", auto_recs)
                if fresh:
                    l2_actions = await agent._l2_autopilot(case["id"], case, report, fresh)
            elif auto_recs:
                fresh = _fresh_recs(agent.store, case["id"], "L1:auto", auto_recs)
                if fresh:
                    l1_actions = agent._l1_autopass(case["id"], case, report, fresh)
            return {
                "report": report_dict,
                "route": "done",
                "case_id": case["id"],
                "tta_ms": tta_ms,
                "ttr_ms": ttr_ms,
                "steps_len": len(obj_steps),
                "signals_len": len(state.get("signals") or []),
                "approvals": approvals,
                "l1_actions": l1_actions,
                "l2_actions": l2_actions,
                "act_mode": mode,
                "act_auto": len(auto_recs),
                "act_pending": len(pending),
            }

        builder = StateGraph(GraphState)
        builder.add_node("triage", node_triage)
        builder.add_node("react", node_react_step)
        builder.add_node("supplement", node_supplement)
        builder.add_node("correlate", node_correlate)
        builder.add_node("report_act", node_report_act)
        builder.add_edge(START, "triage")
        builder.add_conditional_edges(
            "triage",
            lambda s: END if s.get("insufficient") else "react",
            {"react": "react", END: END},
        )
        builder.add_conditional_edges(
            "react", route_react, {"react": "react", "wrap": "supplement"}
        )
        builder.add_edge("supplement", "correlate")
        builder.add_edge("correlate", "report_act")
        builder.add_edge("report_act", END)
        checkpointer = await _get_checkpointer()
        self._graph = builder.compile(checkpointer=checkpointer)
        logger.info("[case-graph] 图构建完成（检查点=%s）", "开" if checkpointer else "关")
        return self._graph

    # -- 新案调查 ----------------------------------------------------------
    async def investigate(
        self,
        clue: str,
        *,
        user_id: str = "",
        source: str = "MANUAL",
        alert_id: str = "",
        fingerprint: str = "",
    ) -> AsyncIterator[dict]:
        graph = await self._ensure_graph()
        if graph is None:
            # 依赖缺失：逐字回落旧引擎
            async for ev in self._agent.investigate(
                clue, user_id=user_id, source=source, alert_id=alert_id, fingerprint=fingerprint
            ):
                yield ev
            return
        case_id = next_message_id()  # 线程号=案件号：triage 节点用同一 id 立案
        now = time.time()
        graph_input: GraphState = {
            "clue": clue,
            "alert_id": alert_id,
            "user_id": user_id,
            "source": source,
            "fingerprint": fingerprint,
            "case_id": case_id,
            "steps": [],
            "messages": [],
            "call_counter": {},
            "signals": [],
            "repeat_warned": False,
            "forced_stop_reason": "",
            "started_wall": now,
            "budget_deadline_wall": now + self._agent.max_total_sec,
            "insufficient": False,
        }
        config = {
            "configurable": {"thread_id": case_id},
            "recursion_limit": max(25, self._agent.max_steps * 3 + 10),
        }
        async for ev in self._stream(graph, config, graph_input, resume=False, step_offset=0):
            yield ev

    # -- 中断续跑 ----------------------------------------------------------
    async def resume(self, case_id: str, *, user_id: str = "") -> AsyncIterator[dict]:
        graph = await self._ensure_graph()
        if graph is None or getattr(graph, "checkpointer", None) is None:
            yield {"type": "error", "message": "续跑不可用：检查点未启用（图引擎未开或 Postgres 不可达）"}
            return
        case = self._agent.store.get_case(case_id)
        if not case:
            yield {"type": "error", "message": f"案件不存在: {case_id}"}
            return
        if case.get("status") != "INVESTIGATING" or case.get("report"):
            yield {"type": "error", "message": "案件不在调查中，无需续跑"}
            return
        lock = self._locks.setdefault(case_id, asyncio.Lock())
        if lock.locked():
            yield {"type": "error", "message": "该案件正在续跑，请勿重复操作"}
            return
        async with lock:  # 条件抢占：同一案件同时只允许一个续跑驱动
            config = {
                "configurable": {"thread_id": case_id},
                "recursion_limit": max(25, self._agent.max_steps * 3 + 10),
            }
            try:
                snap = await graph.aget_state(config)
            except Exception as e:  # noqa: BLE001
                logger.exception("[case-graph] 读取检查点失败 case=%s", case_id)
                yield {"type": "error", "message": f"读取检查点失败: {type(e).__name__}: {e}"}
                return
            values = (snap.values if snap else {}) or {}
            if not values.get("case_id"):
                yield {"type": "error", "message": "没有可恢复的检查点（该案并非图引擎调查或存档为空）"}
                return
            try:
                # 预算按本次续跑重新计时（墙钟不跨进程持久）
                await graph.aupdate_state(
                    config, {"budget_deadline_wall": time.time() + self._agent.max_total_sec}
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("[case-graph] 续跑预算重置失败 case=%s", case_id)
                yield {"type": "error", "message": f"续跑准备失败: {type(e).__name__}: {e}"}
                return
            prior_steps = values.get("steps") or []
            # 回放已完成步骤事件（回放不重算）：前端续上之前的取证轨迹
            for d in prior_steps:
                yield {"type": "step", **d, "replayed": True}
            async for ev in self._stream(
                graph, config, None, resume=True, step_offset=len(prior_steps)
            ):
                yield ev

    # -- 事件序列（与旧引擎逐字对齐的线性编排） --------------------------------
    async def _stream(
        self,
        graph,
        config: dict,
        graph_input: dict | None,
        *,
        resume: bool,
        step_offset: int,
    ) -> AsyncIterator[dict]:
        agent = self._agent
        emitted = step_offset
        prelude_done = resume  # 续跑不再补发 case.created 之前的前置事件
        seen: set[str] = set()  # 已产出增量信息的节点：用于续跑从中间节点进入时补齐缺口事件
        marks: dict[str, float] = {}

        def _emit_enrich_done(steps_len: int):
            yield {
                "type": "stage", "stage": STAGE_ENRICH, "status": "done",
                "ms": int((time.monotonic() - marks.get("enrich", time.monotonic())) * 1000),
                "steps": steps_len,
            }
            yield {
                "type": "phase", "phase": PHASE_INVESTIGATE, "status": "done",
                "steps": steps_len,
            }
            marks["correlate"] = time.monotonic()
            yield {"type": "stage", "stage": STAGE_CORRELATE, "status": "start"}

        def _emit_correlate_done(signals_len: int):
            yield {
                "type": "stage", "stage": STAGE_CORRELATE, "status": "done",
                "ms": int((time.monotonic() - marks.get("correlate", time.monotonic())) * 1000),
                "signals": signals_len,
            }
            marks["recommend"] = time.monotonic()
            yield {"type": "phase", "phase": PHASE_REPORT, "status": "start"}
            yield {"type": "stage", "stage": STAGE_RECOMMEND, "status": "start"}

        try:
            async for chunk in graph.astream(graph_input, config=config, stream_mode="updates"):
                for node, delta in (chunk or {}).items():
                    if not isinstance(delta, dict):
                        continue
                    if node == "triage":
                        triage = delta.get("triage") or {}
                        if not prelude_done:
                            prelude_done = True
                            marks["detect"] = time.monotonic()
                            yield {"type": "phase", "phase": PHASE_TRIAGE, "status": "start"}
                            yield {"type": "stage", "stage": STAGE_DETECT, "status": "start"}
                            yield {
                                "type": "stage", "stage": STAGE_DETECT, "status": "done",
                                "ms": int((time.monotonic() - marks["detect"]) * 1000),
                            }
                            yield {
                                "type": "phase", "phase": PHASE_TRIAGE, "status": "done",
                                "triage": triage,
                            }
                            case = delta.get("case") or {}
                            yield {
                                "type": "case.created",
                                "case_id": case.get("id", delta.get("case_id", "")),
                                "case_no": case.get("case_no", delta.get("case_no", "")),
                                "title": case.get("title", ""),
                            }
                        if delta.get("insufficient"):
                            # 输入不足：与旧引擎一致——跳过的阶段明说，报告给可操作提示
                            for skipped in (STAGE_ENRICH, STAGE_CORRELATE, STAGE_ACT):
                                yield {"type": "stage", "stage": skipped, "status": "skipped"}
                            yield {"type": "stage", "stage": STAGE_RECOMMEND, "status": "start"}
                            yield {"type": "stage", "stage": STAGE_RECOMMEND, "status": "done", "ms": 0}
                            yield {
                                "type": "phase", "phase": PHASE_INVESTIGATE, "status": "skipped",
                                "reason": (delta.get("report") or {}).get("reason_code", ""),
                            }
                            yield {"type": "phase", "phase": PHASE_REPORT, "status": "done"}
                            yield {"type": "report", "report": delta.get("report") or {}}
                            return
                        if not resume:
                            yield {"type": "phase", "phase": PHASE_INVESTIGATE, "status": "start"}
                            marks["enrich"] = time.monotonic()
                            yield {"type": "stage", "stage": STAGE_ENRICH, "status": "start"}
                    elif node == "react":
                        seen.add("react")
                        steps = delta.get("steps") or []
                        for s in steps[emitted:]:
                            emitted += 1
                            yield {"type": "step", **s}
                    elif node == "supplement":
                        seen.add("supplement")
                        steps = delta.get("steps") or []
                        for s in steps[emitted:]:
                            emitted += 1
                            yield {"type": "step", **s}
                        for ev in _emit_enrich_done(len(steps)):
                            yield ev
                    elif node == "correlate":
                        seen.add("correlate")
                        if "supplement" not in seen:
                            # 续跑从关联节点直接进入：补齐取证阶段收尾事件
                            for ev in _emit_enrich_done(emitted):
                                yield ev
                        for ev in _emit_correlate_done(len(delta.get("signals") or [])):
                            yield ev
                    elif node == "report_act":
                        seen.add("report_act")
                        if "supplement" not in seen:
                            for ev in _emit_enrich_done(int(delta.get("steps_len") or emitted)):
                                yield ev
                        if "correlate" not in seen:
                            for ev in _emit_correlate_done(int(delta.get("signals_len") or 0)):
                                yield ev
                        yield {
                            "type": "stage", "stage": STAGE_RECOMMEND, "status": "done",
                            "ms": int((time.monotonic() - marks.get("recommend", time.monotonic())) * 1000),
                        }
                        yield {"type": "phase", "phase": PHASE_REPORT, "status": "done"}
                        yield {"type": "report", "report": delta.get("report") or {}}
                        marks["act"] = time.monotonic()
                        yield {"type": "stage", "stage": STAGE_ACT, "status": "start"}
                        approvals = delta.get("approvals") or []
                        l2_actions = delta.get("l2_actions") or []
                        l1_actions = delta.get("l1_actions") or []
                        if approvals:
                            yield {"type": "approval.created", "approvals": approvals}
                        if l2_actions:
                            yield {"type": "l2.autopilot", "actions": l2_actions}
                        elif l1_actions:
                            yield {"type": "l1.autopass", "actions": l1_actions}
                        yield {
                            "type": "stage", "stage": STAGE_ACT, "status": "done",
                            "ms": int((time.monotonic() - marks["act"]) * 1000),
                            "mode": delta.get("act_mode", ""),
                            "auto": int(delta.get("act_auto") or 0),
                            "pending": int(delta.get("act_pending") or 0),
                        }
                        yield {
                            "type": "end",
                            "case_id": delta.get("case_id", ""),
                            "status": "REPORTED",
                            "tta_ms": int(delta.get("tta_ms") or 0),
                            "ttr_ms": int(delta.get("ttr_ms") or 0),
                            "steps": int(delta.get("steps_len") or 0),
                            "forced_stop_reason": delta.get("forced_stop_reason", ""),
                        }
                        return
        except Exception as e:  # noqa: BLE001
            logger.exception("[case-graph] 调查链路异常（案件保持调查中，可续跑）")
            yield {"type": "error", "message": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _steps_obj(d: dict):
    from core.case.types import EvidenceStep

    return _obj_from_dict(EvidenceStep, d)


def _triage_obj(d: dict) -> TriageResult:
    return _obj_from_dict(TriageResult, d)


def _fresh_recs(store, case_id: str, operator: str, recs: list[dict]) -> list[dict]:
    """防重放过滤：续跑重放时跳过已执行过的同文本自动动作。"""
    done = store.existing_auto_action_keys(case_id, operator)
    return [r for r in recs if str(r.get("action", "")) not in done]
