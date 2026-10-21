"""会话状态卡：agent 自主记忆（note-taking / "案件即记忆"模式的泛化实现）。

设计要点（对标业界）：
- 记什么由 agent 每轮自主决策：LLM 在回答末尾按约定输出 <memory_delta> 块，
  框架不代替 agent 决定记忆内容（区别于 ragent 的定时滚动摘要）。
- 状态卡是结构化 JSON（可审计、可入库），字段任务无关（泛化，不绑定特定领域）：
    task        当前任务/目标一句话
    facts       已确认的事实或结论（可附来源线索）
    open_items  待验证/待解决的开放问题
    decisions   已做的关键决策及理由
    next        建议的下一步
- 与历史窗口互补：history 管"聊过什么"，状态卡管"查到了什么、要干什么"。
- 失败静默降级：DB 不可用或 delta 解析失败时不影响主回答链路。

存储：与业务同库的 t_session_memory（session_id 主键，state JSONB）。
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

from core.db import get_pool

_logger = logging.getLogger("sentinel.session_memory")

# 状态卡默认骨架（任务无关；新会话首次注入时 agent 按实际任务填充）
DEFAULT_STATE: dict[str, Any] = {
    "task": "",
    "facts": [],
    "open_items": [],
    "decisions": [],
    "next": "",
}

# 防膨胀护栏：LLM 输出不可信，写入前统一裁剪
_LIST_FIELDS = ("facts", "open_items", "decisions")
_STR_FIELDS = ("task", "next")
_LIST_MAX_ITEMS = 20
_LIST_ITEM_MAX_CHARS = 300
_STR_MAX_CHARS = 200

_STATE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS t_session_memory (
    session_id VARCHAR(128) PRIMARY KEY,
    state      JSONB        NOT NULL,
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT now()
)
"""


def default_state() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_STATE)


def sanitize_state(state: dict[str, Any]) -> dict[str, Any]:
    """裁剪到安全边界：未知字段丢弃，超长截断，列表去重限量。"""
    out = default_state()
    if not isinstance(state, dict):
        return out
    for key in _STR_FIELDS:
        val = state.get(key)
        if isinstance(val, str):
            out[key] = val.strip()[:_STR_MAX_CHARS]
    for key in _LIST_FIELDS:
        val = state.get(key)
        if not isinstance(val, list):
            continue
        seen: set[str] = set()
        items: list[str] = []
        for item in val:
            if not isinstance(item, str):
                continue
            text = item.strip()[:_LIST_ITEM_MAX_CHARS]
            if text and text not in seen:
                seen.add(text)
                items.append(text)
            if len(items) >= _LIST_MAX_ITEMS:
                break
        out[key] = items
    return out


