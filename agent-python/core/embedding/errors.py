"""嵌入层错误模型：严格对应 ragent infra-ai.http.ModelClientException / ModelClientErrorType。

ragent 用 7 个枚举区分错误类别：
  UNAUTHORIZED, RATE_LIMITED, SERVER_ERROR, CLIENT_ERROR, NETWORK_ERROR, INVALID_RESPONSE, PROVIDER_ERROR
并提供 fromHttpStatus() 把 HTTP 状态码映射到错误类型。Python 这边逐字对齐。
"""
from __future__ import annotations

from enum import Enum


class ModelClientErrorType(str, Enum):
    """错误类型分类——严格对应 ragent ModelClientErrorType（7 个枚举）。"""

    UNAUTHORIZED = "UNAUTHORIZED"
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_ERROR = "SERVER_ERROR"
    CLIENT_ERROR = "CLIENT_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    PROVIDER_ERROR = "PROVIDER_ERROR"

    @classmethod
    def from_http_status(cls, status: int) -> "ModelClientErrorType":
        """ragent ModelClientErrorType.fromHttpStatus：把 HTTP 状态码映射到错误类型。
        
        严格对齐 ragent：
          - 401/403 → UNAUTHORIZED（鉴权失败）
          - 429     → RATE_LIMITED（限流）
          - 5xx     → SERVER_ERROR（服务端错误）
          - 其他     → CLIENT_ERROR（含 4xx 业务错 + 未知）
        
        注：NETWORK_ERROR / INVALID_RESPONSE / PROVIDER_ERROR 由异常类型分类（来自 catch IOException /
        响应解析失败 / provider 配置错误），不从 HTTP 状态码映射。
        """
        if status in (401, 403):
            return cls.UNAUTHORIZED
        if status == 429:
            return cls.RATE_LIMITED
        if 500 <= status < 600:
            return cls.SERVER_ERROR
        return cls.CLIENT_ERROR


class ModelClientError(RuntimeError):
    """嵌入调用失败——严格对应 ragent ModelClientException。

    字段与 ragent ModelClientException 完全对齐：
      - message: 错误描述
      - error_type: 错误类型（用于熔断统计/降级决策）
      - http_status: HTTP 状态码（如果有，401/429/5xx 用于熔断判断）
      - cause: 底层异常（IOException 等）
    """

    def __init__(
        self,
        message: str,
        error_type: ModelClientErrorType = ModelClientErrorType.CLIENT_ERROR,  # 严格对齐 ragent：默认兜底 CLIENT_ERROR（java 端 errorType 默认 null，业务代码显式赋值）
        http_status: int | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.http_status = http_status
        self.__cause__ = cause
