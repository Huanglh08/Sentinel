"""第 1 层：输入防护（确定性规则分类，PromptGuard 角色替代）。

命中即拦：Agent.answer() 链路最前端短路，零检索、零生成，延迟 <1ms。
拦截动作产出统一拒答话术（命中评测 REFUSAL_MARKS 特征词）且 sources 为空，
在 redteam_eval 的 `refused or not sources` 判定下形成双重确定性拦截。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.guard.rules import GUARD_ENABLED, INVISIBLE_CHAR_RE, INPUT_RULES, REFUSAL_TEXT


@dataclass
class GuardVerdict:
    blocked: bool = False
    rule_id: str = ""
    matched: str = ""
    refusal_text: str = REFUSAL_TEXT
    evidence: list = field(default_factory=list)


class InputGuard:
    """对用户问题做高危模式扫描。规则全为高特异性组合模式，单关键词不拦。"""

    def inspect(self, question: str) -> GuardVerdict:
        verdict = GuardVerdict()
        if not GUARD_ENABLED or not question:
            return verdict
        text = question.strip()
        if not text:
            return verdict
        # 长串折叠：base64 裸串可能非常长，避免 regex 回溯开销
        text = text if len(text) <= 2000 else text[:2000]
        # R9 不可见字符混淆（encoding_obfuscation 边缘，治 RT042/RT044 零宽拆词）：
        # 正常输入几乎不含零宽字符，≥2 个即人为混淆强信号；随后清洗再匹配，
        # 防 "C01\u200b_Account" 类拆词绕过 R6-R8 的关键词匹配。
        invisible_hits = INVISIBLE_CHAR_RE.findall(text)
        if len(invisible_hits) >= 2:
            verdict.blocked = True
            verdict.rule_id = "R9_zwsp_density"
            verdict.matched = f"invisible_chars={len(invisible_hits)}"
            verdict.evidence = [verdict.matched]
            return verdict
        if invisible_hits:
            text = INVISIBLE_CHAR_RE.sub("", text)
        for rule_id, pattern in INPUT_RULES:
            m = pattern.search(text)
            if m:
                # 命中第一条即拦，但保留全部命中证据供日志/调试
                verdict.blocked = True
                verdict.rule_id = rule_id
                verdict.matched = m.group(0)[:120]
                verdict.evidence = [verdict.matched]
                break
        return verdict

    @staticmethod
    def inspect_all(question: str) -> list[tuple[str, str]]:
        """调试辅助：返回全部命中规则（不做短路），用于单测与规则回归。"""
        text = (question or "").strip()[:2000]
        hits: list[tuple[str, str]] = []
        # 与 inspect() 保持一致：先做零宽密度判定，再做清洗与规则匹配
        invisible_hits = INVISIBLE_CHAR_RE.findall(text)
        if len(invisible_hits) >= 2:
            hits.append(("R9_zwsp_density", f"invisible_chars={len(invisible_hits)}"))
            return hits
        if invisible_hits:
            text = INVISIBLE_CHAR_RE.sub("", text)
        for rule_id, pattern in INPUT_RULES:
            m = pattern.search(text)
            if m:
                hits.append((rule_id, m.group(0)[:120]))
        return hits
