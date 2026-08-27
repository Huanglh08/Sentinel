"""LLM 错误模型：严格对齐 ragent infra-ai.http.ModelClientException / ModelClientErrorType。

7 个枚举：UNAUTHORIZED / RATE_LIMITED / SERVER_ERROR / CLIENT_ERROR / NETWORK_ERROR / INVALID_RESPONSE / PROVIDER_ERROR
+ fromHttpStatus() HTTP 状态码映射——与嵌入层 errors.py 完全对齐。
"""
from __future__ import annotations

from enum import Enum


class LLMErrorType(str, Enum):
    """错误类型分类——严格对应 ragent ModelClientErrorType（7 个枚举）。"""

    UNAUTHORIZED = "UNAUTHORIZED"
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_ERROR = "SERVER_ERROR"
    CLIENT_ERROR = "CLIENT_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    PROVIDER_ERROR = "PROVIDER_ERROR"

    @classmethod
    def from_http_status(cls, status: int) -> "LLMErrorType":
        """严格对齐 ragent ModelClientErrorType.fromHttpStatus。"""
        if status in (401, 403):
            return cls.UNAUTHORIZED
        if status == 429:
            return cls.RATE_LIMITED
        if 500 <= status < 600:
            return cls.SERVER_ERROR
        return cls.CLIENT_ERROR


class LLMError(RuntimeError):
    """严格对应 ragent ModelClientException。"""

    def __init__(
        self,
        message: str,
        error_type: LLMErrorType = LLMErrorType.CLIENT_ERROR,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.__cause__ = cause
