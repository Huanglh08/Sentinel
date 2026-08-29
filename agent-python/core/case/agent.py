"""CaseInvestigationAgent —— 「监督式调查 Agent」核心（Phase 1）。

三段式状态机（对齐 HolmesGPT / Google Alert Triage / Dropzone 范式）:

  ① Triage（Orchestrator 角色, 快模型）: 线索 -> 结构化分诊（5W1H/主体/优先级/调查计划/SOP 匹配）
  ② Investigate（Investigator 角色, 强模型）: 受控 ReAct 循环
       while steps < max_steps: 推理 -> 选工具 -> 执行 -> 观察 -> 更新假设
     四道护栏:
       a) 工具白名单（catalog 之外的 tool_id 直接拒绝并纠偏）
       b) 重复调用熔断（同 tool+args 第二次重复注入警告, 第三次强制收尾）
       c) 观察结果截断（超长结果保留头尾, 防上下文爆炸）
       d) 总步数/总时长预算（超限强制进入 Reporter, 报告标记 inconclusive）
  ③ Report（Reporter 角色, 强模型）: 证据链 -> 结构化 JSON 报告（结论/置信度/引用/处置建议）

每一步均产出 EvidenceStep（外部化记忆/证据链）并 yield SSE 事件。
工具执行复用既有 MCPToolClient（白名单/提参/降级底座零改造）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import Counter
from typing import AsyncIterator

from core.case.skills import SkillLoader
from core.case.store import CaseStore
from core.case.notify import notify_approval_created
from core.case.types import (
    PHASE_INVESTIGATE,
    PHASE_REPORT,
    PHASE_TRIAGE,
    SEVERITY_INCONCLUSIVE,
    EvidenceStep,
    InvestigationReport,
    TriageResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 工具白名单（Phase 2）：启动/调查时从 MCP server 动态拉取 tools/list 构建，
# 下方 TOOL_CATALOG 仅作为 MCP 不可达时的静态兜底（保证链路永不被工具目录卡死）。
# 写动作属 Phase 3 Responder, 此处一律只读。
# 注意: weather_query 等无关工具不暴露 —— 工具权限按场景分配（EXCLUDED_TOOLS）。
# ---------------------------------------------------------------------------
EXCLUDED_TOOLS: set[str] = {"weather_query"}

TOOL_CATALOG: list[dict] = [
    {
        "tool_id": "ticket_query",
        "layer": "数据查询",
        "description": "查询风控工单/事件记录，按地区、状态、优先级、产品、客户名等条件检索历史事件",
        "args": {
            "region": "地区(可选)",
            "status": "状态(可选, 如 open/closed)",
            "priority": "优先级(可选)",
            "product": "产品线(可选)",
            "customer_name": "客户名(可选)",
            "query_type": "查询类型(可选)",
            "limit": "返回条数(可选)",
        },
    },
    {
        "tool_id": "sales_query",
        "layer": "数据查询",
        "description": "查询业务数据（销量/交易）统计，用于核对异常波动与业务基线",
        "args": {
            "region": "地区(可选)",
            "period": "时间范围(可选)",
            "product": "产品线(可选)",
            "sales_person": "销售(可选)",
            "query_type": "查询类型(可选)",
            "limit": "返回条数(可选)",
        },
    },
    {
        "tool_id": "youcom_search",
        "layer": "情报富化",
        "description": "外部威胁情报/公开网络检索，查黑产手法、IOC、同类案例",
        "args": {
            "query": "检索词(必填)",
            "count": "返回条数(可选)",
            "freshness": "时效(可选, 如 oneMonth)",
        },
    },
]


def _tool_catalog_text(catalog: list[dict]) -> str:
    lines = []
    for t in catalog:
        args_desc = "; ".join(f"{k}={v}" for k, v in t["args"].items())
        lines.append(f"- {t['tool_id']}（{t['layer']}）: {t['description']}。参数: {args_desc}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompt（Phase 1 常量; Phase 2 迁 t_agent_prompt 槽位做 GitOps）
# ---------------------------------------------------------------------------
_TRIAGE_PROMPT = """你是风控分诊专家（Orchestrator）。对下面这条风控线索做快速分诊，只输出一个 JSON 对象（禁止 markdown 代码块）：
{
  "title": "案件标题（<=30字）",
  "subject_type": "account|device|ip|order|merchant|other 之一",
  "subject_id": "从线索中提取的主体标识（账号/设备号/IP/订单号），提取不到给空串",
  "priority": "P0|P1|P2|P3",
  "severity_guess": "benign|suspicious|malicious",
  "key_questions": ["本案需要查证的关键问题1", "问题2"],
  "skill": "与下方技能目录最匹配的技能名，没有则空串",
  "plan": ["调查第1步（指明用什么工具查什么）", "第2步"]
}

