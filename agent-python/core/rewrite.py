"""查询改写与拆分：对应 ragent-main rag/core/rewrite。

原版逻辑（MultiQuestionRewriteService）：
  - LLM 把用户问题改写为"检索友好查询"，并重构成多个子问题（rq[0] 完整重构，rq[1..] 子问题）
  - LLM 失败 -> 回退原问题 + 规则拆分（按标点/连词）
  - 结果用于"每个子问题单独意图识别 + 检索"

保留语义：
  - 失败必须降级，不能让整条链路崩
  - 改写温度高一点(0.5)利于发散；但首包探测用低温度

v2 增强（multiturn 治理，2026-09-01）：
  - 注入最近对话历史：末轮问题含代词/省略时，改写为不依赖上下文的自包含问题
    （指代消解下沉到检索源头，意图解析/检索/门控全链路受益）
  - 澄清标志搭改写顺风车（零额外 LLM 调用）：问题缺少回答必需要素（订单号/时间/
    场景/真实意图）且历史无法补全时，输出 clarify + 追问话术，上层短路输出
  - 澄清判定仅在历史注入后仍无法补全时触发，完备问题必须 clarify=false
"""
from __future__ import annotations
import logging

import json
import re
from dataclasses import dataclass, field

from core.llm import LLMClient, LLMError

_logger = logging.getLogger(__name__)

PROMPT = """你是一个查询理解与重构助手。请结合对话历史（如有），将用户的输入转换为更利于向量检索的检索查询，并拆分为多个子问题。

要求：
1. 第 1 个查询是对用户问题的完整、准确的语义重构：结合对话历史补全指代词（它/该/这个/上述等）与省略成分，明确实体，改写为不依赖上下文即可理解的自包含问题。无历史或问题本身自包含时，保持原语义仅做检索友好化。检索友好化的做法：去除口语寒暄与无信息量泛词（如"有用户投诉""来咨询""想问一下""问题排查"），但必须保留完整语义与领域术语，输出为精炼的完整语义句，不要碎片化堆砌关键词（反例："用户注册 正确手机号格式 问题排查"是错误示范）。本身就是明确的术语/技术查询（如"TensorFlow LSTM dropout 默认值"）保持原样不要改写。跨语言召回：知识库含英文安全手册（清单见末尾），当场景描述对应清单中某个英文文档的主题（如输入校验→input validation、会话管理/踢下线→session management、密码存储→password storage、数据/模型投毒→data poisoning）时，在第 1 个查询末尾以空格追加该英文主题词；对应中文文档（如交易风控、登录防御）则追加中文主题词，始终追加、不得省略。
2. 第 2~N 个查询是拆解出的独立子问题。拆解判定：问题需要来自不同文档或不同知识域的多方面信息组合才能完整回答（如"某流程怎么走 + 什么情况会被拦"），或含多个并列疑问点时，必须拆成各自可独立检索、自成完整语义的子问题；单一知识点的简单问题不要拆。多主题场景（如"密码含emoji导致校验失败且被自动登出"同时涉及密码校验与登录会话）必须按主题分别输出多条查询，每条末尾附带自己对应的主题词，不要合并成一条。语言变体：中文问题中含英文专有术语（括号标注或中英混写，如"完美注入抵抗（perfect injection resistance）"）时，追加 1 个以该英文术语为核心的英文检索子查询（如"perfect injection resistance variable escaping"），用于命中英文知识库文档；纯中文且无英文术语的问题不要生成英文子查询。
3. 澄清判定：仅当用户在描述自身或具体的个案情况（如某笔订单/交易被拦截、某账号登录异常、某接口流量突增），且缺少定位或处置所必需的标识要素（订单号/流水号、设备或环境标识、完整的时间与金额组合、支付通道、触发的提示信息等），对话历史也无法补全时，设置 clarify=true，并在 clarify_question 中礼貌追问当前最关键的缺失要素（一次只追问一个，不要罗列多个方向）。知识概念类问题（如何防御X/什么是Y/手册对Z怎么规定）必须 clarify=false，即答不出也只是检索问题，不是要素缺失。
4. 只输出 JSON 对象，不要解释。示例：
{"queries": [{"query": "公司年假请假流程与天数规定", "reconstruct": true}], "clarify": false, "clarify_question": ""}
{"queries": [{"query": "畸形邮箱导致密码重置失败时的处理流程", "reconstruct": true}, {"query": "畸形邮箱的判定与校验规则"}, {"query": "密码重置的流程与要求"}], "clarify": false, "clarify_question": ""}
{"queries": [{"query": "凌晨连续多笔4999元拆分转账且设备与收货地址变更的交易欺诈排查", "reconstruct": true}], "clarify": false, "clarify_question": ""}
{"queries": [{"query": "用户注册时手机号格式校验误判正常号码的问题排查 input validation", "reconstruct": true}], "clarify": false, "clarify_question": ""}
{"queries": [{"query": "登录后采集人脸信息并存储原始照片的合规性要求", "reconstruct": true}], "clarify": false, "clarify_question": ""}
{"queries": [], "clarify": true, "clarify_question": "请提供您的订单号或大致下单时间，以便我查询该订单的拦截原因。"}
{"queries": [], "clarify": true, "clarify_question": "请问该接口的流量突增持续了多长时间？是否伴随异常来源IP？"}"""

