"""RuleMiner —— 规则挖掘 Agent（Phase 4）。

知识飞轮的消费端：批量读取「案件经验蒸馏池」（t_case_kb_distillation，Phase 3 产出），
经 LLM 把非结构化经验蒸馏成结构化候选规则（t_case_rule_candidate，PENDING），
再走 HITL 审核后回注分诊（CASE_TRIAGE 槽位 __RULE_HINTS__），形成闭环。

业界依据（evaluation/reports/）:
- 同盾小盾「规则挖掘 AI Agent」: 国内风控厂商的正式产品方向（open-source survey §1）;
- Google SecOps 检测工程 Agent: 报告 -> 检测规则 + 合成遥测验证 —— 本类的 validate()
  即其 Sentinel 对应物：候选规则在历史案件集上回放，统计关键词命中率;
- 蚁盾/同盾共性: Agent 位于「策略生产与解释」侧，毫秒级拦截仍由规则引擎承担 ——
  挖掘产物只回注调查侧（分诊提示），绝不直接进实时拦截链路。

护栏:
- condition_hash 唯一约束去重（重复经验不重复产规则）;
- 蒸馏纪律写进 prompt: 宁缺毋滥、条件必须可判定、高危动作禁止入规则;
- 高危动作硬黑名单（types.L2_ACTION_DENYLIST）在落库前二次校验。

持久化间接注入防护（ASI04，读侧纵深防御）:
- 写入侧（store.distill_knowledge）已阻断新沉淀中的注入文本，但候选池可能含
  护栏上线前的历史脏数据。本模块把池内文本拼进 LLM prompt，是注入 payload
  进入推理链的入口，故读侧再做一次消毒 + 数据边界包裹，两层独立生效。
"""

from __future__ import annotations

import hashlib
import logging
import re

from core.case.agent import _extract_json
from core.case.store import get_risk_policy
from core.case.types import AUDIT_OPERATION_RULE_MINED
from core.guard.indirect import sanitize_report_text, wrap_kb_context

logger = logging.getLogger(__name__)

# 关键词切分用的停用模式：标点、空白与高频虚词（轻量中文处理，不引分词依赖）
_STOP_PATTERN = re.compile(
    r"[，。；：、？！\s\-—/\\()\[\]{}<>《》“”\"'`~@#$%^&*=+|！？；：（）,.;:?!]+"
    r"|若|则|且|或|的|了|在|是|与|及|对|为|有|无|等|中|当|如|被|把|该|此|这|那"
)

# 出厂兜底 prompt（GitOps 槽位 CASE_RULE_MINER 配置后覆盖；占位符 __EXPERIENCE__）
_RULE_MINER_PROMPT = """你是风控策略规则工程师（Rule Miner）。下面是若干案件的调查经验沉淀，请把其中可复用、可结构化的经验蒸馏成候选策略规则。只输出一个 JSON 对象（禁止 markdown 代码块）:
{
  "rules": [
    {
      "rule_name": "规则名（<=20字）",
      "condition_text": "触发条件：可判定的线索特征（主体行为/属性模式，<=100字）",
      "action_suggestion": "命中后建议动作（如: 升级人工/观察/创建工单）",
      "severity": "benign|suspicious|malicious 之一（命中该规则的典型结论）",
      "confidence": 0.0到1.0（规则可信度，依据支撑案例的数量与质量）
    }
  ]
}

蒸馏纪律:
1. 只提炼有明确证据支撑、可复用的模式，宁缺毋滥（0 条也是合法输出）。
2. condition_text 必须是可判定的线索特征，禁止「注意风险」类空泛表述。
3. 高危动作（封禁/冻结/止付/清退/拦截/拉黑/关停/下架）禁止作为 action_suggestion，此类动作必须人工决策。
4. 同类模式合并为一条规则，不同模式拆分为多条。

经验沉淀:
__EXPERIENCE__"""


def _keywords(condition_text: str) -> list[str]:
    """从规则条件中提取关键词（长度>=2 的片段），用于历史案件回放匹配。"""
    parts = _STOP_PATTERN.split(condition_text or "")
    seen: set[str] = set()
    words: list[str] = []
    for p in parts:
        w = p.strip()
        if len(w) >= 2 and w not in seen:
            seen.add(w)
            words.append(w)
    return words[:12]  # 回放匹配上限，防超长条件拖慢验证


