"""智能体管理业务：1:1 翻译 ragent AgentProfileAdminServiceImpl。"""
from __future__ import annotations

import logging
import os

from core.agent_profile.cache import AgentPromptCacheManager
from core.agent_profile.do import AgentProfileDO
from core.agent_profile.repository import AgentPromptRepository, AgentProfileRepository
from core.agent_profile.resolver import AgentPromptResolver
from core.agent_profile.slots import (
    AGENT_PROMPT_SLOTS,
    AgentPromptSlot,
    Group,
    OrchestrationMode,
)

_logger = logging.getLogger("sentinel.agent_profile.service")

AVATAR_MAX_LENGTH = 32


class ClientException(Exception):
    """1:1 翻译 ragent framework.exception.ClientException。"""


def _mode() -> OrchestrationMode:
    return OrchestrationMode.of(os.getenv("RAGENT_ENGINE_TYPE"))


class AgentProfileAdminService:
    """1:1 翻译 ragent AgentProfileAdminService。"""

    def __init__(self, profile_repo: AgentProfileRepository,
                 prompt_repo: AgentPromptRepository,
                 resolver: AgentPromptResolver,
                 cache_manager: AgentPromptCacheManager) -> None:
        self._profile_repo = profile_repo
        self._prompt_repo = prompt_repo
        self._resolver = resolver
        self._cache_manager = cache_manager

    # ---- 列表 ----
    def list(self) -> dict:
        mode = _mode()
        configured = self._load_configured_slots()
        agents = []
        for p in self._profile_repo.select_all():
            own = configured.get(p.id, [])
            effective = sum(1 for s in own if s.is_effective_in(mode))
            agents.append({
                "id": p.id, "name": p.name, "description": p.description,
                "avatar": p.avatar,
                "builtin": AgentProfileDO.is_true(p.builtin),
                "active": AgentProfileDO.is_true(p.active),
                "effectiveSlots": effective,
                "inactiveSlots": len(own) - effective,
                "createTime": p.create_time.isoformat() if p.create_time else None,
                "updateTime": p.update_time.isoformat() if p.update_time else None,
            })
        # 内置优先，其余按创建时间
        agents.sort(key=lambda a: (not a["builtin"], a["createTime"] or ""))
        return {
            "mode": mode.name,
            "effectiveSlotTotal": len(AgentPromptSlot.effective_in(mode)),
            "agents": agents,
        }

    def _load_configured_slots(self) -> dict:
        configured: dict = {}
        for prompt in self._prompt_repo.select_configured():
            slot = AgentPromptSlot.find(prompt.slot_key)
            if slot is None:
                continue
            configured.setdefault(prompt.agent_id, []).append(slot)
        return configured

    # ---- 创建 ----
    def create(self, name: str | None, description: str | None, avatar: str | None) -> str:
        name = self._trimmed_name(name)
        self._assert_name_available(name, None)
        profile = AgentProfileDO(
            name=name, description=_trim_to_none(description),
            avatar=self._trimmed_avatar(avatar), builtin=0, active=0,
        )
        return self._profile_repo.insert(profile)

    # ---- 更新 ----
    def update(self, agent_id: str, name: str | None, description: str | None, avatar: str | None) -> None:
        self._must_load_editable(agent_id)
        name = self._trimmed_name(name)
        self._assert_name_available(name, agent_id)
        self._profile_repo.update_fields(
            agent_id,
            name=name,
            description=_trim_to_none(description),
            avatar=self._trimmed_avatar(avatar),
        )

    # ---- 删除 ----
    def delete(self, agent_id: str) -> None:
        profile = self._must_load_editable(agent_id)
        if AgentProfileDO.is_true(profile.active):
            raise ClientException("该智能体正在激活中，请先激活其他智能体再删除")
        self._prompt_repo.delete_by_agent(agent_id)
        self._profile_repo.delete(agent_id)
        self._cache_manager.clear_cache()

    # ---- 激活 ----
    def activate(self, agent_id: str) -> None:
        self._must_load(agent_id)
        self._profile_repo.update_active_zero_where_active_one()
        self._profile_repo.update_fields(agent_id, active=1)
        self._cache_manager.clear_cache()

    # ---- 槽位配置 ----
    def load_prompts(self, agent_id: str) -> dict:
        profile = self._must_load(agent_id)
        builtin = self._profile_repo.select_first_by_flag("builtin")
        mode = _mode()
        own = self._resolver.load_own_prompts(agent_id)
        slots = []
        for slot in AGENT_PROMPT_SLOTS:
            effective = slot.is_effective_in(mode)
            slots.append({
                "slotKey": slot.name,
                "displayName": slot.display_name,
                "group": slot.group.name,
                "groupName": slot.group.display_name,
                "effective": effective,
                "inactiveReason": slot.inactive_reason if not effective else None,
                "requiredPlaceholders": sorted(slot.required_placeholders),
                "content": own.get(slot.name, "") or "",
            })
        return {
            "agentId": agent_id,
            "agentName": profile.name,
            "builtin": AgentProfileDO.is_true(profile.builtin),
            "defaultAgentName": builtin.name if builtin else None,
            "mode": mode.name,
            "slots": slots,
        }

    def save_prompt(self, agent_id: str, slot_key: str, content: str | None) -> None:
        self._must_load_editable(agent_id)
        slot = AgentPromptSlot.find(slot_key)
        if slot is None:
            raise ClientException("未知的提示词：" + slot_key)
        self._assert_placeholders_present(slot, content)
        existed = self._prompt_repo.exists(agent_id, slot.name)
        if existed:
            self._prompt_repo.update_content(
                agent_id, slot.name, content if (content and content.strip()) else None)
        elif content and content.strip():
            self._prompt_repo.insert(agent_id, slot.name, content)
        self._cache_manager.clear_cache()

    def default_prompt(self, slot_key: str) -> str:
        slot = AgentPromptSlot.find(slot_key)
        if slot is None:
            raise ClientException("未知的提示词：" + slot_key)
        builtin = self._profile_repo.select_first_by_flag("builtin")
        if builtin is None:
            return ""
        return self._resolver.load_own_prompts(builtin.id).get(slot.name, "") or ""

    # ---- 内部工具 ----
    def _load_builtin(self) -> AgentProfileDO | None:
        return self._profile_repo.select_first_by_flag("builtin")

    def _assert_placeholders_present(self, slot: AgentPromptSlot, content: str | None) -> None:
        if not content or not content.strip() or not slot.required_placeholders:
            return
        missing = sorted(p for p in slot.required_placeholders if p not in content)
        if missing:
            raise ClientException(
                f"「{slot.display_name}」缺少必需占位符：{'、'.join(missing)}")

    def _trimmed_name(self, name: str | None) -> str:
        name = (name or "").strip() if name else None
        if not name:
            raise ClientException("智能体名称不能为空")
        return name

    def _trimmed_avatar(self, avatar: str | None) -> str | None:
        avatar = (avatar or "").strip() or None
        if avatar and len(avatar) > AVATAR_MAX_LENGTH:
            raise ClientException("头像标识过长")
        return avatar

    def _assert_name_available(self, name: str, exclude_id: str | None) -> None:
        if self._profile_repo.exists_by_name(name, exclude_id):
            raise ClientException("智能体名称已存在")

    def _must_load(self, agent_id: str) -> AgentProfileDO:
        profile = self._profile_repo.select_by_id(agent_id) if agent_id else None
        if profile is None:
            raise ClientException("智能体不存在")
        return profile

    def _must_load_editable(self, agent_id: str) -> AgentProfileDO:
        profile = self._must_load(agent_id)
        if AgentProfileDO.is_true(profile.builtin):
            raise ClientException("内置智能体不可编辑或删除，如需调整请复制一份新建")
        return profile


def _trim_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip()
    return v or None
