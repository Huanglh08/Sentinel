"""MCP tool executor."""
from abc import ABC, abstractmethod
from typing import Any
from core.llm.mcp.types import McpTool


class McpToolExecutor(ABC):
    @abstractmethod
    def execute(self, tool: McpTool, arguments: dict) -> Any: ...


class McpClientToolExecutor(McpToolExecutor):
    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    def bind(self, client: Any) -> None:
        self._client = client

    def execute(self, tool: McpTool, arguments: dict) -> Any:
        if self._client is None:
            raise RuntimeError("MCP client not bound")
        return self._client.call_tool(tool.name, arguments)
