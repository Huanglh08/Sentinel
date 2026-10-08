"""OpenAI 风格 SSE 解析器：严格对应 ragent infra-ai.model.OpenAIStyleSseParser。

关键设计（与 ragent 一致）：
  - parseLine(line)：去掉 'data:' 前缀后解析 JSON；遇 '[DONE]' 返 None
  - extractDelta(chunk)：从 chunk.choices[0].delta.content 提取增量文本
"""
from __future__ import annotations
import logging

import json
from typing import Any

_logger = logging.getLogger(__name__)


class OpenAIStyleSseParser:
    """严格对应 ragent OpenAIStyleSseParser（纯静态方法工具类）。"""

    @staticmethod
    def parse_line(line: str) -> dict[str, Any] | None:
        """解析一行 SSE 数据（去掉 'data:' 前缀后的 JSON）。

        对应 ragent OpenAIStyleSseParser.parseLine。
        """
        if not line:
            return None
        # 去掉 "data:" 前缀（如果还在）
        if line.startswith("data:"):
            line = line[len("data:"):].strip()
        if not line or line == "[DONE]":
            return None
        try:
            return json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def extract_delta(chunk: dict[str, Any]) -> str | None:
        """从 OpenAI 风格响应 chunk 提取增量文本。

        严格对齐 ragent OpenAIStyleSseParser.extractDelta：
          chunk.choices[0].delta.content
        """
        try:
            choices = chunk.get("choices")
            if not choices:
                return None
            delta = choices[0].get("delta", {})
            content = delta.get("content")
            return content if content else None
        except (KeyError, IndexError, TypeError, AttributeError):
            return None

    @staticmethod
    def extract_reasoning_delta(chunk: dict[str, Any]) -> str | None:
        """提取 reasoning_content / thinking_content delta（对应 ragent 思维链分离）。"""
        try:
            choices = chunk.get("choices")
            if not choices:
                return None
            delta = choices[0].get("delta", {})
            # DeepSeek-R1 等模型用 reasoning_content；部分厂商用 thinking_content
            r = delta.get("reasoning_content") or delta.get("thinking_content")
            return r if r else None
        except (KeyError, IndexError, TypeError, AttributeError):
            return None

    @staticmethod
    def extract_reasoning(chunk):
        try:
            choices = chunk.get("choices")
            if not choices:
                return None
            delta = choices[0].get("delta", {})
            r = delta.get("reasoning_content")
            return r if r else None
        except Exception as e:  # noqa: BLE001
            _logger.debug("SSE chunk 推理内容解析失败: %s", e)
            return None

    @staticmethod
    def is_completed(chunk):
        try:
            choices = chunk.get("choices")
            if not choices:
                return False
            return bool(choices[0].get("finish_reason"))
        except Exception as e:  # noqa: BLE001
            _logger.debug("SSE chunk 完成标记解析失败: %s", e)
            return False
