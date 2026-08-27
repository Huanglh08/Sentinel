"""意图解析器：1:1 翻译 ragent rag.core.intent.IntentResolver（LLM 打分）。

链路：
  - resolve(rewrite_result) → 并发对每个 sub_question 调用 classify → capTotalIntents
  - classify(question) → LLM 打分（与 ragent DefaultIntentClassifier.classifyTargets 严格一致）
  - cap_total_intents(sub_intents) → 配额控制（与 ragent IntentResolver.capTotalIntents 严格一致）
  - merge_intent_group(sub_intents) → 合并 mcp / kb（与 ragent IntentResolver.mergeIntentGroup 严格一致）

任何 LLM 失败 → 返回空 list（降级为无意图，让上层走通用兜底）。
"""
from __future__ import annotations

import asyncio
import json
import logging

from core.intent.types import IntentNode, NodeScore, IntentKind
from core.intent.factory import DEFAULT_INTENT_TREE
from core.intent.sub_question_intent import SubQuestionIntent
from core.llm import LLMClient, LLMError

_logger = logging.getLogger(__name__)

INTENT_MIN_SCORE = 0.3
MAX_INTENT_COUNT = 5


class IntentResolver:
    def __init__(self, llm: LLMClient, tree: list[IntentNode] | None = None) -> None:
        self.llm = llm
        self.tree = tree or DEFAULT_INTENT_TREE
        # 取 KB + MCP 节点作候选（SYSTEM 节点走兜底）
        candidates = [n for n in self.tree if n.kind != IntentKind.SYSTEM]
        self.leaf_nodes = candidates or self.tree

    def _build_prompt(self) -> str:
        lines = [
            "你是一个意图分类器。下面是一组意图节点，请判断用户问题匹配哪些意图，"
            "并为每个匹配项给出 0~1 的置信度 score。", "",
            "只从以下 id 中选择，输出 JSON 数组：",
            '[{"id": "<意图id>", "score": 0.9, "reason": "<简短理由>"}]', "",
            "特别强调：如果问题只提到某个具体系统/主题，不要选不相关的分类。", "",
            "可选意图节点：",
        ]
        for n in self.leaf_nodes:
            t = n.kind.value
            extra = f"  toolId={n.tool_id}" if n.tool_id else ""
            ex = f"  examples={ ' / '.join(n.examples) }" if n.examples else ""
            lines.append(f"- id={n.id}\n  path={n.name}\n  description={n.description}\n  type={t}{extra}{ex}\n")
        return "\n".join(lines)

    # ---- 顶层入口：1:1 翻译 ragent IntentResolver.resolve ----
    async def resolve(self, sub_questions: list[str]) -> list[SubQuestionIntent]:
        """严格对应 ragent IntentResolver.resolve(rewriteResult)：
        并发对每个 sub_question 调用 classifyIntents，捕获每个子问题的异常降级为空意图，
        再走 capTotalIntents 做总配额控制。
        """
        if not sub_questions:
            return []
        tasks = [asyncio.create_task(self._classify_one(q)) for q in sub_questions]
        results = await asyncio.gather(*tasks)
        return self.cap_total_intents(list(results))

    async def _classify_one(self, question: str) -> SubQuestionIntent:
        try:
            scores = await self.classify(question)
        except Exception as e:  # noqa: BLE001
            _logger.warning("子问题意图分类失败，降级为空意图，question：%s, err=%s", question, e)
            scores = []
        return SubQuestionIntent(question=question, node_scores=scores)

    # ---- 单子问题分类：1:1 翻译 ragent classifyIntents ----
    async def classify(self, question: str) -> list[NodeScore]:
        """LLM 打分；任何失败都返回空列表（降级为无意图）。"""
        try:
            raw = await self.llm.chat(
                [{"role": "system", "content": self._build_prompt()},
                 {"role": "user", "content": question}],
                temperature=0.1, top_p=0.3,
            )
        except LLMError:
            return []
        return self._parse_scores(raw)

    def _parse_scores(self, raw: str) -> list[NodeScore]:
        try:
            cleaned = raw.strip().strip("`").replace("```json", "").replace("```", "").strip()
            data = json.loads(cleaned)
            arr = data if isinstance(data, list) else data.get("results", [])
            id2node = {n.id: n for n in self.leaf_nodes}
            scores: list[NodeScore] = []
            for item in arr:
                if not isinstance(item, dict) or "id" not in item or "score" not in item:
                    continue
                node = id2node.get(item["id"])
                if node is None:
                    continue
                scores.append(NodeScore(node, float(item["score"]), item.get("reason", "")))
            scores.sort(key=lambda s: s.score, reverse=True)
            return [s for s in scores if s.score >= INTENT_MIN_SCORE][:MAX_INTENT_COUNT]
        except Exception:
            return []

    # ---- 配额控制：1:1 翻译 ragent IntentResolver.capTotalIntents ----
    def cap_total_intents(self, sub_intents: list[SubQuestionIntent]) -> list[SubQuestionIntent]:
        """严格对应 ragent IntentResolver.capTotalIntents：
          1. 总数 ≤ MAX_INTENT_COUNT → 原样返回
          2. 每个子问题保底 1 个最高分
          3. 剩余配额按分数从高到低分配
        """
        total = sum(len(si.node_scores) for si in sub_intents)
        if total <= MAX_INTENT_COUNT:
            return sub_intents

        # 步骤1：收集所有候选，标记所属子问题索引 + 按分数降序
        candidates: list[tuple[int, float, NodeScore]] = []
        for i, si in enumerate(sub_intents):
            for ns in si.node_scores:
                candidates.append((i, ns.score, ns))
        candidates.sort(key=lambda x: x[1], reverse=True)

        # 步骤2：每个子问题保底 1 个最高分
        reserved_idx: set[int] = set()
        reserved: list[tuple[int, float, NodeScore]] = []
        for i, score, ns in candidates:
            if i in reserved_idx:
                continue
            reserved.append((i, score, ns))
            reserved_idx.add(i)

        # 步骤3：剩余配额（去除已 reserved 的）
        remaining = MAX_INTENT_COUNT - len(reserved)
        used_keys: set[tuple[int, str]] = {(i, ns.node.id) for i, _, ns in reserved if ns.node}
        additional: list[tuple[int, float, NodeScore]] = []
        for cand in candidates:
            if remaining <= 0:
                break
            i, _, ns = cand
            key = (i, ns.node.id) if ns.node else (i, "")
            if key in used_keys:
                continue
            additional.append(cand)
            used_keys.add(key)
            remaining -= 1

        # 步骤4：按子问题索引重组
        chosen: dict[int, list[NodeScore]] = {}
        for i, _, ns in reserved + additional:
            chosen.setdefault(i, []).append(ns)
        # 在每个子问题内部，按分数重新降序
        result: list[SubQuestionIntent] = []
        for i, si in enumerate(sub_intents):
            scores = chosen.get(i, [])
            scores.sort(key=lambda s: s.score, reverse=True)
            result.append(SubQuestionIntent(question=si.question, node_scores=scores))
        return result

    # ---- 合并 MCP / KB：1:1 翻译 ragent IntentResolver.mergeIntentGroup ----
    def merge_intent_group(self, sub_intents: list[SubQuestionIntent]) -> tuple[list[NodeScore], list[NodeScore]]:
        """返回 (mcp_intents, kb_intents)，与 ragent IntentGroup.getMcpIntents / getKbIntents 一致。"""
        mcp: list[NodeScore] = []
        kb: list[NodeScore] = []
        for si in sub_intents:
            mcp.extend(self._filter_mcp(si.node_scores))
            kb.extend(self._filter_kb(si.node_scores))
        return mcp, kb

    @staticmethod
    def _filter_mcp(scores: list[NodeScore]) -> list[NodeScore]:
        """1:1 翻译 ragent NodeScoreFilters.mcp：node 非空、kind=MCP、toolId 非空。"""
        return [
            s for s in scores
            if s.node and s.node.kind == IntentKind.MCP and (s.node.tool_id or "")
        ]

    @staticmethod
    def _filter_kb(scores: list[NodeScore]) -> list[NodeScore]:
        """1:1 翻译 ragent NodeScoreFilters.kb：node 非空、kind=KB。"""
        return [s for s in scores if s.node and s.node.kind == IntentKind.KB]

    # ---- 旧 split_by_kind 保留（向后兼容）：与 merge_intent_group 等价 ----
    def split_by_kind(self, scores: list[NodeScore]) -> tuple[list[NodeScore], list[NodeScore]]:
        """旧入口：与 merge_intent_group 等价，保留用于已有调用。"""
        return self._filter_mcp(scores), self._filter_kb(scores)
