"""SOP 技能加载器（HolmesGPT Skills 机制的 Phase 1 简化版）。

- skills/<skill-name>/SKILL.md：YAML frontmatter（name/description）+ 正文 SOP
- 目录注入只带 name+description（省 token），正文按需加载
- Phase 1 采用「分诊阶段语义匹配 → 调查上下文注入全文」的简单策略；
  Phase 2 可升级为 LLM 主动 load_skill 动作
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_SKILL_DIR = Path(__file__).resolve().parent.parent.parent / "skills"


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: str


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 ``---`` 包裹的简易 frontmatter，返回 (meta, body)。"""
    meta: dict[str, str] = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return meta, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return meta, text
    for line in lines[1:end]:
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    body = "\n".join(lines[end + 1 :]).strip()
    return meta, body


class SkillLoader:
    """扫描技能目录并解析 SKILL.md。目录缺失/解析失败均不抛异常（降级为空技能集）。"""

    def __init__(self, skill_dir: str | Path | None = None) -> None:
        self.skill_dir = Path(skill_dir) if skill_dir else _DEFAULT_SKILL_DIR
        self._skills: dict[str, Skill] = {}
        self.reload()

    def reload(self) -> None:
        self._skills.clear()
        try:
            if not self.skill_dir.is_dir():
                logger.warning("[case] 技能目录不存在: %s", self.skill_dir)
                return
            for md in sorted(self.skill_dir.glob("*/SKILL.md")):
                try:
                    meta, body = _parse_frontmatter(md.read_text(encoding="utf-8"))
                    name = meta.get("name", md.parent.name)
                    self._skills[name] = Skill(
                        name=name,
                        description=meta.get("description", ""),
                        body=body,
                        path=str(md),
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("[case] 技能解析失败: %s", md)
        except Exception:  # noqa: BLE001
            logger.exception("[case] 技能目录扫描失败: %s", self.skill_dir)
        logger.info("[case] 已加载技能: %s", list(self._skills) or "无")

    def catalog_text(self) -> str:
        """技能目录（仅 name+description，注入 prompt 用）。"""
        if not self._skills:
            return "（暂无可用 SOP 技能）"
        return "\n".join(f"- {s.name}: {s.description}" for s in self._skills.values())

    def get(self, name: str) -> Skill | None:
        if not name:
            return None
        skill = self._skills.get(name.strip())
        if skill:
            return skill
        # 名称模糊兜底：包含匹配
        key = name.strip().lower()
        for s in self._skills.values():
            if key in s.name.lower() or s.name.lower() in key:
                return s
        return None
