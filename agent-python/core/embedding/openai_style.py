"""OpenAI 兼容协议抽象基类：严格对应 ragent AbstractOpenAIStyleEmbeddingClient。

照搬 ragent 行为：
  - URL 拼接：{provider.url}{endpoints["embedding"]}（对应 ragent ModelUrlResolver + endpointMap）
  - Headers：Authorization: Bearer {apiKey}（Ollama 跳过）+ Content-Type: application/json
  - Body：extra 字段 + {model, input, dimensions, encoding_format}（Ollama 不加 encoding_format）
  - Response：json["data"][i]["embedding"] → List[float]
  - 错误：HTTP 4xx/5xx → ModelClientError；网络错 → NETWORK_ERROR；超时 → TIMEOUT
  - 空输入：直接返回空列表（不调 HTTP）
  - 批量：超过 max_batch_size 走切分（由调用方负责；本基类只做单次 HTTP）
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from core.embedding.base import BaseEmbeddingClient, ModelTarget
from core.embedding.errors import ModelClientError, ModelClientErrorType


def _join_url(base: str, path: str) -> str:
    """拼接 base + path，避免出现 //v1/... 的双斜杠。"""
    base = base.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


class OpenAIStyleEmbeddingClient(BaseEmbeddingClient):
    """OpenAI 兼容协议抽象基类——严格对应 ragent AbstractOpenAIStyleEmbeddingClient。

    子类只覆写 provider() / requires_api_key() / customize_request_body() / max_batch_size()。
    """

    DEFAULT_TIMEOUT_S: float = 60.0

    def do_embed_batch(self, texts: list[str], target: ModelTarget) -> list[list[float]]:
        """对应 ragent AbstractOpenAIStyleEmbeddingClient.doEmbed()。

        空输入直接返回 []（与 ragent 行为一致：空输入不走 HTTP）。
        """
        if not texts:
            return []

        endpoint = target.provider.endpoints.get("embedding", "/v1/embeddings")
        url = _join_url(target.provider.url, endpoint)

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.requires_api_key() and target.provider.api_key:
            headers["Authorization"] = f"Bearer {target.provider.api_key}"

        body: dict[str, Any] = dict(target.candidate.extra)
        body["model"] = target.candidate.model
        body["input"] = texts
        body["dimensions"] = target.candidate.dimension
        self.customize_request_body(body, target)

        timeout_s = (target.timeout_ms / 1000.0) if target.timeout_ms else self.DEFAULT_TIMEOUT_S

        try:
            resp = httpx.post(url, headers=headers, json=body, timeout=timeout_s)
        except httpx.TimeoutException as e:
            raise ModelClientError(
                f"嵌入请求超时: {url} ({timeout_s}s)",
                error_type=ModelClientErrorType.NETWORK_ERROR,  # 对齐 ragent：超时归到 NETWORK_ERROR（java 端无 TIMEOUT 枚举）
                cause=e,
            ) from e
        except httpx.HTTPError as e:
            raise ModelClientError(
                f"嵌入网络错误: {url}: {e}",
                error_type=ModelClientErrorType.NETWORK_ERROR,
                cause=e,
            ) from e

        return self._parse_response(resp, url, target)

    def _parse_response(
        self,
        resp: httpx.Response,
        url: str,
        target: ModelTarget,
    ) -> list[list[float]]:
        """解析响应——对应 ragent parseResponse()。"""
        if resp.status_code >= 400:
            err_type = ModelClientErrorType.from_http_status(resp.status_code)
            snippet = resp.text[:500] if resp.text else ""
            raise ModelClientError(
                f"嵌入 provider 错误 [{resp.status_code}] {url}: {snippet}",
                error_type=err_type,
                http_status=resp.status_code,
            )

        try:
            payload = resp.json()
        except json.JSONDecodeError as e:
            raise ModelClientError(
                f"嵌入响应不是合法 JSON: {url}",
                error_type=ModelClientErrorType.INVALID_RESPONSE,
                cause=e,
            ) from e

        data = payload.get("data")
        if not isinstance(data, list):
            raise ModelClientError(
                f"嵌入响应缺少 data 字段: {url}",
                error_type=ModelClientErrorType.INVALID_RESPONSE,
            )

        vectors: list[list[float]] = []
        for i, item in enumerate(data):
            if not isinstance(item, dict) or "embedding" not in item:
                raise ModelClientError(
                    f"嵌入响应 data[{i}] 缺少 embedding 字段",
                    error_type=ModelClientErrorType.INVALID_RESPONSE,
                )
            vec = item["embedding"]
            if not isinstance(vec, list):
                raise ModelClientError(
                    f"嵌入响应 data[{i}].embedding 不是数组",
                    error_type=ModelClientErrorType.INVALID_RESPONSE,
                )
            vectors.append([float(x) for x in vec])

        return vectors
