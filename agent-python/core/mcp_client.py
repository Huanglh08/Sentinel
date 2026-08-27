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
import logging
from dataclasses import dataclass

_logger = logging.getLogger(__name__)


@dataclass
class MCPConfig:
    transport: str = "sse"
    sse_url: str = "http://127.0.0.1:8000/sse"
    stdio_command: list[str] | None = None
    call_timeout_sec: float = 30.0
    list_timeout_sec: float = 10.0


class MCPToolClient:
    def __init__(self, config: MCPConfig | None = None):
        self.config = config or MCPConfig()

    async def call(self, tool_id: str, arguments: dict, timeout: float | None = None) -> tuple[bool, str]:
        """调用 MCP 工具，返回 (is_error, text)。"""
        timeout = timeout if timeout is not None else self.config.call_timeout_sec
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
            from mcp.client.stdio import stdio_client as stdio_transport
            from mcp.client.stdio import StdioServerParameters

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

    @staticmethod
    def _extract(result) -> tuple[bool, str]:
        is_error = bool(getattr(result, "isError", False))
        parts: list[str] = []
        for c in getattr(result, "content", []) or []:
            if getattr(c, "type", "") == "text":
                parts.append(getattr(c, "text", ""))
        return is_error, "\n".join(parts)
