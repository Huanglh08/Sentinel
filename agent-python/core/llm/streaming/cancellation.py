"""StreamCancellationHandle + StreamCancellationHandles：严格对应 ragent infra.chat.*。

- StreamCancellationHandle：1 方法 interface（cancel，幂等）
- StreamCancellationHandles：静态工厂（fromCall/noop）
  - fromCall(call, cancelled)：包装底层 call（httpx/okhttp）+ cancelled 标志
  - noop()：空操作句柄（拒绝兜底时返回）
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Callable


class StreamCancellationHandle(ABC):
    """严格对应 ragent infra.chat.StreamCancellationHandle interface。"""

    @abstractmethod
    def cancel(self) -> None:
        """幂等取消——多次调用不抛异常。"""
        ...


class StreamCancellationHandles:
    """严格对应 ragent infra.chat.StreamCancellationHandles（静态工厂）。"""

    @staticmethod
    def from_call(call: Callable[[], None], cancelled: threading.Event) -> StreamCancellationHandle:
        """严格对应 ragent StreamCancellationHandles.fromOkHttp(call, cancelled)。"""
        return _HttpCallHandle(call, cancelled)

    @staticmethod
    def noop() -> StreamCancellationHandle:
        """严格对应 ragent StreamCancellationHandles.noop()。"""
        return _NoopHandle()


class _HttpCallHandle(StreamCancellationHandle):
    """包装底层 call + cancelled 事件。"""

    def __init__(self, call: Callable[[], None], cancelled: threading.Event) -> None:
        self._call = call
        self._cancelled = cancelled

    def cancel(self) -> None:
        if not self._cancelled.is_set():
            self._cancelled.set()
            try:
                self._call()
            except Exception:
                pass


class _NoopHandle(StreamCancellationHandle):
    """空操作句柄——拒绝兜底时返回。"""
    def cancel(self) -> None:
        pass
