"""智能体管理控制器：1:1 翻译 ragent AgentProfileController（FastAPI 路由）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.agent_profile.cache import AgentPromptCacheManager
from core.agent_profile.repository import AgentPromptRepository, AgentProfileRepository
from core.agent_profile.resolver import AgentPromptResolver
from core.agent_profile.service import AgentProfileAdminService, ClientException


class AgentProfileSaveRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    avatar: str | None = None


class AgentPromptSaveRequest(BaseModel):
    content: str | None = None


def build_router() -> APIRouter:
    """构造 /agents 路由，依赖 core.db 共享连接池。"""
    router = APIRouter(prefix="/agents", tags=["agents"])

    def _service() -> AgentProfileAdminService:
        profile_repo = AgentProfileRepository()
        prompt_repo = AgentPromptRepository()
        cache_manager = AgentPromptCacheManager()
        resolver = AgentPromptResolver(profile_repo, prompt_repo, cache_manager)
        return AgentProfileAdminService(profile_repo, prompt_repo, resolver, cache_manager)

    @router.get("")
    def list_agents():
        return _service().list()

    @router.post("")
    def create_agent(req: AgentProfileSaveRequest):
        return _service().create(req.name, req.description, req.avatar)

    @router.put("/{agent_id}")
    def update_agent(agent_id: str, req: AgentProfileSaveRequest):
        _service().update(agent_id, req.name, req.description, req.avatar)
        return None

    @router.delete("/{agent_id}")
    def delete_agent(agent_id: str):
        _service().delete(agent_id)
        return None

    @router.post("/{agent_id}/activate")
    def activate_agent(agent_id: str):
        _service().activate(agent_id)
        return None

    @router.get("/{agent_id}/prompts")
    def prompts(agent_id: str):
        return _service().load_prompts(agent_id)

    @router.put("/{agent_id}/prompts/{slot_key}")
    def save_prompt(agent_id: str, slot_key: str, req: AgentPromptSaveRequest):
        _service().save_prompt(agent_id, slot_key, req.content)
        return None

    @router.get("/prompt-slots/{slot_key}/default")
    def default_prompt(slot_key: str):
        return _service().default_prompt(slot_key)

    return router


# 统一把 ClientException 映射为 400
def _client_exception_handler(request, exc: ClientException):
    return HTTPException(status_code=400, detail=str(exc))
