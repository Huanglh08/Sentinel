"""MCP tool registry."""
from abc import ABC, abstractmethod
from core.llm.mcp.types import McpTool


class McpToolRegistry(ABC):
    @abstractmethod
    def register(self, tool): ...
    @abstractmethod
    def get(self, name): ...
    @abstractmethod
    def all(self): ...


class DefaultMcpToolRegistry(McpToolRegistry):
    def __init__(self):
        self._tools = {}

    def register(self, tool):
        self._tools[tool.name] = tool

    def get(self, name):
        return self._tools.get(name)

    def all(self):
        return list(self._tools.values())
