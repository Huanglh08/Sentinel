"""提示词模板工具：1:1 翻译 ragent PromptTemplateUtils（fillSlots / cleanupPrompt）。"""
from __future__ import annotations

import re

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def fill_slots(template: str, slots: dict[str, str]) -> str:
    """1:1 翻译 PromptTemplateUtils#fillSlots：将 {key} 替换为给定值，缺失则留空。"""
    if not template:
        return ""
    return _PLACEHOLDER_RE.sub(lambda m: slots.get(m.group(1), ""), template)


def cleanup_prompt(text: str) -> str:
    """1:1 翻译 PromptTemplateUtils#cleanupPrompt：清理多余空行与首尾空白。"""
    if not text:
        return ""
    lines = [ln.rstrip() for ln in text.splitlines()]
    # 折叠连续空行
    out: list[str] = []
    blank = False
    for ln in lines:
        if ln == "":
            if blank:
                continue
            blank = True
        else:
            blank = False
        out.append(ln)
    return "\n".join(out).strip()
