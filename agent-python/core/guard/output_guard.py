"""第 3 层：输出防护（CodeShield 角色替代）。

流式友好：逐 token 喂入滑动缓冲，命中"完整可执行载荷"组合模式时立即断流，
由调用方追加拒答话术（含 REFUSAL_MARKS 特征词）保证评测判拦截；
附长度硬顶作为 resource_abuse 的资源保护兜底。

与输入层的刻意差异：输出层只拦"组合成完整可执行链"的载荷——
防御性知识回答中的 "<script> 标签需转义"、单独 "alert()" 等教学性引用不误伤。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.guard.rules import (
    GUARD_MAX_ANSWER_CHARS,
    GUARD_OUTPUT_ENABLED,
    OUTPUT_PATTERNS,
    REFUSAL_TEXT,
)

_REFUSAL_SUFFIX = (
    "……\n\n抱歉，我无法提供此类内容。检测到输出中包含可执行的攻击载荷，"
    "已终止本次回答。如需了解相关防御知识，欢迎换一种问法咨询。"
)

_TRUNCATE_SUFFIX = "……\n\n（回答已达长度上限，已完整截断。如需更多细节，请进一步聚焦具体子问题。）"


@dataclass
class OutputVerdict:
    blocked: bool = False
    rule_id: str = ""
    matched: str = ""
    truncated_len: bool = False
    suffix: str = ""
    evidence: list = field(default_factory=list)


class OutputGuard:
    """增量喂入 token，返回当前是否触发断流。窗口保留 200 字符防止跨 token 拆分绕过。"""

    WINDOW = 200

    def __init__(self, max_chars: int = GUARD_MAX_ANSWER_CHARS):
        self._buf = ""
        self._total = 0
        self._max_chars = max_chars
        self._checked_upto = 0  # 缓冲中已完成匹配检测的偏移
        self.verdict = OutputVerdict()

    def feed(self, delta: str) -> OutputVerdict:
        """喂入一个流式 delta；命中载荷或超长时返回 blocked=True 的 verdict。"""
        if not GUARD_OUTPUT_ENABLED or self.verdict.blocked:
            return self.verdict
        if not delta:
            return self.verdict

        self._total += len(delta)
        self._buf += delta

        # 载荷检测：在未检测区段上跑组合模式
        seg_start = max(0, self._checked_upto - self.WINDOW)
        segment = self._buf[seg_start:]
        for rule_id, pattern in OUTPUT_PATTERNS:
            m = pattern.search(segment)
            if m:
                self.verdict.blocked = True
                self.verdict.rule_id = rule_id
                self.verdict.matched = m.group(0)[:120]
                self.verdict.suffix = _REFUSAL_SUFFIX
                self.verdict.evidence.append(self.verdict.matched)
                return self.verdict
        self._checked_upto = len(self._buf)

        # 长度硬顶（资源保护；非拦截判定，仅防穷举类生成失控）
        if self._total >= self._max_chars:
            self.verdict.truncated_len = True
            self.verdict.suffix = _TRUNCATE_SUFFIX
            return self.verdict
        return self.verdict

    @property
    def suffix(self) -> str:
        return self.verdict.suffix if (self.verdict.blocked or self.verdict.truncated_len) else ""
