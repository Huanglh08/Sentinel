"""歧义引导服务：1:1 翻译 ragent IntentGuidanceService。

当用户问题存在歧义（多个意图分支得分接近）时，主动引导用户澄清。
核心逻辑：
  1. 单问题 + 多个 KB 意图候选（得分 >= INTENT_MIN_SCORE）
  2. 按 systemNodeId 去重取最高分
  3. 快速通道：分数比值低于边界下限时跳过澄清
  4. 快速通道：用户问题中显式提到某个系统名时跳过澄清
  5. 边界区间调 LLM 确认歧义
  6. 确认后渲染引导提示
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from core.intent.types import IntentNode, IntentLevel, NodeScore

_logger = logging.getLogger("sentinel.intent.guidance")

# 配置默认值（与 ragent GuidanceProperties 默认值一致）
DEFAULT_AMBIGUITY_SCORE_RATIO = 0.8
DEFAULT_AMBIGUITY_MARGIN = 0.15
DEFAULT_MAX_OPTIONS = 5
INTENT_MIN_SCORE = 0.3


@dataclass
class GuidanceDecision:
    """1:1 翻译 ragent GuidanceDecision。"""
    action: str  # "NONE" or "PROMPT"
    prompt: str | None = None

    @property
    def is_prompt(self) -> bool:
        return self.action == "PROMPT"

    @staticmethod
    def none() -> "GuidanceDecision":
        return GuidanceDecision(action="NONE", prompt=None)

    @staticmethod
    def prompt(text: str) -> "GuidanceDecision":
        return GuidanceDecision(action="PROMPT", prompt=text)


class IntentGuidanceService:
    """1:1 翻译 ragent IntentGuidanceService。

    检测用户问题是否存在歧义，需要引导用户澄清。
    """

    def __init__(
        self,
        enabled: bool = True,
        ambiguity_score_ratio: float = DEFAULT_AMBIGUITY_SCORE_RATIO,
        ambiguity_margin: float = DEFAULT_AMBIGUITY_MARGIN,
        max_options: int = DEFAULT_MAX_OPTIONS,
        llm_checker: Any = None,
    ) -> None:
        self._enabled = enabled
        self._ambiguity_score_ratio = ambiguity_score_ratio
        self._ambiguity_margin = ambiguity_margin
        self._max_options = max_options
        self._llm_checker = llm_checker

    def detect_ambiguity(
        self,
        question: str,
        sub_intents: list[Any],
    ) -> GuidanceDecision:
        """检测歧义并返回决策。

        Args:
            question: 用户问题（改写后）
            sub_intents: 子问题意图列表

        Returns:
            GuidanceDecision: 引导决策
        """
        if not self._enabled:
            return GuidanceDecision.none()

        if not sub_intents or len(sub_intents) != 1:
            return GuidanceDecision.none()

        # 获取 KB 意图候选
        candidates = self._filter_candidates(sub_intents[0].node_scores)
        if len(candidates) < 2:
            return GuidanceDecision.none()

        # 按 systemNodeId 去重取最高分
        system_best: dict[str, NodeScore] = {}
        for ns in candidates:
            system_node_id = self._resolve_system_node_id(ns.node)
            if not system_node_id:
                continue
            if system_node_id not in system_best or ns.score > system_best[system_node_id].score:
                system_best[system_node_id] = ns

        ranked = sorted(system_best.values(), key=lambda x: x.score, reverse=True)
        if len(ranked) < 2:
            return GuidanceDecision.none()

        # 快速通道 1：分数比值低于边界下限
        if self._should_skip_guidance(question, ranked):
            return GuidanceDecision.none()

        # 确认歧义
        if not self._confirm_ambiguity(question, ranked):
            return GuidanceDecision.none()

        # 裁剪选项
        trimmed = ranked[: self._max_options]
        topic_name = trimmed[0].node.name if trimmed[0].node else ""

        # 渲染引导提示
        prompt = self._build_prompt(topic_name, trimmed)
        return GuidanceDecision.prompt(prompt)

    def _filter_candidates(self, scores: list[NodeScore]) -> list[NodeScore]:
        """过滤 KB 意图候选。"""
        return [ns for ns in scores if ns.score >= INTENT_MIN_SCORE]

    def _resolve_system_node_id(self, node: IntentNode | None) -> str | None:
        """解析系统节点 ID（向上查找到 DOMAIN 下的 CATEGORY）。"""
        if node is None:
            return None
        current = node
        parent = self._fetch_parent(current)
        while current is not None:
            level = current.level
            if level == IntentLevel.CATEGORY and (parent is None or parent.level == IntentLevel.DOMAIN):
                return current.id
            if parent is None:
                return current.id
            current = parent
            parent = self._fetch_parent(current)
        return None

    def _fetch_parent(self, node: IntentNode) -> IntentNode | None:
        """获取父节点。"""
        if node is None or not node.parent_id:
            return None
        # 从意图树中查找父节点
        from core.intent.loader import InMemoryIntentLoader
        loader = InMemoryIntentLoader()
        tree = loader.load()
        if tree and node.parent_id in tree.nodes:
            return tree.nodes[node.parent_id]
        return None

    def _resolve_domain_name(self, node: IntentNode | None) -> str:
        """解析域名（向上查找到 DOMAIN 级别）。"""
        if node is None:
            return ""
        current = node
        while current is not None:
            if current.level == IntentLevel.DOMAIN:
                return current.name or ""
            parent = self._fetch_parent(current)
            current = parent
        return ""

    def _should_skip_guidance(self, question: str, ranked: list[NodeScore]) -> bool:
        """快速通道：判断是否跳过澄清。"""
        top = ranked[0].score
        if top <= 0:
            return True

        ratio = ranked[1].score / top if top > 0 else 0
        threshold = self._ambiguity_score_ratio
        margin = self._ambiguity_margin

        # 快速通道 1：分数比值低于边界下限，意图明确
        if ratio < threshold - margin:
            _logger.debug("分数比值(ratio=%.3f)低于边界下限(%.3f), 跳过澄清", ratio, threshold - margin)
            return True

        # 快速通道 2：用户问题中显式提到了某个系统的 DOMAIN 级名称
        if question:
            domain_names = []
            for ns in ranked:
                name = self._resolve_domain_name(ns.node)
                if name:
                    domain_names.append(name)

            normalized_question = self._normalize_name(question)
            for name in domain_names:
                for alias in self._build_system_aliases(name):
                    if len(alias) >= 2 and alias in normalized_question:
                        _logger.debug("用户问题包含系统名[%s], 跳过澄清", name)
                        return True

        return False

    def _confirm_ambiguity(self, question: str, ranked: list[NodeScore]) -> bool:
        """确认是否存在歧义。"""
        top = ranked[0].score
        second = ranked[1].score
        if top <= 0:
            return False

        ratio = second / top
        threshold = self._ambiguity_score_ratio
        margin = self._ambiguity_margin

        if ratio >= threshold:
            _logger.info("分数比值(ratio=%.3f)超过阈值(%.3f), 判定为歧义", ratio, threshold)
            return True

        if ratio >= threshold - margin:
            _logger.info("分数比值(ratio=%.3f)在边界区间[%.3f, %.3f), 调 LLM 确认",
                        ratio, threshold - margin, threshold)
            if self._llm_checker is not None:
                return self._llm_checker.check_ambiguity(question, ranked)
            # 无 LLM checker 时默认不触发澄清
            return False

        return False

    def _build_prompt(self, topic_name: str, ranked: list[NodeScore]) -> str:
        """构建引导提示。"""
        options_text = self._render_options(ranked)
        return (
            f"您的问题涉及【{topic_name}】相关的内容，请确认您想咨询的具体方向：\n"
            f"{options_text}\n"
            f"请回复对应的编号或具体描述，以便我为您提供更准确的回答。"
        )

    def _render_options(self, ranked: list[NodeScore]) -> str:
        """渲染选项列表。"""
        lines = []
        for i, ns in enumerate(ranked):
            display = self._resolve_option_display(ns.node)
            lines.append(f"{i + 1}) {display}")
        return "\n".join(lines)

    def _resolve_option_display(self, node: IntentNode | None) -> str:
        """解析选项显示文本。"""
        if node is None:
            return ""
        # IntentNode 数据类无 full_path 字段（历史上误引用导致 AttributeError 使 SSE 流中断）
        full_path = getattr(node, "full_path", None)
        if full_path:
            return str(full_path)
        return node.description or node.name or node.id

    def _build_system_aliases(self, system_name: str) -> list[str]:
        """构建系统别名列表。"""
        if not system_name:
            return []
        normalized = self._normalize_name(system_name)
        return [normalized] if normalized else []

    @staticmethod
    def _normalize_name(name: str) -> str:
        """标准化名称（小写+去除标点和空格）。"""
        if not name:
            return ""
        cleaned = name.strip().lower()
        return re.sub(r"[^\w\u4e00-\u9fff]+", "", cleaned)
