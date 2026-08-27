"""MCP client (JSON-RPC over HTTP)."""
import httpx
from core.llm.errors import LLMError, LLMErrorType


class McpClient:
    def __init__(self, base_url, api_key=""):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    def call_tool(self, name, arguments, timeout=30.0):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        with httpx.Client(timeout=timeout) as c:
            r = c.post(self._base_url, json=payload, headers=self._headers())
            r.raise_for_status()
            return r.json()
