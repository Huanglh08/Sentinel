"""MCP 工具调用客户端：sse/stdio transport，支持 server.py 直接调用。

差异说明：本文件早于 core.llm.mcp.McpClient（位于 core/llm/mcp/client.py），保留为 server.py
实际 import 的"老"客户端。完整 1:1 翻译 ragent 的 MCP 链路已迁移到 core/llm/mcp/：
  - core.llm.mcp.McpToolRegistry        (白名单)
  - core.llm.mcp.McpClientToolExecutor   (执行器)
  - core.llm.mcp.LLMMcpParameterExtractor (提参器)
  - core.llm.mcp.McpClient               (底层 JSON-RPC)

降级：调用失败 / 超时 → isError=true，编排层把信息塞进 text，不崩整条链路。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass

_logger = logging.getLogger(__name__)


@dataclass
class MCPConfig:
    transport: str = "sse"
    sse_url: str = "http://127.0.0.1:8000/sse"
    streamable_url: str = "http://127.0.0.1:9099/mcp"
    stdio_command: list[str] | None = None
    call_timeout_sec: float = 30.0
    list_timeout_sec: float = 10.0

    @classmethod
    def from_env(cls) -> "MCPConfig":
        """环境变量覆盖默认值（不设环境变量时行为与硬编码默认值完全一致）。"""
        cfg = cls()
        cfg.transport = os.getenv("MCP_TRANSPORT", cfg.transport)
        cfg.sse_url = os.getenv("MCP_SSE_URL", cfg.sse_url)
        cfg.streamable_url = os.getenv("MCP_STREAMABLE_URL", cfg.streamable_url)
        return cfg


class MCPToolClient:
    def __init__(self, config: MCPConfig | None = None):
        self.config = config if config is not None else MCPConfig.from_env()

    async def call(self, tool_id: str, arguments: dict, timeout: float | None = None) -> tuple[bool, str]:
        """调用 MCP 工具，返回 (is_error, text)。"""
        timeout = timeout if timeout is not None else self.config.call_timeout_sec
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
            from mcp.client.stdio import stdio_client as stdio_transport
            from mcp.client.stdio import StdioServerParameters

            if self.config.transport == "streamable":
                result = await asyncio.wait_for(
                    self._streamable_rpc("tools/call", {"name": tool_id, "arguments": arguments}),
                    timeout=timeout,
                )
                payload = result or {}
                if payload.get("error"):
                    return True, f"[MCP 工具 {tool_id} 调用失败: {payload['error']}]"
                res = payload.get("result") or {}
                parts = [
                    str(c.get("text", ""))
                    for c in res.get("content", []) or []
                    if isinstance(c, dict) and c.get("type") == "text"
                ]
                return bool(res.get("isError", False)), "\n".join(parts)
            if self.config.transport == "sse":
                async with sse_client(self.config.sse_url) as (r, w):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
                        result = await asyncio.wait_for(
                            session.call_tool(tool_id, arguments),
                            timeout=timeout,
                        )
                        return self._extract(result)
            else:
                cmd = self.config.stdio_command or ["python", "mcp-server-py/server.py"]
                params = StdioServerParameters(command=cmd[0], args=cmd[1:])
                async with stdio_transport(params) as (r, w):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
                        result = await asyncio.wait_for(
                            session.call_tool(tool_id, arguments),
                            timeout=timeout,
                        )
                        return self._extract(result)
        except Exception as e:  # noqa: BLE001
            return True, f"[MCP 工具 {tool_id} 调用失败: {e}]"

    async def get_tool_schema(self, tool_name: str) -> dict | None:
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
            from mcp.client.stdio import stdio_client as stdio_transport
            from mcp.client.stdio import StdioServerParameters

            if self.config.transport == "streamable":
                payload = await asyncio.wait_for(
                    self._streamable_rpc("tools/list", {}),
                    timeout=self.config.list_timeout_sec,
                )
                tools = (payload or {}).get("result", {}).get("tools", []) or []
                for t in tools:
                    if isinstance(t, dict) and t.get("name") == tool_name:
                        return t.get("inputSchema")
            if self.config.transport == "sse":
                async with sse_client(self.config.sse_url) as (r, w):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
                        tools = await asyncio.wait_for(
                            session.list_tools(),
                            timeout=self.config.list_timeout_sec,
                        )
                        for t in getattr(tools, "tools", []) or []:
                            if getattr(t, "name", "") == tool_name:
                                return getattr(t, "inputSchema", None)
            else:
                cmd = self.config.stdio_command or ["python", "mcp-server-py/server.py"]
                params = StdioServerParameters(command=cmd[0], args=cmd[1:])
                async with stdio_transport(params) as (r, w):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
                        tools = await asyncio.wait_for(
                            session.list_tools(),
                            timeout=self.config.list_timeout_sec,
                        )
                        for t in getattr(tools, "tools", []) or []:
                            if getattr(t, "name", "") == tool_name:
                                return getattr(t, "inputSchema", None)
        except Exception as e:  # noqa: BLE001
            return None
        return None

    async def list_tools(self) -> list[dict]:
        """列出全部工具，返回 [{name, description, inputSchema}]。失败返回空列表由调用方降级。"""
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
            from mcp.client.stdio import stdio_client as stdio_transport
            from mcp.client.stdio import StdioServerParameters

            if self.config.transport == "streamable":
                payload = await asyncio.wait_for(
                    self._streamable_rpc("tools/list", {}),
                    timeout=self.config.list_timeout_sec,
                )
                tools = (payload or {}).get("result", {}).get("tools", []) or []
                return [
                    {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "inputSchema": t.get("inputSchema") or {},
                    }
                    for t in tools
                    if isinstance(t, dict) and t.get("name")
                ]
            if self.config.transport == "sse":
                async with sse_client(self.config.sse_url) as (r, w):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
                        tools = await asyncio.wait_for(
                            session.list_tools(),
                            timeout=self.config.list_timeout_sec,
                        )
                        return self._normalize_sdk_tools(tools)
            else:
                cmd = self.config.stdio_command or ["python", "mcp-server-py/server.py"]
                params = StdioServerParameters(command=cmd[0], args=cmd[1:])
                async with stdio_transport(params) as (r, w):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
                        tools = await asyncio.wait_for(
                            session.list_tools(),
                            timeout=self.config.list_timeout_sec,
                        )
                        return self._normalize_sdk_tools(tools)
        except Exception as e:  # noqa: BLE001
            _logger.warning("[mcp] list_tools 失败: %s", e)
            return []

    @staticmethod
    def _normalize_sdk_tools(tools) -> list[dict]:
        result: list[dict] = []
        for t in getattr(tools, "tools", []) or []:
            name = getattr(t, "name", "")
            if not name:
                continue
            result.append(
                {
                    "name": name,
                    "description": getattr(t, "description", "") or "",
                    "inputSchema": getattr(t, "inputSchema", None) or {},
                }
            )
        return result

    @staticmethod
    def _extract(result) -> tuple[bool, str]:
        is_error = bool(getattr(result, "isError", False))
        parts: list[str] = []
        for c in getattr(result, "content", []) or []:
            if getattr(c, "type", "") == "text":
                parts.append(getattr(c, "text", ""))
        return is_error, "\n".join(parts)

    async def _streamable_rpc(self, method: str, params: dict) -> dict | None:
        """Streamable-HTTP transport：initialize 握手 → initialized 通知 → JSON-RPC 请求。"""
        import httpx

        url = self.config.streamable_url
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        async with httpx.AsyncClient(timeout=self.config.call_timeout_sec) as client:
            init_resp = await client.post(
                url,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "sentinel-agent", "version": "0.0.1"},
                    },
                },
            )
            init_resp.raise_for_status()
            session_id = init_resp.headers.get("mcp-session-id", "")
            if session_id:
                await client.post(
                    url,
                    headers=headers,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
            req_headers = {**headers, "mcp-session-id": session_id} if session_id else headers
            resp = await client.post(
                url,
                headers=req_headers,
                json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}},
            )
            resp.raise_for_status()
            return self._parse_rpc_response(resp)

    @staticmethod
    def _parse_rpc_response(resp) -> dict | None:
        """解析 JSON 或 SSE(text/event-stream) 两种格式的 JSON-RPC 响应。"""
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            for line in resp.text.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and "id" in payload:
                    return payload
            return None
        if not resp.text:
            return None
        return resp.json()
