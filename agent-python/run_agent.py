"""Agent 服务入口：替代 `python -m uvicorn server:app`。

Windows 下 uvicorn 0.52 用 loop_factory 直接返回 ProactorEventLoop（不读事件循环策略），
而异步 psycopg（案件图引擎的 Postgres 检查点）只支持 SelectorEventLoop——
直接用 uvicorn 启动会导致检查点静默降级为无持久化。故在此覆写其循环工厂；
Linux/macOS 路径不受影响。server.py 内的策略切换保留，覆盖非 uvicorn 入口。
"""
from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":
    import uvicorn.loops.asyncio as _uv_loop

    _uv_loop.asyncio_loop_factory = lambda use_subprocess=False: asyncio.SelectorEventLoop

import uvicorn

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8001)
