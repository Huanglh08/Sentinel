"""评测接口：1:1 翻译 ragent eval/EvalController / EvalProperties / EvalResponse。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from fastapi import APIRouter, Query


@dataclass
class EvalProperties:
    """1:1 翻译 ragent EvalProperties（配置前缀 ragent.eval）。"""

    enabled: bool = False
    endpoint: Optional[str] = None
    token: Optional[str] = None


@dataclass
class EvalResponse:
    """1:1 翻译 ragent EvalResponse。"""

    enabled: bool = False
    endpoint: Optional[str] = None
    message: str = ""


def build_eval_router(properties: EvalProperties | None = None) -> APIRouter:
    """1:1 翻译 ragent EvalController（GET /rag/eval，受 ragent.eval.enabled 控制）。"""
    props = properties or EvalProperties(
        enabled=os.getenv("RAGENT_EVAL_ENABLED", "false").lower() == "true",
        endpoint=os.getenv("RAGENT_EVAL_ENDPOINT"),
        token=os.getenv("RAGENT_EVAL_TOKEN"),
    )
    router = APIRouter()

    @router.get("/rag/eval")
    def eval_status() -> EvalResponse:
        if not props.enabled:
            return EvalResponse(enabled=False, message="评测功能未启用")
        return EvalResponse(
            enabled=True, endpoint=props.endpoint,
            message="评测功能已启用")

    return router
