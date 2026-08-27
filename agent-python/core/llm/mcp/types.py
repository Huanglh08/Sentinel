"""MCP types."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class McpTool:
    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    server_id: str = ""


@dataclass
class McpExtractionResult:
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
