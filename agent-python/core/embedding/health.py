"""熔断器：严格对应 ragent infra-ai.model.ModelHealthStore（含内嵌 CallPermit record）。

三态机：
  CLOSED    正常：失败计数 < 阈值
  OPEN      熔断：拒绝所有请求；超过 open_duration 后进入 HALF_OPEN
  HALF_OPEN 半开：放行一个探针请求；成功 → CLOSED；失败 → OPEN

对应 ragent ModelHealthStore 关键设计：
  - CallPermit(modelId, halfOpenToken) record：
      halfOpenToken == 0  → 无半开名额（拒绝）
      halfOpenToken  > 0  → 持有半开探测名额（用于互斥：同一时刻只允许一个探针）
  - allowCall(modelId)  → CallPermit（带 token）
  - recordSuccess(modelId, token) → 必须 token > 0 才校验是"探针"返回的允许
  - recordFailure(modelId, token, error) → 同上
  - probeTokenSeq 共享序号：每次新探针 token 递增
  - halfOpenInFlight: 半开探测进行中标志（互斥）
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum

from core.embedding.errors import ModelClientError, ModelClientErrorType


class CircuitState(str, Enum):
    """三态——严格对应 ragent ModelHealthStore.CircuitState。"""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass(frozen=True)
class CallPermit:
    """严格对应 ragent ModelHealthStore.CallPermit record。

    字段：
      model_id:       目标模型 id
      half_open_token: 半开探测 token；0 表示无半开名额（拒绝调用），>0 表示持有半开探测名额

    语义（与 ragent 行为一致）：
      - CLOSED 状态下调用 allowCall：返回 CallPermit(model_id, half_open_token=0)
        → 放行（半开 token=0 表示"非探针"）
      - HALF_OPEN 状态下首次调用 allowCall：返回 CallPermit(model_id, half_open_token=N)
        → 放行，并标记 inFlight（同一时刻只允许 1 个探针）
      - HALF_OPEN 状态下非首次调用（inFlight 已置位）：返回 CallPermit(model_id, half_open_token=0)
        → 拒绝（半开 token=0 表示"无名额"）
      - recordSuccess/recordFailure 接 token：
          token > 0 → 是"探针"调用，必须校验是同 token 的回调
          token == 0 → 是"普通"调用，不校验
    """
    model_id: str
    half_open_token: int = 0


class ModelHealthStore:
    """模型级熔断器——严格对应 ragent ModelHealthStore。"""

    def __init__(self, failure_threshold: int = 2, open_duration_ms: int = 30_000) -> None:
        self._failure_threshold = failure_threshold
        self._open_duration_ms = open_duration_ms
        self._lock = threading.RLock()
        self._failures: dict[str, int] = {}
        self._opened_at_ms: dict[str, float] = {}
        self._state: dict[str, CircuitState] = {}
        # 半开探测进行中标志（互斥）——严格对应 ragent halfOpenInFlight
        self._half_open_in_flight: dict[str, bool] = {}
        # 半开探测 token 序号（共享）——严格对应 ragent probeTokenSeq
        self._probe_token_seq: int = 0

    # ---------------- public API ----------------

    def allow_call(self, model_id: str) -> CallPermit:
        """严格对应 ragent ModelHealthStore.allowCall(modelId)：返回 CallPermit。

        状态机（与 ragent 一致）：
          CLOSED     → 放行（token=0）
          HALF_OPEN  → 若 inFlight=False：放行并置 inFlight，token=N（递增 probeTokenSeq）
                       若 inFlight=True：拒绝（token=0）
          OPEN       → 若 (now - opened_at) >= open_duration：进入 HALF_OPEN
                         再走 HALF_OPEN 分支（递归调用）
                       否则：拒绝（token=0）
        """
        with self._lock:
            state = self._state.get(model_id, CircuitState.CLOSED)
            if state == CircuitState.CLOSED:
                return CallPermit(model_id=model_id, half_open_token=0)

            if state == CircuitState.HALF_OPEN:
                if self._half_open_in_flight.get(model_id, False):
                    # 已有探针在进行中：拒绝
                    return CallPermit(model_id=model_id, half_open_token=0)
                # 分配新探针名额
                self._probe_token_seq += 1
                self._half_open_in_flight[model_id] = True
                return CallPermit(model_id=model_id, half_open_token=self._probe_token_seq)

            # OPEN
            opened_at = self._opened_at_ms.get(model_id, 0.0)
            if (time.monotonic() * 1000.0) - opened_at >= self._open_duration_ms:
                # 超时，进入 HALF_OPEN
                self._state[model_id] = CircuitState.HALF_OPEN
                # 释放旧 inFlight（若有），分配新探针名额
                self._half_open_in_flight[model_id] = True
                self._probe_token_seq += 1
                return CallPermit(model_id=model_id, half_open_token=self._probe_token_seq)
            return CallPermit(model_id=model_id, half_open_token=0)

    def record_success(self, model_id: str, permit: CallPermit | None = None) -> None:
        """严格对应 ragent ModelHealthStore.recordSuccess(modelId, token)。

        行为（与 ragent 一致）：
          - token > 0：必须是当前 inFlight 的同 token 才校验为"探针成功"
            → 清零失败计数、回到 CLOSED、释放 inFlight
          - token == 0：普通成功 → 清零失败计数 + 回到 CLOSED
        """
        with self._lock:
            if permit is not None and permit.half_open_token > 0:
                # 探针回调：必须当前 inFlight 且 token 匹配（递增序号天然唯一，但保险起见校验状态）
                if self._state.get(model_id) != CircuitState.HALF_OPEN:
                    return
                if not self._half_open_in_flight.get(model_id, False):
                    return
                # 释放 inFlight + 回到 CLOSED
                self._half_open_in_flight[model_id] = False
            self._failures[model_id] = 0
            self._opened_at_ms[model_id] = 0.0
            self._state[model_id] = CircuitState.CLOSED
            self._half_open_in_flight[model_id] = False

    def record_failure(
        self,
        model_id: str,
        permit: CallPermit | None = None,
        error: Exception | None = None,
    ) -> None:
        """严格对应 ragent ModelHealthStore.recordFailure(modelId, token, error)。

        关键行为（ragent：只对"非致命"错误计数；UNAUTHORIZED 视场景）：
          - UNAUTHORIZED 错误：直接 OPEN（重试无意义），与 ragent 行为一致
          - 普通失败（permitted token>0 即"探针失败"）：进入 OPEN + 释放 inFlight
          - 普通失败（token=0）：failure_count++，达到阈值进入 OPEN
        """
        with self._lock:
            is_probe = permit is not None and permit.half_open_token > 0

            if isinstance(error, ModelClientError) and error.error_type == ModelClientErrorType.UNAUTHORIZED:
                # 鉴权错：直接 OPEN（重试无意义），与 ragent 行为一致
                self._state[model_id] = CircuitState.OPEN
                self._opened_at_ms[model_id] = time.monotonic() * 1000.0
                self._half_open_in_flight[model_id] = False
                return

            if is_probe:
                # 探针失败：立即 OPEN + 释放 inFlight（不再累加 failure_count）
                self._state[model_id] = CircuitState.OPEN
                self._opened_at_ms[model_id] = time.monotonic() * 1000.0
                self._half_open_in_flight[model_id] = False
                return

            # 普通失败：累加失败计数
            count = self._failures.get(model_id, 0) + 1
            self._failures[model_id] = count
            if count >= self._failure_threshold:
                self._state[model_id] = CircuitState.OPEN
                self._opened_at_ms[model_id] = time.monotonic() * 1000.0

    def is_unavailable(self, model_id: str) -> bool:
        """严格对应 ragent ModelHealthStore.isUnavailable(modelId)。

        返回 True 表示"应跳过该 modelId 的调用"：
          - OPEN 且未到 open_duration：拒绝
          - HALF_OPEN 且 inFlight=True（已有探针在跑）：拒绝
          - 其他（CLOSED / OPEN 到期 / HALF_OPEN 无探针）：False（可用）
        """
        with self._lock:
            state = self._state.get(model_id, CircuitState.CLOSED)
            if state == CircuitState.OPEN:
                opened_at = self._opened_at_ms.get(model_id, 0.0)
                return (time.monotonic() * 1000.0) - opened_at < self._open_duration_ms
            if state == CircuitState.HALF_OPEN:
                return self._half_open_in_flight.get(model_id, False)
            return False

    def state(self, model_id: str) -> CircuitState:
        """读状态（用于调试/指标）。"""
        with self._lock:
            return self._state.get(model_id, CircuitState.CLOSED)

    def force_close(self, model_id: str) -> None:
        """强制关闭——对应 ragent ModelHealthStore.forceClose()。"""
        with self._lock:
            self._failures[model_id] = 0
            self._opened_at_ms[model_id] = 0.0
            self._state[model_id] = CircuitState.CLOSED
            self._half_open_in_flight[model_id] = False
