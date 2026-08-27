"""智能体画像与提示词实体：1:1 翻译 ragent AgentProfileDO / AgentPromptDO。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class AgentProfileDO:
    """1:1 翻译 ragent AgentProfileDO（表 t_agent_profile）。"""

    id: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    avatar: Optional[str] = None
    builtin: int = 0
    active: int = 0
    create_by: Optional[str] = None
    update_by: Optional[str] = None
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None
    deleted: int = 0

    @staticmethod
    def is_true(flag: Optional[int]) -> bool:
        return flag is not None and flag == 1


@dataclass
class AgentPromptDO:
    """1:1 翻译 ragent AgentPromptDO（表 t_agent_prompt）。"""

    id: Optional[str] = None
    agent_id: Optional[str] = None
    slot_key: Optional[str] = None
    content: Optional[str] = None
    create_by: Optional[str] = None
    update_by: Optional[str] = None
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None
    deleted: int = 0
