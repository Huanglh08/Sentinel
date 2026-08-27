"""LLM-based MCP parameter extractor."""
import json
from core.llm.mcp.types import McpExtractionResult, McpTool
from core.llm.mcp.parameter_extractor import McpParameterExtractor
from core.llm.errors import LLMError


class LLMMcpParameterExtractor(McpParameterExtractor):
    def __init__(self, llm):
        self._llm = llm

    def extract(self, user_input, tools):
        if not tools:
            return None
        prompt = self._build_prompt(user_input, tools)
        try:
            raw = self._llm.do_chat_simple([{"role": "user", "content": prompt}])
        except LLMError:
            return None
        return self._parse(raw, tools)

    def _build_prompt(self, user_input, tools):
        lines = ["你是参数提取器。从用户问题中提取工具参数。", "工具列表:"]
        for t in tools:
            lines.append(f"- {t.name}: {t.description} schema={t.parameters}")
        lines.append(f"用户输入: {user_input}")
        lines.append('输出 JSON: {"name": "<tool>", "arguments": {...}, "confidence": 0.9}')
        return "\n".join(lines)

    def _parse(self, raw, tools):
        try:
            cleaned = raw.strip().strip("`").replace("```json", "").replace("```", "").strip()
            data = json.loads(cleaned)
            return McpExtractionResult(
                tool_name=data.get("name", ""),
                arguments=data.get("arguments", {}),
                confidence=float(data.get("confidence", 0.0)),
            )
        except Exception:
            return None
