"""MCP parameter extractor interface."""
from abc import ABC, abstractmethod
from core.llm.mcp.types import McpExtractionResult, McpTool


class McpParameterExtractor(ABC):
    @abstractmethod
    def extract(self, user_input: str, tools: list[McpTool]) -> McpExtractionResult | None: ...
