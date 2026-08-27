"""SSE 全局并发限流：1:1 翻译 ragent ChatQueueLimiter。

包装 FairDistributedRateLimiter，对外暴露 enqueue_or_reject：
  - 抢到许可 -> 回调 on_acquired(QueueTicket)，业务持有 QueueTicket 并在流结束后 release()
  - 等待超时 -> 回调 on_rejected（SSE 侧据此写 429 拒绝帧）
"""
from __future__ import annotations

import logging

from .fair_limiter import AcquireRequest, FairDistributedRateLimiter

_logger = logging.getLogger("sentinel.ratelimit.queue")


class QueueTicket:
    """1:1 翻译 ragent ChatQueueLimiter.QueueTicket。"""

    def __init__(self, limiter: "ChatQueueLimiter", inner) -> None:
        self._limiter = limiter
        self._inner = inner

    def release(self) -> None:
        try:
            self._inner.cancel()
        except Exception as exc:  # noqa: BLE001
            _logger.debug("QueueTicket.release 异常: %s", exc)


class ChatQueueLimiter:
    """1:1 翻译 ragent ChatQueueLimiter。"""

    def __init__(self, limiter: FairDistributedRateLimiter) -> None:
        self._limiter = limiter

    async def start(self) -> None:
        await self._limiter.start()

    async def stop(self) -> None:
        await self._limiter.stop()

    def enqueue_or_reject(self, request_id: str, max_wait_ms: int,
                          on_acquired, on_rejected) -> "object":
        def _on_acquired() -> None:
            on_acquired(QueueTicket(self, ticket))

        req = AcquireRequest(
            max_wait_millis=max_wait_ms,
            on_acquired=_on_acquired,
            on_timeout=on_rejected,
        )
        ticket = self._limiter.acquire(req)
        return ticket
