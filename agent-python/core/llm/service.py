"""LLM 服务层：严格对齐 ragent infra-ai.chat.LLMService / AbstractOpenAIStyleChatClient。

- LLMService：顶层接口（4 方法：chat/stream/chatWithTools/streamWithTools）
- OpenAIStyleLLMService：抽象基类（仿 ragent AbstractOpenAIStyleChatClient）—— OpenAI 协议通用实现
- 向后兼容：LLMClient 旧名 = OpenAIStyleLLMService
"""
from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

import httpx

from core.llm.config import AIModelProperties
from core.llm.errors import LLMError, LLMErrorType
from core.llm.model_target import ModelTarget
from core.llm.routing_executor import ModelRoutingExecutor
from core.llm.routing_service import RoutingLLMService
from core.llm.selector import ModelSelector
from core.llm.streaming import OpenAIStyleSseParser, ProbeStreamBridge


class LLMService(ABC):
    """严格对应 ragent infra-ai.chat.LLMService 接口（4 方法）。"""

    @abstractmethod
    async def chat(self, messages: list[dict], **kwargs) -> str:
        """单次非流式调用——对应 ragent doChat。"""
        ...

    @abstractmethod
    def stream(self, messages: list[dict], **kwargs) -> AsyncIterator[str]:
        """流式调用——对应 ragent doStream。"""
        ...

    async def chat_with_tools(self, messages: list[dict], tools: list[dict], **kwargs) -> dict:
        """工具调用（非流式）—— ragent 可选支持，默认未实现。"""
        raise NotImplementedError("chatWithTools 未实现（ragent 可选支持）")

    def stream_with_tools(self, messages: list[dict], tools: list[dict], **kwargs) -> AsyncIterator[dict]:
        """工具调用（流式）—— ragent 可选支持，默认未实现。"""
        raise NotImplementedError("streamWithTools 未实现（ragent 可选支持）")
        yield  # 让方法成为 generator（永不执行）


class OpenAIStyleLLMService(LLMService):
    """严格对应 ragent infra-ai.model.AbstractOpenAIStyleChatClient。"""

    DEFAULT_BASE_URL = "http://localhost:11434/v1"
    DEFAULT_MODEL = "qwen2.5:7b"

    # 子类覆写（严格对齐 ragent 4 provider 类层级）
    provider_id: str = "ollama"
    requires_api_key: bool = True  # OllamaLLMService 覆写为 False

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = (base_url or os.getenv("AGENT_LLM_BASE_URL", self.DEFAULT_BASE_URL)).rstrip("/")
        self.api_key = api_key or os.getenv("AGENT_LLM_API_KEY", "ollama")
        self.model = model or os.getenv("AGENT_LLM_MODEL", self.DEFAULT_MODEL)
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _url(self) -> str:
        return f"{self.base_url}/chat/completions"

    async def chat(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.1,
        top_p: float = 0.3,
        max_tokens: int = 1024,
    ) -> str:
        """单次非流式——严格对齐 ragent AbstractOpenAIStyleChatClient.doChat。"""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self._url(), json=payload, headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as e:
            raise LLMError(
                f"LLM chat 失败: {e.response.status_code}",
                error_type=LLMErrorType.from_http_status(e.response.status_code),
                cause=e,
            ) from e
        except Exception as e:  # noqa: BLE001
            raise LLMError(
                f"LLM chat 调用失败: {e}",
                error_type=LLMErrorType.NETWORK_ERROR,
                cause=e,
            ) from e

    async def stream(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.3,
        top_p: float = 0.9,
        max_tokens: int = 2048,
    ) -> AsyncIterator[dict]:
        """流式——严格对齐 ragent AbstractOpenAIStyleChatClient.doStream。

        用 OpenAIStyleSseParser 解析 SSE 协议，逐 token yield dict。
        yield 结构：{"type": "response", "delta": str} 或 {"type": "think", "delta": str}
        注：完整"首包探测 + 切超时"由 ProbeStreamBridge 编排（见 LLMService 子类实现）。
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "stream": True,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream(
                    "POST", self._url(), json=payload, headers=self._headers()
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        chunk = OpenAIStyleSseParser.parse_line(line.strip())
                        if chunk is None:
                            continue
                        # 优先提取 reasoning / thinking，再提取 content
                        reasoning = OpenAIStyleSseParser.extract_reasoning_delta(chunk)
                        if reasoning:
                            yield {"type": "think", "delta": reasoning}
                        delta = OpenAIStyleSseParser.extract_delta(chunk)
                        if delta:
                            yield {"type": "response", "delta": delta}
        except httpx.HTTPStatusError as e:
            raise LLMError(
                f"LLM stream 失败: {e.response.status_code}",
                error_type=LLMErrorType.from_http_status(e.response.status_code),
                cause=e,
            ) from e
        except Exception as e:  # noqa: BLE001
            raise LLMError(
                f"LLM stream 调用失败: {e}",
                error_type=LLMErrorType.NETWORK_ERROR,
                cause=e,
            ) from e


# 向后兼容
    def do_chat(self, target, request) -> str:
        url = f"{target.provider.base_url.rstrip('/')}/chat/completions"
        with httpx.Client(timeout=60.0) as c:
            r = c.post(url, json={"model": target.candidate.model or target.id, "messages": request.get("messages", []), "stream": False}, headers=self._headers())
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]


class AsyncRoutingLLMService(LLMService):
    """把 ragent 风格同步路由适配为 Agent 所需的异步 LLM 接口。"""

    def __init__(self, routing: RoutingLLMService) -> None:
        self.routing = routing

    async def chat(self, messages: list[dict], **kwargs: Any) -> str:
        request = {"messages": messages, **kwargs}
        return await __import__("asyncio").to_thread(self.routing.chat, request)

    async def stream(self, messages: list[dict], **kwargs: Any) -> AsyncIterator[dict]:
        request = {"messages": messages, **kwargs}
        thinking = bool(request.get("thinking", False))
        targets = self.routing._selector.select_chat_candidates(thinking, None, None)
        health = self.routing._executor._health_store
        last_error: Exception | None = None
        for target in targets:
            client = self.routing._resolve_client(target)
            if client is None:
                continue
            permit = health.allow_call(target.id)
            if permit.half_open_token == 0 and health.is_unavailable(target.id):
                continue
            queue: asyncio.Queue[dict | BaseException | None] = asyncio.Queue()
            first_token = False

            async def consume() -> None:
                try:
                    async for token in client.stream(
                        messages,
                        temperature=kwargs.get("temperature", 0.1),
                        top_p=kwargs.get("top_p", 0.9),
                        max_tokens=kwargs.get("max_tokens", 2048),
                    ):
                        await queue.put(token)
                    await queue.put(None)
                except BaseException as exc:  # noqa: BLE001
                    await queue.put(exc)

            task = asyncio.create_task(consume())
            try:
                while True:
                    item = await queue.get()
                    if isinstance(item, BaseException):
                        raise item
                    if item is None:
                        health.record_success(target.id, permit)
                        break
                    first_token = True
                    yield item
                return
            except asyncio.CancelledError:
                task.cancel()
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                health.record_failure(target.id, permit, exc)
                if first_token:
                    raise
            finally:
                if not task.done():
                    task.cancel()
        raise LLMError(
            f"All CHAT model candidates failed: {last_error or 'unknown'}",
            error_type=LLMErrorType.SERVER_ERROR,
            cause=last_error,
        )


LLMClient = OpenAIStyleLLMService
