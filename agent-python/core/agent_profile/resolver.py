"""智能体提示词解析器：1:1 翻译 ragent AgentPromptResolver。

优先取激活智能体的槽位，空白则回落内置智能体；面向终端用户的提示词一律从此处读取。
"""
from __future__ import annotations

import logging

from core.agent_profile.cache import AgentPromptCacheManager
from core.agent_profile.do import AgentProfileDO
from core.agent_profile.repository import AgentPromptRepository, AgentProfileRepository
from core.agent_profile.slots import AgentPromptSlot

_logger = logging.getLogger("sentinel.agent_profile.resolver")


class AgentPromptResolver:
    """1:1 翻译 ragent AgentPromptResolver。"""

    def __init__(self, profile_repo: AgentProfileRepository,
                 prompt_repo: AgentPromptRepository,
                 cache_manager: AgentPromptCacheManager) -> None:
        self._profile_repo = profile_repo
        self._prompt_repo = prompt_repo
        self._cache_manager = cache_manager

    def resolve(self, slot: AgentPromptSlot | None) -> str:
        if slot is None:
            return ""
        return self.resolve_all().get(slot.name, "") or ""

    def render(self, slot: AgentPromptSlot, slots: dict[str, str]) -> str:
        from core.agent_profile.prompt_utils import cleanup_prompt, fill_slots

        return cleanup_prompt(fill_slots(self.resolve(slot), slots))

    def resolve_all(self) -> dict:
        cached = self._cache_manager.get_from_cache()
        if cached is not None:
            return cached
        resolved = self._load_from_db()
        self._cache_manager.save_to_cache(resolved)
        return resolved

    def load_own_prompts(self, agent_id: str | None) -> dict:
        """读取某个智能体自身配置的槽位，不做回落，供控制台编辑态展示。"""
        own: dict = {}
        if not agent_id:
            return own
        for prompt in self._prompt_repo.select_by_agent(agent_id):
            own[prompt.slot_key] = prompt.content or ""
        return own

    def _load_from_db(self) -> dict:
        builtin = self._profile_repo.select_first_by_flag("builtin")
        if builtin is None:
            _logger.warning("未找到内置智能体，空槽位将无提示词可回落")
        resolved: dict = {}
        self._put_non_blank(resolved, builtin)
        self._put_non_blank(resolved, self._profile_repo.select_first_by_flag("active"))
        return resolved

    def _put_non_blank(self, target: dict, profile: AgentProfileDO | None) -> None:
        if profile is None:
            return
        for slot_key, content in self.load_own_prompts(profile.id).items():
            if content and content.strip():
                target[slot_key] = content