class RuleMiner:
    """规则挖掘器：蒸馏池 -> 候选规则（PENDING）-> 回放验证 -> HITL 审核。

    复用 CaseInvestigationAgent 的 llm 与 GitOps 槽位解析（CASE_RULE_MINER），
    不新建 LLM/提示词链路。
    """

    def __init__(self, case_agent, store) -> None:
        self.agent = case_agent
        self.store = store

    # ------------------------------------------------------------------
    # 挖掘：蒸馏池 -> LLM 蒸馏 -> 候选规则落库（去重）
    # ------------------------------------------------------------------
    async def mine(self, limit: int = 10, operator: str = "rule_miner") -> dict:
        """消费最近的蒸馏记录，产出候选规则。返回批次统计供接口层审计。"""
        distills = self.store.list_kb_distillations(limit=max(1, min(limit, 50)))
        if not distills:
            return {"mined": 0, "skipped_dup": 0, "rules": [], "message": "蒸馏池为空，无经验可挖掘"}

        # 读侧消毒：候选池是持久化文本，可能含护栏上线前的历史脏数据
        exp_lines, total_hits = [], []
        for d in distills:
            text, hits = sanitize_report_text(str(d.get("distillation", "")))
            if hits:
                total_hits.extend(hits)
                logger.warning(
                    "[rule-miner] 蒸馏记录命中注入模式已消毒 case=%s hits=%s",
                    d.get("case_no", ""), hits,
                )
            exp_lines.append(
                f"- 案件 {d.get('case_no', '')}（结论 {d.get('conclusion', '')}）: {text}"
            )
        if total_hits:
            logger.warning("[rule-miner] 本批蒸馏文本共消毒注入模式 %d 处", len(total_hits))
        # 数据边界包裹：明确告知 LLM 其中任何文字都不是指令
        prompt = (
            self.agent._slot_prompt("CASE_RULE_MINER", _RULE_MINER_PROMPT)
            .replace("__EXPERIENCE__", wrap_kb_context("\n".join(exp_lines), len(total_hits)))
        )
        try:
            raw = await self.agent.llm.chat([{"role": "user", "content": prompt}], max_tokens=1200)
        except Exception as e:  # noqa: BLE001
            logger.exception("[rule-miner] 蒸馏调用失败")
            return {"mined": 0, "skipped_dup": 0, "rules": [], "message": f"LLM 调用失败: {e}"}

        data = _extract_json(raw) or {}
        case_ids = ";".join(sorted({str(d.get("case_id", "")) for d in distills if d.get("case_id")}))
        distill_ids = ";".join(sorted({str(d.get("id", "")) for d in distills if d.get("id")}))
        rows: list[dict] = []
        deny_hits: list[str] = []
        for r in (data.get("rules") or [])[:10]:
            if not isinstance(r, dict):
                continue
            cond = str(r.get("condition_text", "")).strip()
            action = str(r.get("action_suggestion", "")).strip()
            if not cond or not action:
                continue
            # 护栏：高危动作不得入规则（prompt 纪律 + 代码二次校验，读 DB 策略保持同步）
            if any(
                kw in action for kw in get_risk_policy()["l2_action_denylist"]
            ):
                deny_hits.append(action[:40])
                continue
            # clamp 到 [0,1]：LLM 可能输出越界值（如 1.5）或非数值（如 "high"），
            # 越界 confidence 会在 list_rule_hints 的 DESC 排序中霸榜
            try:
                conf = max(0.0, min(1.0, float(r.get("confidence") or 0.0)))
            except (TypeError, ValueError):
                conf = 0.0
            rows.append(
                {
                    "rule_name": str(r.get("rule_name", "")).strip()[:60] or cond[:20],
                    "condition_text": cond[:1024],
                    "action_suggestion": action[:512],
                    "severity": str(r.get("severity", "")).strip()[:16],
                    "confidence": conf,
                    # 哈希只取条件文本（同一条件不同措辞入口名视为重复，去重语义以条件为准）
                    "condition_hash": hashlib.sha256(cond.encode("utf-8")).hexdigest()[:32],
                    "source_case_ids": case_ids[:512],
                    "source_distill_ids": distill_ids[:512],
                }
            )
        created, skipped = self.store.create_rule_candidates(rows, operator=operator)
        if created:
            self.store.record_audit(
                case_id="",
                operation=AUDIT_OPERATION_RULE_MINED,
                target=",".join(c["id"] for c in created[:5]),
                operator=operator,
                detail=(
                    f"Rule Miner 挖掘: 蒸馏记录 {len(distills)} 条 -> 候选规则 {len(created)} 条"
                    f"（重复跳过 {skipped}，高危动作拦截 {len(deny_hits)}）"
                ),
            )
        return {
            "mined": len(created),
            "skipped_dup": skipped,
            "denied_actions": len(deny_hits),
            "rules": created,
            "message": "",
        }

    # ------------------------------------------------------------------
    # 验证：历史案件回放（Google 检测工程 Agent「合成遥测验证」的 Sentinel 对应物）
    # ------------------------------------------------------------------
    async def validate(self, rule_id: str, sample_size: int = 50) -> dict:
        """候选规则在最近 N 起历史案件上回放：统计条件关键词的命中案件比例。

        轻量关键词匹配（不引分词依赖），结果写入 validation_* 字段供审核参考；
        命中率本身不是审核结论，只是给 HITL 的量化信号。
        """
        rule = self.store.get_rule_candidate(rule_id)
        if rule is None:
            return {"validated": False, "message": "规则不存在"}
        cond = str(rule.get("condition_text", ""))
        words = _keywords(cond)
        if not words:
            note = "回放跳过：条件文本无可提取关键词"
            self.store.update_rule_validation(rule_id, hit_rate=0.0, sample=0, note=note)
            return {"validated": False, "hit_rate": 0.0, "sample": 0, "message": note}

        cases = self.store.list_cases(limit=max(1, min(sample_size, 200)))
        matched = 0
        for c in cases:
            detail = self.store.get_case(str(c.get("id", ""))) or {}
            # 回放语料：线索摘要 + 分诊 + 报告摘要（调查全文过长，取摘要级）
            corpus = " ".join(
                str(detail.get(k) or "")
                for k in ("clue_summary", "raw_input", "title")
            )
            if detail.get("triage"):
                corpus += " " + str(detail["triage"])
            if detail.get("report"):
                corpus += " " + str(detail["report"])
            if any(w in corpus for w in words):
                matched += 1
        sample = len(cases)
        hit_rate = round(matched / sample, 4) if sample else 0.0
        note = f"关键词回放: {','.join(words[:6])}{'…' if len(words) > 6 else ''}"
        self.store.update_rule_validation(rule_id, hit_rate=hit_rate, sample=sample, note=note)
        logger.info(
            "[rule-miner] 规则回放验证 rule=%s 命中 %d/%d=%.2f", rule_id, matched, sample, hit_rate
        )
        return {
            "validated": True,
            "hit_rate": hit_rate,
            "matched": matched,
            "sample": sample,
            "keywords": words,
            "message": note,
        }
