"""分布式公平限流器：1:1 翻译 ragent FairDistributedRateLimiter（Redis 异步版）。

严格对齐 Java 的算法与 Redis 键结构：
  - {name}:semaphore        可用许可计数（整数）
  - {name}:queue            ZSET 排队（score=seq，member=requestId）
  - {name}:queue:seq        自增序列号
  - {name}:queue:notify     Pub/Sub 通知通道
  - {name}:entry:{id}       存活标记（SET with TTL = 等待预算 + 5s 缓冲）
  - {name}:permit:{uuid}    已发放许可（SET with TTL = leaseSeconds，租约自动过期）

claim 用与 Java 完全一致的 Lua 脚本（queue_claim_atomic.lua）做原子出队 + 僵尸清理。
票据状态机 PENDING/GRANTED/TIMED_OUT/CANCELLED 与 Java 一致，终态互斥、业务回调最多触发一次。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid

_logger = logging.getLogger("sentinel.ratelimit.fair")

_CLAIM_LUA = """
-- 当请求位于允许的队头窗口内时，进行出队 claim；同时清理过期僵尸条目
local queueKey = KEYS[1]
local requestId = ARGV[1]
local maxRank = tonumber(ARGV[2])
local entryPrefix = ARGV[3]
local slack = 16
local headEntries = redis.call('ZRANGE', queueKey, 0, maxRank + slack - 1)
local liveRank = -1
local liveCount = 0
for i = 1, #headEntries do
    local member = headEntries[i]
    if redis.call('EXISTS', entryPrefix .. member) == 1 then
        if member == requestId then liveRank = liveCount end
        liveCount = liveCount + 1
    else
        redis.call('ZREM', queueKey, member)
    end
