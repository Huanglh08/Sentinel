"""StreamAsyncExecutor：严格对应 ragent infra.chat.StreamAsyncExecutor。

设计（与 ragent 一致）：
  - 静态类（私有构造器 + static submit）
  - 统一处理线程池提交、拒绝兜底、取消句柄构建
  - AtomicBoolean cancelled（python 用 threading.Event 替代）+ runAsync
  - RejectedExecutionException 兜底：call.cancel() + callback.onError(SERVER_ERROR)
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Executor
from typing import Awaitable, Callable

from core.llm.streaming.cancellation import StreamCancellationHandle, StreamCancellationHandles
from core.llm.streaming.span_callback import StreamCallback
from core.llm.errors import LLMError, LLMErrorType

# stream_task: 接收 cancelled 事件，异步执行流式读取
StreamTask = Callable[[threading.Event], Awaitable[None]]
# call: 取消时调用（模拟 okhttp Call.cancel()）
StreamCall = Callable[[], None]


class StreamAsyncExecutor:
    """严格对应 ragent infra.chat.StreamAsyncExecutor（静态类）。"""

    STREAM_BUSY_MESSAGE = "流式线程池繁忙"

    @staticmethod
    def submit(
        executor: Executor,
        call: StreamCall,
        callback: StreamCallback,
        stream_task: StreamTask,
    ) -> StreamCancellationHandle:
        """严格对应 ragent StreamAsyncExecutor.submit(executor, call, callback, streamTask)。

        行为：
          - 成功提交 → 返回带 cancelled 事件的 handle（绑 call）
          - 拒绝（RejectedExecutionException）→ call() + callback.onError(SERVER_ERROR) + noop handle
        """
        cancelled = threading.Event()
        try:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
            # 在 executor 上跑 stream_task；loop.run_in_executor 包装 awaitable
            async def _runner() -> None:
                await stream_task(cancelled)
            future = loop.run_in_executor(executor, lambda: asyncio.run(_runner()) if loop.is_closed() else asyncio.ensure_future(_runner()))
            return StreamCancellationHandles.from_call(call, cancelled)
        except (RuntimeError, Exception) as ex:  # RejectedExecutionException 兜底
            call()
            if callback is not None:
                callback.on_error(LLMError(
                    StreamAsyncExecutor.STREAM_BUSY_MESSAGE,
                    error_type=LLMErrorType.SERVER_ERROR,
                    cause=ex,
                ))
            return StreamCancellationHandles.noop()
