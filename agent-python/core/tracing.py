"""轻量级、无外部依赖的请求追踪实现。"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


_local = threading.local()


@dataclass(frozen=True)
class SpanContext:
    trace_id: str
    span_id: str
    parent_span_id: str | None = None


class Span(ABC):
    @abstractmethod
    def set_attribute(self, key, value): ...

    @abstractmethod
    def record_exception(self, exc): ...

    @abstractmethod
    def end(self): ...


class NoopSpan(Span):
    def __init__(self, context: SpanContext):
        self.context = context

    def set_attribute(self, key, value):
        return self

    def record_exception(self, exc):
        return self

    def end(self):
        return self


@dataclass
class ConsoleSpan(Span):
    name: str
    context: SpanContext
    kind: str = "INTERNAL"
    attributes: dict = field(default_factory=dict)
    exception: str | None = None
    started_at: float = field(default_factory=time.perf_counter)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("sentinel.trace"))

    def set_attribute(self, key, value):
        self.attributes[str(key)] = value
        return self

    def record_exception(self, exc):
        self.exception = f"{type(exc).__name__}: {exc}"
        return self

    def end(self):
        duration_ms = round((time.perf_counter() - self.started_at) * 1000, 2)
        self.logger.info(
            "span_end name=%s trace_id=%s span_id=%s duration_ms=%s attributes=%s exception=%s",
            self.name, self.context.trace_id, self.context.span_id, duration_ms, self.attributes, self.exception,
        )
        return self


class Tracer:
    def __init__(self, enabled=True):
        self.enabled = enabled

    def current(self):
        return getattr(_local, "ctx", None)

    def start_span(self, name, kind="INTERNAL"):
        parent = self.current()
        context = SpanContext(
            trace_id=parent.trace_id if parent else uuid.uuid4().hex,
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=parent.span_id if parent else None,
        )
        span = ConsoleSpan(name, context, kind) if self.enabled else NoopSpan(context)
        _local.ctx = context
        return span

    def use_span(self, span):
        previous = self.current()
        _local.ctx = span.context

        class _Scope:
            def __enter__(self_nonlocal):
                return span

            def __exit__(self_nonlocal, exc_type, exc, tb):
                if exc:
                    span.record_exception(exc)
                span.end()
                _local.ctx = previous
                return False

        return _Scope()