可用技能目录:
__SKILL_CATALOG__

线索:
__CLUE__"""

_INVESTIGATOR_SYSTEM = """你是资深风控调查员（Investigator）。你正在对一起案件做多步自主调查。

可用工具（白名单，只能用这些）:
__TOOL_CATALOG__

调查规则:
1. 每轮只输出一个 JSON 对象（禁止 markdown 代码块、禁止多余文字），二选一:
   调用工具: {"thought": "本步推理（简短）", "action": "tool", "tool_id": "工具名", "args": {...}}
   结束调查: {"thought": "收尾推理", "action": "finish", "conclusion": "阶段性结论（简短）"}
2. args 按参数说明从已知信息构造，禁止编造线索中不存在的 ID/数值。
3. 沿调查计划/SOP 推进；同一工具同一参数严禁重复调用；某工具连续失败 2 次就换路径。
4. 证据足以回答分诊阶段的 key_questions 时立即 finish，不浪费步数。
5. 若上下文提供了 SOP 技能正文，按其步骤顺序执行，并在 thought 中标注当前 SOP 步骤。
6. tool_id 必须逐字取自上方「可用工具」目录，禁止编造目录外工具名；某工具调用失败后禁止用相同 tool_id+args 重试，换参数或换工具；若目录内工具均无法推进调查，立即 finish 并在 conclusion 中说明缺失的工具/数据。"""

_REPORTER_PROMPT = """你是风控调查报告撰写人（Reporter）。基于案件线索、分诊结论与全部调查记录，输出严格的 JSON 调查报告（禁止 markdown 代码块）:
{
  "conclusion": "benign|suspicious|malicious|inconclusive 之一",
  "confidence": 0.0到1.0,
  "summary": "结论摘要（3-5句，说清发生了什么、判断依据）",
  "key_findings": [{"finding": "关键发现", "evidence_step": 引用的调查步号int}],
  "recommendations": [{"action": "建议动作（如: 放行/观察/冻结账户/升级人工）", "requires_human_approval": true|false, "reason": "理由"}],
  "knowledge_distillation": "本案可沉淀到知识库的经验手法（1-3句，没有给空串）"
}

评分纪律: confidence 必须与证据强度匹配；只有工具证据支撑的发现才能进 key_findings；无证据支撑的猜测 confidence 不得超过 0.3；处置建议默认 requires_human_approval=true。

线索: __CLUE__

分诊结论: __TRIAGE__

