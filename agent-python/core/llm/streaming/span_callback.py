"""StreamSpanCallback + ForwardingStreamCallback + StreamCallback + StreamSpan：严格对齐 ragent infra.chat.*。

- StreamCallback：3 方法 interface（onContent/onComplete/onError）
- ForwardingStreamCallback：装饰器基类（委托 + finish 钩子）
- StreamSpan：3 方法 abstract（finishSuccess/finishError/finishCancelledIfRunning）
- StreamSpanCallback：ForwardingStreamCallback 子类，把 finish 桥接到 StreamSpan
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class StreamCallback(ABC):
    """严格对应 ragent infra.chat.StreamCallback interface。"""

    @abstractmethod
    def on_content(self, chunk: str) -> None:
        """每 token 增量回调。"""
        ...

    def on_thinking(self, reasoning: str) -> None:
        """思维链增量回调（可选，默认空实现——子类可选择性覆写）。"""
        ...

    @abstractmethod
    def on_complete(self) -> None:
        """流式完成回调。"""
        ...

    @abstractmethod
    def on_error(self, error: Exception) -> None:
        """流式错误回调。"""
        ...


class ForwardingStreamCallback(StreamCallback):
    """严格对应 ragent infra.chat.ForwardingStreamCallback（装饰器基类）。"""

    def __init__(self, delegate: StreamCallback) -> None:
        self._delegate = delegate

    def on_content(self, chunk: str) -> None:
        self._delegate.on_content(chunk)

    def on_thinking(self, reasoning: str) -> None:
        self._delegate.on_thinking(reasoning)

    def on_complete(self) -> None:
        self._delegate.on_complete()
        self._on_finish(True, None)

    def on_error(self, error: Exception) -> None:
        self._delegate.on_error(error)
        self._on_finish(False, error)

    def _on_finish(self, success: bool, error: Exception | None) -> None:
        """子类覆写以桥接 finish 事件——默认空实现。"""
        ...


class StreamSpan(ABC):
    """严格对应 ragent framework.trace.RagStreamTraceSupport.StreamSpan interface。"""

    @abstractmethod
    def finish_success(self) -> None:
        """追踪成功结束。"""
        ...

    @abstractmethod
    def finish_error(self, error: Exception | None) -> None:
        """追踪错误结束。"""
        ...

    @abstractmethod
    def finish_cancelled_if_running(self) -> None:
        """若仍在 RUNNING 状态，按取消语义结束——避免 trace 行悬挂。"""
        ...


class StreamSpanCallback(ForwardingStreamCallback):
    """严格对应 ragent infra.chat.StreamSpanCallback。"""

    def __init__(self, delegate: StreamCallback, span: StreamSpan) -> None:
        super().__init__(delegate)
        self._span = span

    def _on_finish(self, success: bool, error: Exception | None) -> None:
        if success:
            self._span.finish_success()
        else:
            self._span.finish_error(error)

    def on_cancel(self) -> None:
        """取消时由调用方触发——严格对齐 ragent StreamSpanCallback.onCancel。"""
        self._span.finish_cancelled_if_running()
        # 注：ragent 这里 finishExternally(false, null)，不委托 on_error（避免重复）