def merge_delta(state: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """合并 agent 决策的记忆增量（原地修改并返回 state）。

    delta 字段语义（协议见 build_state_card_prompt）：
    - 字符串字段（task/next）：非空即整体替换；
    - 列表字段：{"add": [...], "remove": [...]} 精准增删，或直接给数组视为 add。
    """
    if not isinstance(delta, dict):
        return state
    for key in _STR_FIELDS:
        val = delta.get(key)
        if isinstance(val, str) and val.strip():
            state[key] = val.strip()[:_STR_MAX_CHARS]
    for key in _LIST_FIELDS:
        val = delta.get(key)
        adds: list[str] = []
        removes: list[str] = []
        if isinstance(val, dict):
            adds = [x for x in val.get("add", []) if isinstance(x, str)]
            removes = [x for x in val.get("remove", []) if isinstance(x, str)]
        elif isinstance(val, list):
            adds = [x for x in val if isinstance(x, str)]
        if not adds and not removes:
            continue
        items = [x for x in state.get(key, []) if isinstance(x, str)]
        for r in removes:
            items = [x for x in items if x != r.strip()]
        for a in adds:
            text = a.strip()[:_LIST_ITEM_MAX_CHARS]
            if text and text not in items:
                items.append(text)
        state[key] = items[-_LIST_MAX_ITEMS:]
    return state


def build_state_card_prompt(state: dict[str, Any]) -> str:
    """渲染注入 system 的状态卡文本 + memory_delta 输出协议。"""
    card = json.dumps(state, ensure_ascii=False, indent=1)
    return (
        "## 会话状态卡（持久记忆）\n"
        "以下是本会话此前累积的结构化状态，是跨越窗口裁剪依然可靠的记忆来源，"
        "回答时必须与其保持一致（已确认的事实不要反悔，已排除的方向不要重复调查）：\n"
        f"{card}\n\n"
        "## 记忆更新协议\n"
        "若本轮产生了值得长期记住的新信息（确认的事实、解决的开放问题、新的决策、"
        "任务目标变化），在回答全部内容结束之后、作为输出的最后一段，"
        "严格按以下格式输出一次记忆增量；没有值得记住的变化时绝对不要输出该块：\n"
        '<memory_delta>{"task": "...", "facts": {"add": ["..."], "remove": ["..."]},'
        ' "open_items": {"add": ["..."], "remove": ["..."]},'
        ' "decisions": {"add": ["..."]}, "next": "..."}</memory_delta>\n'
        "规则：字段全部可选，只写有变化的；add/remove 只对你想增删的条目负责；"
        "该块不会展示给用户。"
    )


class MemoryDeltaFilter:
    """流式输出上的 memory_delta 捕获器。

    - 正常内容实时透传（尾部最多滞留 len(START)-1 个字符，流结束由 finish() 补发）；
    - 一旦出现 <memory_delta> 开始标签即进入捕获模式，后续内容不再透传；
    - captured_text 为剥离标签后的增量原文（可能为空 / 可能 JSON 非法，由调用方解析）。
    """

    START = "<memory_delta>"
    END = "</memory_delta>"

    def __init__(self) -> None:
        self._tail_buf = ""
        self._capturing = False
        self._captured = ""

    @property
    def captured_text(self) -> str:
        return self._captured

    def feed(self, delta: str) -> str:
        """喂入一段生成 token，返回应当透传给用户的部分（可能为空串）。"""
        if self._capturing:
            self._captured += delta
            if self.END in self._captured:  # 闭合后停止积累
                self._captured = self._captured.split(self.END, 1)[0]
                self._capturing = False
            return ""
        text = self._tail_buf + delta
        pos = text.find(self.START)
        if pos >= 0:
            self._capturing = True
            self._captured = text[pos + len(self.START):]
            if self.END in self._captured:
                self._captured = self._captured.split(self.END, 1)[0]
                self._capturing = False
            self._tail_buf = ""
            return text[:pos]
        keep = len(self.START) - 1
        if len(text) > keep:
            self._tail_buf = text[-keep:]
            return text[:-keep]
        self._tail_buf = text
        return ""

    def finish(self) -> str:
        """流结束：返回缓冲中滞留的透传尾巴。"""
        rest, self._tail_buf = self._tail_buf, ""
        return rest


class SessionMemory:
    """状态卡持久化：t_session_memory 表 CRUD（psycopg 同步池，短查询）。"""

    def ensure_table(self) -> None:
        with get_pool().connection() as conn:
            conn.execute(_STATE_TABLE_DDL)

    def load(self, session_id: str) -> dict[str, Any]:
        """读取状态卡；无记录/异常返回默认空卡（fail-open，不阻塞回答）。"""
        if not session_id:
            return default_state()
        try:
            with get_pool().connection() as conn:
                row = conn.execute(
                    "SELECT state FROM t_session_memory WHERE session_id = %s",
                    (session_id,),
                ).fetchone()
            if row and row[0]:
                return sanitize_state(row[0] if isinstance(row[0], dict) else json.loads(row[0]))
        except Exception as e:  # noqa: BLE001
            _logger.warning("session_memory load 失败 session_id=%s: %s", session_id, e)
        return default_state()

    def save(self, session_id: str, state: dict[str, Any]) -> None:
        clean = sanitize_state(state)
        try:
            with get_pool().connection() as conn:
                conn.execute(
                    """
                    INSERT INTO t_session_memory (session_id, state, updated_at)
                    VALUES (%s, %s::jsonb, now())
                    ON CONFLICT (session_id)
                    DO UPDATE SET state = EXCLUDED.state, updated_at = now()
                    """,
                    (session_id, json.dumps(clean, ensure_ascii=False)),
                )
        except Exception as e:  # noqa: BLE001
            _logger.warning("session_memory save 失败 session_id=%s: %s", session_id, e)

    def apply_delta(self, session_id: str, delta_text: str) -> dict[str, Any] | None:
        """解析 <memory_delta> 原文并合并入库；返回合并后的状态卡，失败返回 None。"""
        if not session_id or not delta_text or not delta_text.strip():
            return None
        try:
            delta = json.loads(delta_text)
        except (ValueError, TypeError):
            _logger.info("memory_delta 非 JSON，丢弃 session_id=%s", session_id)
            return None
        if not isinstance(delta, dict) or not delta:
            return None
        state = merge_delta(self.load(session_id), delta)
        self.save(session_id, state)
        return state