调查记录（逐步）:
__STEPS__"""


def _extract_json(text: str) -> dict | None:
    """从 LLM 输出中稳健提取第一个 JSON 对象（容忍 ```json 围栏与前后杂文字）。"""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    start = cleaned.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(cleaned)):
        if cleaned[i] == "{":
            depth += 1
        elif cleaned[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(cleaned[start : i + 1])
                    return obj if isinstance(obj, dict) else None
                except Exception:  # noqa: BLE001
                    return None
    return None


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars * 2 // 3
    tail = max_chars - head
    return f"{text[:head]}\n...[观察结果过长，中段已截断，原文 {len(text)} 字符]...\n{text[-tail:]}"


def _digest_steps(steps: list[EvidenceStep]) -> str:
    lines = []
    for s in steps:
        tool = f"[{s.tool_id}({json.dumps(s.tool_args, ensure_ascii=False)})]" if s.tool_id else "[推理]"
        flag = "❌" if s.is_error else "✅"
        obs = (s.observation or "").replace("\n", " ")
        lines.append(f"第{s.step_no}步 {flag} {tool} thought: {s.thought} | 观察: {obs[:400]}")
    return "\n".join(lines) if lines else "（无）"


def _report_prompt(clue: str, triage: TriageResult, steps: list[EvidenceStep]) -> str:
    return (
        _REPORTER_PROMPT.replace("__CLUE__", clue)
        .replace("__TRIAGE__", json.dumps(triage.to_dict(), ensure_ascii=False))
        .replace("__STEPS__", _digest_steps(steps))
    )


class CaseInvestigationAgent:
    """三段式案件调查 Agent。llm/mcp 复用主 Agent 的实例（共享路由/降级底座）。"""

    def __init__(
        self,
        llm,
        mcp,
        store: CaseStore | None = None,
        skills: SkillLoader | None = None,
        *,
        max_steps: int = 10,
        max_total_sec: float = 240.0,
        observation_max_chars: int = 4000,
    ) -> None:
        self.llm = llm
        self.mcp = mcp
        self.store = store or CaseStore()
        self.skills = skills or SkillLoader()
        self.max_steps = max_steps
        self.max_total_sec = max_total_sec
        self.observation_max_chars = observation_max_chars
        self._catalog_cache: tuple[float, list[dict]] | None = None
        self._catalog_ttl_sec = 300.0

    # ------------------------------------------------------------------
    # 动态工具目录：MCP tools/list 拉取（带 TTL 缓存），失败降级静态清单
    # ------------------------------------------------------------------
    async def _load_catalog(self) -> list[dict]:
        now = time.monotonic()
        if self._catalog_cache is not None and now - self._catalog_cache[0] < self._catalog_ttl_sec:
            return self._catalog_cache[1]
        catalog: list[dict] = []
        try:
            tools = await self.mcp.list_tools()
            for t in tools:
                name = str(t.get("name", "")).strip()
                if not name or name in EXCLUDED_TOOLS:
                    continue
                schema = t.get("inputSchema") or {}
                props = schema.get("properties") or {}
                required = set(schema.get("required") or [])
                args: dict[str, str] = {}
                for k, v in props.items():
                    hint = ""
                    if isinstance(v, dict):
                        hint = str(v.get("description", "") or "")
                    mark = "必填" if k in required else "可选"
                    args[k] = f"{hint}({mark})" if hint else mark
                catalog.append(
                    {
                        "tool_id": name,
                        "layer": "MCP 动态",
                        "description": str(t.get("description", "") or "（服务端未提供描述）"),
                        "args": args or {"(无参数)": "直接调用"},
                    }
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("[case] 动态工具目录拉取失败，降级静态白名单: %s", e)
        if not catalog:
            catalog = TOOL_CATALOG
        self._catalog_cache = (now, catalog)
        logger.info(
            "[case] 工具目录就绪: %d 个工具（%s）",
            len(catalog),
            "动态" if catalog is not TOOL_CATALOG else "静态兜底",
        )
        return catalog

    # ------------------------------------------------------------------
    # 主入口：异步生成器，逐步 yield 事件，最终事件携带完整报告
    # ------------------------------------------------------------------
    async def investigate(
        self,
        clue: str,
        user_id: str = "",
        source: str = "MANUAL",
    ) -> AsyncIterator[dict]:
        started = time.monotonic()
        try:
            # ---------- ① Triage ----------
            yield {"type": "phase", "phase": PHASE_TRIAGE, "status": "start"}
            triage, triage_steps = await self._triage(clue)
            yield {"type": "phase", "phase": PHASE_TRIAGE, "status": "done", "triage": triage.to_dict()}

            case = self.store.create_case(
                title=triage.title or (clue[:30] if clue else "未命名案件"),
                raw_input=clue,
                clue_summary="；".join(triage.key_questions) or clue[:200],
                triage=triage.to_dict(),
                user_id=user_id,
                source=source,
            )
            case_id = case["id"]
            yield {"type": "case.created", "case_id": case_id, "case_no": case["case_no"], "title": case["title"]}
            tta_ms = int((time.monotonic() - started) * 1000)

            # ---------- ② Investigate ----------
            yield {"type": "phase", "phase": PHASE_INVESTIGATE, "status": "start"}
            steps, forced_stop_reason = await self._investigate(clue, triage)
            for s in steps:
                self.store.append_evidence(case_id, s)
                yield {"type": "step", **s.to_dict()}
            yield {"type": "phase", "phase": PHASE_INVESTIGATE, "status": "done", "steps": len(steps)}

            # ---------- ③ Report ----------
            yield {"type": "phase", "phase": PHASE_REPORT, "status": "start"}
            report = await self._report(clue, triage, steps, forced_stop_reason)
            ttr_ms = int((time.monotonic() - started) * 1000)
            self.store.finish_case(case_id, report=report.to_dict(), tta_ms=tta_ms, ttr_ms=ttr_ms)
            yield {"type": "phase", "phase": PHASE_REPORT, "status": "done"}
            report_dict = report.to_dict()
            report_dict.pop("raw", None)  # raw 仅供落库审计，不下发 SSE 前端
            yield {"type": "report", "report": report_dict}

            # ---------- ④ 审批闭环：需人工审批的处置建议自动落待办 ----------
            pending = [
                r
                for r in report.recommendations
                if isinstance(r, dict) and r.get("requires_human_approval")
            ]
            if pending:
                approvals = self.store.create_approvals(case_id, case["case_no"], pending)
                notify_approval_created(approvals, case.get("case_no", ""))
                yield {"type": "approval.created", "approvals": approvals}

            yield {
                "type": "end",
                "case_id": case_id,
                "status": "REPORTED",
                "tta_ms": tta_ms,
                "ttr_ms": ttr_ms,
                "steps": len(steps),
                "forced_stop_reason": forced_stop_reason or "",
            }
        except Exception as e:  # noqa: BLE001
            logger.exception("[case] 调查链路异常")
            yield {"type": "error", "message": f"{type(e).__name__}: {e}"}

    # ------------------------------------------------------------------
    # ① Triage（快，1 次 LLM 调用；失败降级为默认分诊，不阻塞调查）
    # ------------------------------------------------------------------
    async def _triage(self, clue: str) -> tuple[TriageResult, list[EvidenceStep]]:
        t = EvidenceStep(step_no=0, phase=PHASE_TRIAGE, tool_id="llm.triage")
        started = time.monotonic()
        raw = ""
        try:
            raw = await self.llm.chat(
                [
                    {
                        "role": "user",
                        "content": _TRIAGE_PROMPT.replace(
                            "__SKILL_CATALOG__", self.skills.catalog_text()
                        ).replace("__CLUE__", clue),
                    }
                ],
                max_tokens=500,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[case] 分诊调用失败，使用默认分诊: %s", e)
            t.is_error = True
        t.observation = raw[:2000]
        t.elapsed_ms = int((time.monotonic() - started) * 1000)
        data = _extract_json(raw) or {}
        triage = TriageResult(
            title=str(data.get("title", ""))[:60],
            subject_type=str(data.get("subject_type", "other")),
            subject_id=str(data.get("subject_id", "")),
            priority=str(data.get("priority", "P2")),
            severity_guess=str(data.get("severity_guess", "suspicious")),
            key_questions=[str(q) for q in (data.get("key_questions") or [])][:6],
            skill=str(data.get("skill", "")),
            plan=[str(p) for p in (data.get("plan") or [])][:8],
        )
        return triage, [t]

    # ------------------------------------------------------------------
    # ② Investigate：受控 ReAct 循环 + 四道护栏
    # ------------------------------------------------------------------
    async def _investigate(
        self, clue: str, triage: TriageResult
    ) -> tuple[list[EvidenceStep], str]:
        # SOP 全文按需注入（分诊匹配到的技能）
        sop_text = ""
        skill = self.skills.get(triage.skill)
        if skill:
            sop_text = f"\n\n已匹配 SOP 技能《{skill.name}》，请按其步骤顺序调查:\n{skill.body}"

        catalog = await self._load_catalog()
        allowed = {t["tool_id"] for t in catalog}
        system = _INVESTIGATOR_SYSTEM.replace("__TOOL_CATALOG__", _tool_catalog_text(catalog))
        context = (
            f"案件线索:\n{clue}\n\n分诊结论:\n{json.dumps(triage.to_dict(), ensure_ascii=False)}"
            f"{sop_text}\n\n请开始第 1 步调查。"
        )
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": context},
        ]

        steps: list[EvidenceStep] = []
        call_counter: Counter[str] = Counter()
        repeat_warned = False
        forced_stop_reason = ""
        deadline = time.monotonic() + self.max_total_sec

        for step_no in range(1, self.max_steps + 1):
            if time.monotonic() > deadline:
                forced_stop_reason = "总时长超限"
                break
            raw = await self.llm.chat(messages, max_tokens=600)
            action = _extract_json(raw)
            if action is None:
                step = EvidenceStep(
                    step_no=step_no,
                    phase=PHASE_INVESTIGATE,
                    thought="输出不是合法 JSON",
                    observation=raw[:800],
                    is_error=True,
                )
                steps.append(step)
                messages.append({"role": "assistant", "content": raw[:1500]})
                messages.append(
                    {
                        "role": "user",
                        "content": "输出格式错误：必须是单个 JSON 对象（action=tool 或 finish）。请重新输出。",
                    }
                )
                continue

            thought = str(action.get("thought", ""))
            act = str(action.get("action", "tool"))

            if act == "finish":
                steps.append(
                    EvidenceStep(
                        step_no=step_no,
                        phase=PHASE_INVESTIGATE,
                        thought=thought,
                        tool_id="llm.finish",
                        observation=str(action.get("conclusion", "")),
                    )
                )
                break

            tool_id = str(action.get("tool_id", "")).strip()
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            step = EvidenceStep(
                step_no=step_no, phase=PHASE_INVESTIGATE, thought=thought, tool_id=tool_id, tool_args=args
            )
            started = time.monotonic()

            # 护栏 a: 白名单（动态目录）
            if tool_id not in allowed:
                step.is_error = True
                step.observation = f"[护栏] 工具 {tool_id} 不在白名单内，可用工具: {', '.join(sorted(allowed))}"
            else:
                # 护栏 b: 重复调用熔断
                key = tool_id + "|" + hashlib.md5(
                    json.dumps(args, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()
                call_counter[key] += 1
                if call_counter[key] >= 3:
                    forced_stop_reason = f"重复调用熔断（{tool_id} 同参数已调用 {call_counter[key]} 次）"
                    step.is_error = True
                    step.observation = f"[护栏] {forced_stop_reason}，强制收尾"
                    steps.append(step)
                    break
                if call_counter[key] == 2 and not repeat_warned:
                    repeat_warned = True
                try:
                    is_error, text = await self.mcp.call(tool_id, args)
                    step.is_error = is_error
                    # 护栏 c: 观察截断
                    step.observation = _truncate(text, self.observation_max_chars)
                except Exception as e:  # noqa: BLE001
                    step.is_error = True
                    step.observation = f"[异常] {type(e).__name__}: {e}"
            step.elapsed_ms = int((time.monotonic() - started) * 1000)
            steps.append(step)
            messages.append({"role": "assistant", "content": raw[:1500]})
            messages.append({"role": "user", "content": f"第 {step_no} 步观察结果:\n{step.observation}"})
        else:
            forced_stop_reason = forced_stop_reason or f"步数达到上限 {self.max_steps}"

        return steps, forced_stop_reason

    # ------------------------------------------------------------------
    # ③ Report：结构化报告 + 一次纠偏重试 + 兜底报告
    # ------------------------------------------------------------------
    async def _report(
        self,
        clue: str,
        triage: TriageResult,
        steps: list[EvidenceStep],
        forced_stop_reason: str,
    ) -> InvestigationReport:
        base = _report_prompt(clue, triage, steps)
        if forced_stop_reason:
            base += f"\n\n注意: 调查被强制中断（{forced_stop_reason}），若证据不足请给 conclusion=inconclusive。"

        raw = ""
        for attempt in range(2):
            try:
                raw = await self.llm.chat([{"role": "user", "content": base}], max_tokens=900)
            except Exception as e:  # noqa: BLE001
                logger.warning("[case] 报告调用失败: %s", e)
                break
            data = _extract_json(raw)
            if data is not None:
                return self._build_report(data, raw)
            base = (
                _report_prompt(clue, triage, steps)
                + f"\n\n你上次的输出不是合法 JSON: {raw[:300]}\n请严格只输出 JSON。"
            )

        # 兜底报告：从已有 thought 聚合，保证链路不因 LLM 输出异常而中断
        thoughts = [s.thought for s in steps if s.thought and not s.is_error]
        return InvestigationReport(
            conclusion=SEVERITY_INCONCLUSIVE,
            confidence=0.05,
            summary="报告生成失败（LLM 输出无法解析），以下为调查过程摘要自动兜底。",
            key_findings=[{"finding": t[:120], "evidence_step": i + 1} for i, t in enumerate(thoughts[:3])],
            recommendations=[
                {
                    "action": "升级人工",
                    "requires_human_approval": True,
                    "reason": "自动报告生成失败，需人工查看证据链",
                }
            ],
            raw=raw[:2000],
        )

    @staticmethod
    def _build_report(data: dict, raw: str) -> InvestigationReport:
        allowed_conclusions = {"benign", "suspicious", "malicious", "inconclusive"}
        conclusion = str(data.get("conclusion", SEVERITY_INCONCLUSIVE)).lower()
        if conclusion not in allowed_conclusions:
            conclusion = SEVERITY_INCONCLUSIVE
        try:
            confidence = min(1.0, max(0.0, float(data.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        findings = []
        for f in (data.get("key_findings") or [])[:8]:
            if isinstance(f, dict) and f.get("finding"):
                findings.append(
                    {"finding": str(f["finding"]), "evidence_step": int(f.get("evidence_step", 0) or 0)}
                )
        recs = []
        for r in (data.get("recommendations") or [])[:5]:
            if isinstance(r, dict) and r.get("action"):
                recs.append(
                    {
                        "action": str(r["action"]),
                        "requires_human_approval": bool(r.get("requires_human_approval", True)),
                        "reason": str(r.get("reason", "")),
                    }
                )
        return InvestigationReport(
            conclusion=conclusion,
            confidence=confidence,
            summary=str(data.get("summary", "")),
            key_findings=findings,
            recommendations=recs
            or [{"action": "升级人工", "requires_human_approval": True, "reason": "报告未给出处置建议"}],
            knowledge_distillation=str(data.get("knowledge_distillation", "")),
            raw=raw[:2000],
        )
