"""Judge 客户端：OpenAI 兼容 /chat/completions 的轻量封装（不依赖 core.llm，避免污染生产链路）。

与生产 LLM 客户端的三点关键差异：
1. **缓存友好**：system（rubric 静态定义）永远作为 messages[0] 且逐字符稳定，
   DeepSeek 上下文硬盘缓存按前缀匹配 → 输入成本降 98%（¥1 → ¥0.02 每百万）。
   因此 rubric 文本一旦定稿就不要随意改动，改动会使缓存整体失效。
2. **确定性**：temperature=0 + 关闭思考模式 + JSON 结构化输出，保证同一输入可复现。
3. **可观测**：每次调用记录 token 用量，汇总后可精确核算 judge 成本。
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from eval.judge.config import JudgeConfig


@dataclass
class JudgeUsage:
    """judge 调用用量累计（用于成本核算）。"""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    errors: int = 0
    retries: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)

    def add(
        self,
        *,
        task: str,
        prompt_tokens: int,
        completion_tokens: int,
        retries: int = 0,
    ) -> None:
        self.calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.retries += retries
        self.details.append(
            {
                "task": task,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "retries": retries,
            }
        )

    def estimate_cost_cny(self, *, input_price: float = 1.0, output_price: float = 2.0) -> float:
        """按每百万 token 单价估算人民币成本（默认 deepseek-v4-flash 原价）。"""
        return (
            self.prompt_tokens / 1_000_000 * input_price
            + self.completion_tokens / 1_000_000 * output_price
        )


class JudgeClient:
    """同步/异步两用的 judge 调用器（内部统一走 httpx.AsyncClient）。"""

    def __init__(self, config: JudgeConfig | None = None) -> None:
        self.config = config or JudgeConfig.from_env()
        self.usage = JudgeUsage()

    # ---------- 底层调用 ----------

    def _payload(self, system: str, user: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        # 百炼非标准参数：关闭思考模式（省输出 token + 降延迟 + 减少漂移）
        if not self.config.enable_thinking:
            payload["enable_thinking"] = False
        return payload

    async def _post(self, payload: dict[str, Any]) -> tuple[str, dict[str, int]]:
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage") or {}
        tokens = {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
        }
        return content, tokens

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        """容错解析：去 Markdown 围栏 → json.loads → 正则兜底。"""
        text = (content or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", text, re.S)
            if not m:
                raise ValueError(f"judge 输出无法解析为 JSON: {text[:200]}")
            data = json.loads(m.group(0))
        if not isinstance(data, dict):
            raise ValueError(f"judge 输出不是对象: {type(data)}")
        return data

    # ---------- 对外主入口 ----------

    async def judge(
        self,
        *,
        task: str,
        system: str,
        user: str,
    ) -> dict[str, Any]:
        """执行一次判分，返回解析后的 dict（失败重试，最终失败抛异常）。"""
        if not self.config.api_key:
            raise RuntimeError(
                "未配置 judge API Key：请设置 JUDGE_API_KEY 或 BAILIAN_API_KEY / DASHSCOPE_API_KEY"
            )

        last_err: Exception | None = None
        retries = 0
        for attempt in range(self.config.max_retries):
            payload = self._payload(system, user)
            if attempt > 0:
                # 二次尝试：追加强约束提示（不改动 system，避免破坏缓存前缀）
                payload["messages"][1]["content"] = (
                    user + "\n\n【重要】上一次输出不是合法 JSON，请只输出 JSON 对象，不要任何解释。"
                )
                retries += 1
                await asyncio.sleep(self.config.retry_backoff**attempt)
            try:
                content, tokens = await self._post(payload)
                data = self._parse_json(content)
                self.usage.add(task=task, retries=retries, **tokens)
                if self.config.trace:
                    data.setdefault("_judge_meta", {})
                    data["_judge_meta"].update(
                        {"model": self.config.model, "task": task, "attempt": attempt + 1}
                    )
                return data
            except Exception as e:  # noqa: BLE001
                last_err = e
                self.usage.errors += 1
                continue

        raise RuntimeError(f"judge 调用失败（已重试 {self.config.max_retries} 次）: {last_err}")

    def run(self, **kwargs) -> dict[str, Any]:
        """同步入口（脚本里用）。"""
        return asyncio.run(self.judge(**kwargs))
