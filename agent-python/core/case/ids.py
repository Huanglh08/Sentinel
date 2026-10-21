"""雪花 ID 生成（自 core/memory/store.py 迁出：消息持久化设施，与记忆机制无关）。

1:1 翻译 ragent MyBatis ASSIGN_ID 雪花算法，数值型主键，与 Java 一致可 Long.parseLong。
"""
from __future__ import annotations

import threading
import time


class _Snowflake:
    def __init__(self, node: int = 1) -> None:
        self._node = node & 0x3FF
        self._lock = threading.Lock()
        self._last_ts = -1
        self._seq = 0

    def next_id(self) -> int:
        with self._lock:
            ts = (int(time.time() * 1000) - 1288834974657) & 0x1FFFFFFFFFF
            if ts == self._last_ts:
                self._seq = (self._seq + 1) & 0xFFF
                if self._seq == 0:
                    while ts <= self._last_ts:
                        ts = (int(time.time() * 1000) - 1288834974657) & 0x1FFFFFFFFFF
            else:
                self._seq = 0
            self._last_ts = ts
            return (ts << 22) | (self._node << 12) | self._seq


_sf = _Snowflake()


def next_message_id() -> str:
    """生成数值型雪花 ID（字符串形式，与 Java 一致可 Long.parseLong）。"""
    return str(_sf.next_id())
