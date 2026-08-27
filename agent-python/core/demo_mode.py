"""体验环境只读模式：1:1 翻译 ragent DemoModeInterceptor / DemoModeProperties。"""
from __future__ import annotations

import json
import os
from typing import Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

# 1:1 对应 Java DemoModeInterceptor.DEMO_REJECT_MESSAGE
DEMO_REJECT_MESSAGE = "体验环境仅支持查询操作"
# 1:1 对应 Java BaseErrorCode.CLIENT_ERROR.code()
CLIENT_ERROR_CODE = "A000001"
# 1:1 对应 Java DemoModeInterceptor.SSE_PATHS
SSE_PATHS = frozenset({"/rag/v3/chat"})


class DemoModeProperties:
    """1:1 翻译 ragent DemoModeProperties（配置前缀 ragent，默认关闭）。"""

    def __init__(self, demo_mode: Optional[bool] = None) -> None:
        env = os.getenv("RAGENT_DEMO_MODE")
        if demo_mode is None and env is not None:
            demo_mode = env.strip().lower() in ("1", "true", "yes", "on")
        self.demo_mode: bool = bool(demo_mode)


class DemoModeMiddleware:
    """1:1 翻译 ragent DemoModeInterceptor（Spring HandlerInterceptor → ASGI 中间件）。"""

    def __init__(self, app: ASGIApp, properties: DemoModeProperties | None = None) -> None:
        self.app = app
        self.properties = properties or DemoModeProperties()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.properties.demo_mode:
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        method = request.method.upper()
        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return
        path = request.url.path
        is_sse = path in SSE_PATHS
        if method == "GET" and not is_sse:
            await self.app(scope, receive, send)
            return
        if is_sse:
            await self._write_sse_reject(send)
        else:
            await self._write_json_reject(send)

    async def dispatch(self, request: Request, call_next):
        """1:1 对应 Java preHandle；供 FastAPI 函数中间件复用。"""
        if not self.properties.demo_mode:
            return await call_next(request)
        method = request.method.upper()
        if method == "OPTIONS":
            return await call_next(request)
        path = request.url.path
        is_sse = path in SSE_PATHS
        if method == "GET" and not is_sse:
            return await call_next(request)
        if is_sse:
            return Response(
                self._sse_reject_body(),
                status_code=200,
                media_type="text/event-stream;charset=UTF-8",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )
        return JSONResponse(
            status_code=200,
            content={"code": CLIENT_ERROR_CODE, "message": DEMO_REJECT_MESSAGE, "data": None},
        )

    @staticmethod
    def _sse_reject_body() -> bytes:
        delta = json.dumps({"type": "response", "delta": DEMO_REJECT_MESSAGE},
                           ensure_ascii=False)
        return (
            "event: reject\ndata: " + delta + "\n\n"
            "event: done\ndata: \"[DONE]\"\n\n"
        ).encode("utf-8")

    @staticmethod
    async def _write_sse_reject(send: Send) -> None:
        # 1:1 对应 Java DemoModeInterceptor.writeSseReject
        delta = json.dumps({"type": "response", "delta": DEMO_REJECT_MESSAGE},
                           ensure_ascii=False)
        body = (
            "event: reject\ndata: " + delta + "\n\n"
            "event: done\ndata: \"[DONE]\"\n\n"
        ).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/event-stream;charset=UTF-8"),
                (b"cache-control", b"no-cache"),
                (b"connection", b"keep-alive"),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _write_json_reject(send: Send) -> None:
        # 1:1 对应 Java DemoModeInterceptor.writeJsonReject（Result<Void>）
        payload = json.dumps(
            {"code": CLIENT_ERROR_CODE, "message": DEMO_REJECT_MESSAGE, "data": None},
            ensure_ascii=False).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json;charset=UTF-8"),
            ],
        })
        await send({"type": "http.response.body", "body": payload})


def build_demo_mode_middleware(properties: DemoModeProperties | None = None):
    """工厂：返回可直接 app.add_middleware 包装的 DemoModeMiddleware 实例。"""
    return DemoModeMiddleware  # 由 FastAPI 以 (app) 形式实例化
