"""查询改写与拆分：对应 ragent-main rag/core/rewrite。

原版逻辑（MultiQuestionRewriteService）：
  - LLM 把用户问题改写为"检索友好查询"，并重构成多个子问题（rq[0] 完整重构，rq[1..] 子问题）
  - LLM 失败 -> 回退原问题 + 规则拆分（按标点/连词）
  - 结果用于"每个子问题单独意图识别 + 检索"

保留语义：
  - 失败必须降级，不能让整条链路崩
  - 改写温度高一点(0.5)利于发散；但首包探测用低温度
"""
from __future__ import annotations

import json
import re

from core.llm import LLMClient, LLMError

PROMPT = """你是一个查询理解与重构助手。请将用户的输入，转换为更利于向量检索的检索查询，并拆分为多个子查询。

要求：
1. 第 1 个查询是对用户问题的完整、准确的语义重构（补全指代、明确实体）。
2. 第 2~N 个查询是拆解出的独立子问题（当问题复杂、含多意图时才有）。
3. 只输出 JSON 数组，不要解释。示例：
[{"query": "公司年假请假流程与天数规定", "reconstruct": true}, {"query": "年假申请入口在哪里", "reconstruct": false}]
"""


class QueryRewriter:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    async def rewrite(self, question: str) -> list[str]:
        """返回子查询列表；失败时返回 [原问题]（降级）。"""
        try:
            raw = await self.llm.chat(
                [{"role": "system", "content": PROMPT}, {"role": "user", "content": question}],
                temperature=0.5, top_p=0.6,
            )
            return self._parse(raw, question)
        except LLMError:
            return [question]
        except Exception:
            # JSON 解析等异常也降级
            return [question]

    def _parse(self, raw: str, fallback: str) -> list[str]:
        try:
            cleaned = raw.strip().strip("`").replace("```json", "").replace("```", "").strip()
            data = json.loads(cleaned)
            arr = data if isinstance(data, list) else data.get("queries", [])
            queries = [str(x["query"]) if isinstance(x, dict) else str(x) for x in arr if x]
            queries = [q.strip() for q in queries if q.strip()]
            if queries:
                return queries
        except Exception:
            pass
        return self._rule_split(fallback)

    @staticmethod
    def _rule_split(text: str) -> list[str]:
        """规则降级拆分：按中文标点/连词切分。"""
        parts = re.split(r"[；;。！!？?\n]|以及|,|并且|和|与|还有", text)
        return [p.strip() for p in parts if p.strip()] or [text]