# 注入历史的最大轮数（1 轮 = user + assistant 两条消息）
MAX_HISTORY_ROUNDS = 3
# 单条历史消息截断长度（字符），控制改写调用 token 成本
HISTORY_MESSAGE_MAX_CHARS = 200


@dataclass
class RewriteResult:
    """改写结果：子查询列表 + 澄清标志（搭同一次 LLM 调用，零额外成本）。"""

    queries: list[str] = field(default_factory=list)
    clarify: bool = False
    clarify_question: str = ""


class QueryRewriter:
    def __init__(self, llm: LLMClient, doc_domains: list[str] | None = None):
        self.llm = llm
        # 知识库文档域清单：注入 prompt 让场景句改写能对齐领域术语
        # （RelevanceGate 对纯口语句双模型分数趋零，带术语尾巴才可召回）
        self.doc_domains = list(doc_domains or [])

    def _system_prompt(self) -> str:
        if not self.doc_domains:
            return PROMPT
        listed = "\n".join(f"- {d.removesuffix('.md').replace('_', ' ')}" for d in self.doc_domains[:60])
        return (
            PROMPT
            + "\n\n知识库文档域参考（第 1 个查询末尾以空格追加 1~3 个最相关文档域的术语短语，仅限此清单内；清单外不要编造）：\n"
            + listed
        )

    async def rewrite(
        self, question: str, history: list[dict] | None = None
    ) -> RewriteResult:
        """返回 RewriteResult；失败时降级为 [原问题] 且不澄清（绝不能让链路崩）。"""
        try:
            raw = await self.llm.chat(
                self._messages(question, history),
                temperature=0.3, top_p=0.6,
            )
            return self._parse(raw, question)
        except LLMError as e:
            _logger.warning("查询改写 LLM 调用失败，回退原问题: %s", e)
            return RewriteResult(queries=[question])
        except Exception as e:  # noqa: BLE001
            # JSON 解析等异常也降级
            _logger.debug("查询改写结果处理失败，回退原问题: %s", e)
            return RewriteResult(queries=[question])

    def _messages(self, question: str, history: list[dict] | None) -> list[dict]:
        msgs: list[dict] = [{"role": "system", "content": self._system_prompt()}]
        recent = list(history or [])[-MAX_HISTORY_ROUNDS * 2:]
        for h in recent:
            role = h.get("role")
            content = (h.get("content") or "").strip()[:HISTORY_MESSAGE_MAX_CHARS]
            if role in ("user", "assistant") and content:
                msgs.append({"role": role, "content": content})
        msgs.append({"role": "user", "content": question})
        return msgs

    def _parse(self, raw: str, fallback: str) -> RewriteResult:
        try:
            cleaned = raw.strip().strip("`").replace("```json", "").replace("```", "").strip()
            data = json.loads(cleaned)
            if isinstance(data, list):
                arr, clarify, cq = data, False, ""
            else:
                arr = data.get("queries", [])
                clarify = bool(data.get("clarify", False))
                cq = str(data.get("clarify_question") or "").strip()
            queries = [str(x["query"]) if isinstance(x, dict) else str(x) for x in arr if x]
            queries = [q.strip() for q in queries if q.strip()]
            if clarify and cq:
                return RewriteResult(queries=queries, clarify=True, clarify_question=cq)
            if queries:
                return RewriteResult(queries=queries)
        except Exception as e:  # noqa: BLE001
            _logger.debug("改写结果 JSON 解析失败，回退规则拆分: %s", e)
        return RewriteResult(queries=self._rule_split(fallback))

    @staticmethod
    def _rule_split(text: str) -> list[str]:
        """规则降级拆分：按中文标点/连词切分。"""
        parts = re.split(r"[；;。！!？?\n]|以及|,|并且|和|与|还有", text)
        return [p.strip() for p in parts if p.strip()] or [text]
