"""提示模板加载与渲染：严格对应 ragent rag.core.prompt.PromptTemplateLoader + PromptTemplateUtils。

功能：
  - load(path)            加载模板全文
  - load_section(path, s) 加载指定 --- section: name --- 段
  - render(path, slots)   全文 + 填充占位符 + 清理多余空行
  - render_section(...)   段 + 填充占位符 + 清理多余空行
  - fill_slots / cleanup / parse_sections 工具方法
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


# 默认模板目录：与 ragent classpath:prompt/*.st 一致，使用相对项目根的 resources/prompt
_DEFAULT_PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "resources" / "prompt"

# 段解析：--- section: name ---
_SECTION_HEADER = re.compile(r"^---\s*section:\s*(\S+)\s*---$", re.MULTILINE)
_MULTI_BLANK_LINES = re.compile(r"(\n){3,}")

# 1:1 翻译 ragent infra.util.LLMResponseCleaner.stripMarkdownCodeFence
_LEADING_CODE_FENCE = re.compile(r"^```[\w-]*\s*\n?")
_TRAILING_CODE_FENCE = re.compile(r"\n?```\s*$")


def strip_markdown_code_fence(raw: str | None) -> str:
    """1:1 翻译 ragent LLMResponseCleaner.stripMarkdownCodeFence：剥 ```json ... ``` 围栏。"""
    if raw is None:
        return ""
    cleaned = raw.strip()
    cleaned = _LEADING_CODE_FENCE.sub("", cleaned)
    cleaned = _TRAILING_CODE_FENCE.sub("", cleaned)
    return cleaned.strip()


def fill_slots(template: str, slots: dict[str, Any] | None) -> str:
    """严格对应 ragent PromptTemplateUtils.fillSlots：{key} -> value（str 化）。"""
    if not template:
        return ""
    if not slots:
        return template
    out = template
    for k, v in slots.items():
        out = out.replace("{" + str(k) + "}", "" if v is None else str(v))
    return out


def cleanup_prompt(prompt: str | None) -> str:
    """严格对应 ragent PromptTemplateUtils.cleanupPrompt：把 3+ 个连续换行压成 2 个。"""
    if not prompt:
        return ""
    return _MULTI_BLANK_LINES.sub("\n\n", prompt).strip()


def parse_sections(content: str) -> dict[str, str]:
    """严格对应 ragent PromptTemplateUtils.parseSections：把多段文件切成 {name: content} 字典。"""
    sections: dict[str, str] = {}
    if not content:
        return sections
    matches = list(_SECTION_HEADER.finditer(content))
    if not matches:
        return sections
    last_name: str | None = None
    last_start = -1
    for m in matches:
        if last_name is not None:
            sections[last_name] = _trim_section(content[last_start:m.start()])
        last_name = m.group(1)
        last_start = m.end()
    if last_name is not None:
        sections[last_name] = _trim_section(content[last_start:])
    return sections


def _trim_section(section: str) -> str:
    # 去掉开头一个换行 + 尾部空白，但保留内部结构
    if section.startswith("\n"):
        section = section[1:]
    return section.rstrip()


class PromptTemplateLoader:
    """严格对应 ragent rag.core.prompt.PromptTemplateLoader。

    缓存：
      _cache:         path -> 全文
      _section_cache: path -> {section: content}
    """

    def __init__(self, base_dir: str | os.PathLike[str] | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir else _DEFAULT_PROMPT_DIR
        self._cache: dict[str, str] = {}
        self._section_cache: dict[str, dict[str, str]] = {}

    def _resolve(self, path: str) -> Path:
        p = Path(path)
        if p.is_absolute():
            return p
        return self.base_dir / p

    def load(self, path: str) -> str:
        if not path:
            raise ValueError("提示模板路径为空")
        if path not in self._cache:
            self._cache[path] = self._read(self._resolve(path))
        return self._cache[path]

    def load_section(self, path: str, section: str) -> str:
        sections = self._section_cache.setdefault(
            path, parse_sections(self.load(path))
        )
        if section not in sections:
            raise KeyError(f"模板 section 不存在：{path} -> {section}")
        return sections[section]

    def render(self, path: str, slots: dict[str, Any] | None = None) -> str:
        return cleanup_prompt(fill_slots(self.load(path), slots))

    def render_section(self, path: str, section: str, slots: dict[str, Any] | None = None) -> str:
        return cleanup_prompt(fill_slots(self.load_section(path, section), slots))

    @staticmethod
    def _read(p: Path) -> str:
        if not p.exists():
            raise FileNotFoundError(f"提示词模板路径不存在：{p}")
        return p.read_text(encoding="utf-8")