end
if liveRank < 0 or liveRank >= maxRank then return {0} end
local score = redis.call('ZSCORE', queueKey, requestId)
redis.call('ZREM', queueKey, requestId)
redis.call('DEL', entryPrefix .. requestId)
return {1, score}
"""

_ENTRY_TTL_BUFFER_MS = 5_000


class AcquireRequest:
    """1:1 翻译 ragent FairDistributedRateLimiter.AcquireRequest。"""

    def __init__(self, max_wait_millis: int, on_acquired, on_timeout,
                 on_acquired_executor=None, cancel_binder=None):
        if max_wait_millis <= 0:
            raise ValueError("maxWaitMillis must be > 0")
        self.max_wait_millis = max_wait_millis
        self.on_acquired = on_acquired
        self.on_timeout = on_timeout
        self.on_acquired_executor = on_acquired_executor
        self.cancel_binder = cancel_binder


class FairDistributedRateLimiter:
    """1:1 翻译 ragent FairDistributedRateLimiter（redis.asyncio 实现）。"""

    def __init__(self, name: str, redis_url: str | None = None,
                 max_permits: int = 16, lease_seconds: int = 120,
                 poll_interval_ms: int = 200) -> None:
        self.name = name
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
        self.max_permits = max_permits
        self.lease_seconds = lease_seconds
        self.poll_interval_ms = max(poll_interval_ms, 50)
        self._redis = None
        self._lua = None
        self._pubsub = None
        self._listener_task = None
        self._pollers: dict[str, asyncio.Task] = {}
        self._started = False

    # ---- Redis 懒加载 ----
    @property
    def redis(self):
        if self._redis is None:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(self.redis_url, decode_responses=True)
        return self._redis

    def _sem_key(self):
        return f"{self.name}:semaphore"

    def _queue_key(self):
        return f"{self.name}:queue"

    def _seq_key(self):
        return f"{self.name}:queue:seq"

    def _notify_key(self):
        return f"{self.name}:queue:notify"

    def _entry_prefix(self):
        return f"{self.name}:entry:"

    def _permit_key(self, pid: str):
        return f"{self.name}:permit:{pid}"

    # ---- 生命周期 ----
    async def start(self) -> None:
        if self._started:
            return
        await self.redis.set(self._sem_key(), self.max_permits, nx=True)
        self._lua = self.redis.register_script(_CLAIM_LUA)
        self._pubsub = self.redis.pubsub()
        await self._pubsub.subscribe(self._notify_key())
        self._listener_task = asyncio.create_task(self._listen())
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        if self._listener_task:
            self._listener_task.cancel()
        if self._pubsub:
            await self._pubsub.unsubscribe(self._notify_key())
        for task in self._pollers.values():
            task.cancel()
        self._pollers.clear()
        self._started = False

    async def _listen(self) -> None:
        try:
            async for _msg in self._pubsub.listen():
                self._fire_pollers()
        except asyncio.CancelledError:
            return

    def _fire_pollers(self) -> None:
        for task in list(self._pollers.values()):
            if not task.done():
                task.cancel()

    # ---- 抢占入口 ----
    def acquire(self, req: AcquireRequest) -> "Ticket":
        ticket = Ticket(req, self)
        if req.cancel_binder is not None:
            req.cancel_binder(ticket.cancel)
        # entry 存活标记必须先于入队写入
        asyncio.create_task(self._set_entry(ticket.request_id, req.max_wait_millis))
        asyncio.create_task(self._enqueue(ticket))
        return ticket

    async def _set_entry(self, request_id: str, max_wait_ms: int) -> None:
        ttl = max(int(max_wait_ms), 1) + _ENTRY_TTL_BUFFER_MS
        try:
            await self.redis.set(self._entry_prefix() + request_id, "1", px=ttl)
        except Exception as exc:  # noqa: BLE001
            _logger.debug("[%s] 写入 entry 标记失败 %s: %s", self.name, request_id, exc)

    async def _enqueue(self, ticket: "Ticket") -> None:
        seq = await self.redis.incr(self._seq_key())
        await self.redis.zadd(self._queue_key(), {ticket.request_id: seq})
        if await ticket.try_acquire_if_ready():
            return
        self._schedule_poll(ticket)

    def _schedule_poll(self, ticket: "Ticket") -> None:
        loop = asyncio.get_event_loop()
        task = loop.create_task(self._poll_loop(ticket))
        self._pollers[ticket.request_id] = task

    async def _poll_loop(self, ticket: "Ticket") -> None:
        try:
            while ticket.is_pending():
                if time.monotonic() * 1000 > ticket.deadline_ms:
                    ticket.timeout()
                    return
                if await ticket.try_acquire_if_ready():
                    return
                await asyncio.sleep(self.poll_interval_ms / 1000)
        finally:
            self._pollers.pop(ticket.request_id, None)

    # ---- 许可操作 ----
    async def _try_acquire_permit(self) -> str | None:
        pid = uuid.uuid4().hex
        # 原子：avail>0 则 DECR 并写入带租约的 permit 键
        script = """
        local avail = tonumber(redis.call('GET', KEYS[1]) or '0')
        if avail > 0 then
            redis.call('DECR', KEYS[1])
            redis.call('SET', KEYS[2], '1', 'EX', tonumber(ARGV[1]))
            return 1
        end
        return 0
        """
        ok = await self.redis.eval(script, 2, self._sem_key(), self._permit_key(pid), self.lease_seconds)
        return pid if ok == 1 else None

    async def _release_permit(self, pid: str) -> None:
        try:
            if await self.redis.delete(self._permit_key(pid)):
                await self.redis.incr(self._sem_key())
            await self._publish_notify()
        except Exception as exc:  # noqa: BLE001
            _logger.debug("[%s] 释放 permit 失败 %s: %s", self.name, pid, exc)

    async def _available_permits(self) -> int:
        val = await self.redis.get(self._sem_key())
        return int(val) if val else 0

    async def _claim_if_ready(self, request_id: str, avail: int):
        result = await self._lua(keys=[self._queue_key()], args=[
            request_id, str(avail), self._entry_prefix()])
        if not result or int(result[0]) != 1:
            return -1
        return int(float(result[1])) if len(result) >= 2 else -1

    async def _publish_notify(self) -> None:
        try:
            await self.redis.publish(self._notify_key(), "permit_changed")
        except Exception:  # noqa: BLE001
            pass

    async def _delete_entry(self, request_id: str) -> None:
        try:
            await self.redis.delete(self._entry_prefix() + request_id)
        except Exception:  # noqa: BLE001
            pass

    async def _remove_from_queue(self, request_id: str) -> bool:
        return bool(await self.redis.zrem(self._queue_key(), request_id))


# ---------------------------------------------------------------------------
# 票据状态机（1:1 翻译 Java Ticket）
# ---------------------------------------------------------------------------
from enum import Enum


class _State(Enum):
    PENDING = "PENDING"
    GRANTED = "GRANTED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class Ticket:
    def __init__(self, req: AcquireRequest, limiter: FairDistributedRateLimiter) -> None:
        self.req = req
        self.limiter = limiter
        self.request_id = uuid.uuid4().hex
        self.deadline_ms = time.monotonic() * 1000 + req.max_wait_millis
        self._state = _State.PENDING
        self._permit: str | None = None

    def is_pending(self) -> bool:
        return self._state == _State.PENDING

    def cancel(self) -> None:
        if self._state == _State.PENDING:
            self._state = _State.CANCELLED
        asyncio.create_task(self._cleanup())

    def timeout(self) -> None:
        if not (self._state == _State.PENDING and self._state.value == "PENDING"):
            # 用 compare_and_set 语义
            pass
        if self._cas(_State.PENDING, _State.TIMED_OUT):
            asyncio.create_task(self._cleanup())
            self._submit(self.req.on_timeout)

    def _cas(self, expect: _State, target: _State) -> bool:
        # 单线程 asyncio 下直接比较赋值即可（无并发）
        if self._state == expect:
            self._state = target
            return True
        return False

    async def try_acquire_if_ready(self) -> bool:
        if not self.is_pending():
            return False
        avail = await self.limiter._available_permits()
        if avail <= 0:
            return False
        claimed = await self.limiter._claim_if_ready(self.request_id, avail)
        if claimed < 0:
            return False
        pid = await self.limiter._try_acquire_permit()
        if pid is None:
            # 队头但无 permit：重入队保留位次
            await self.limiter._set_entry(self.request_id, max(1, self.deadline_ms - time.monotonic() * 1000))
            seq = await self.limiter.redis.incr(self.limiter._seq_key())
            await self.limiter.redis.zadd(self.limiter._queue_key(), {self.request_id: seq})
            await self.limiter._publish_notify()
            if not self.is_pending():
                await self.limiter._remove_from_queue(self.request_id)
                await self.limiter._delete_entry(self.request_id)
            return False
        if not self.is_pending():
            await self.limiter._release_permit(pid)
            await self.limiter._publish_notify()
            return False
        await self.limiter._publish_notify()
        return self._grant(pid)

    def _grant(self, pid: str) -> bool:
        self._permit = pid
        if not self._cas(_State.PENDING, _State.GRANTED):
            if self._permit == pid:
                self._permit = None
                asyncio.create_task(self.limiter._release_permit(pid))
                asyncio.create_task(self.limiter._publish_notify())
            return False

        def wrapped():
            try:
                self.req.on_acquired()
            finally:
                asyncio.create_task(self._release_held())

        if self.req.on_acquired_executor is not None:
            try:
                self.req.on_acquired_executor.submit(wrapped)
                return True
            except Exception:  # noqa: BLE001
                asyncio.create_task(self._release_held())
                self._submit(self.req.on_timeout)
                return False
        else:
            wrapped()
            return True

    async def _release_held(self) -> None:
        pid = self._permit
        self._permit = None
        if pid is not None:
            await self.limiter._release_permit(pid)

    async def _cleanup(self) -> None:
        await self.limiter._remove_from_queue(self.request_id)
        await self.limiter._delete_entry(self.request_id)
        if self._state != _State.GRANTED:
            pid = self._permit
            self._permit = None
            if pid is not None:
                await self.limiter._release_permit(pid)
        self._pollers_pop()

    def _pollers_pop(self) -> None:
        self.limiter._pollers.pop(self.request_id, None)

    def _submit(self, runnable) -> None:
        if self.req.on_acquired_executor is not None:
            try:
                self.req.on_acquired_executor.submit(runnable)
            except Exception:  # noqa: BLE001
                _logger.warning("[%s] 回调提交失败", self.limiter.name)
        else:
            runnable()
