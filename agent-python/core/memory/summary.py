"""对话记忆摘要：1:1 翻译 ragent JdbcConversationMemorySummaryService。

算法严格对齐 Java：
  - compressIfNeeded：仅对 assistant 消息触发，异步（线程池）执行 doCompressIfNeeded
  - doCompressIfNeeded：分布式锁（此处用进程内锁，单进程等价 Redisson；多实例可换 Redis 锁）
    → 统计用户消息数 ≥ summaryStartTurns 才触发
    → 取最近 maxTurns 条用户消息，计算 historyStartId（最早）、summaryCutoffId（中点，重叠滑窗）
    → 取上次摘要 lastMessageId 或按时间 findMaxMessageIdAtOrBefore 作为 afterId
    → 仅当 afterId < historyStartId 才生成（重叠滑出窗口后再次摘要）
    → summarizeMessages 调 LLM 合并历史摘要与窗口内消息，落库 t_conversation_summary
  - loadLatestSummary / decorateIfNeeded：与 Java 一致（system 包装）
"""
from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Awaitable, Callable

from core.memory.config import MemoryProperties
from core.memory.store import ConversationRepository, next_message_id
from core.memory.types import ChatMessage, Role

_logger = logging.getLogger("sentinel.memory.summary")

_SUMMARY_LOCK_PREFIX = "ragent:memory:summary:lock:"
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="memory-summary")


def _lock_for(conversation_id: str, user_id: str) -> threading.Lock:
    key = f"{user_id.strip()}:{conversation_id.strip()}"
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


class ConversationMemorySummaryService:
    """1:1 翻译 ragent ConversationMemorySummaryService 接口。"""

    def compress_if_needed(
        self, conversation_id: str, user_id: str, message: ChatMessage
    ) -> None:
        raise NotImplementedError

    def load_latest_summary(
        self, conversation_id: str, user_id: str
    ) -> ChatMessage | None:
        raise NotImplementedError

    def decorate_if_needed(self, summary: ChatMessage | None) -> ChatMessage | None:
        raise NotImplementedError


class JdbcConversationMemorySummaryService(ConversationMemorySummaryService):
    """1:1 翻译 ragent JdbcConversationMemorySummaryService。"""

    def __init__(
        self,
        repo: ConversationRepository | None = None,
        memory_properties: MemoryProperties | None = None,
        llm_chat: Callable[[list[dict], str], Awaitable[str]] | None = None,
        summary_prompt: str | None = None,
    ) -> None:
        self.repo = repo or ConversationRepository()
        self.memory_properties = memory_properties or MemoryProperties()
        self.llm_chat = llm_chat
        self._summary_prompt = summary_prompt

    # ---- 接口实现 ----
    def compress_if_needed(
        self, conversation_id: str, user_id: str, message: ChatMessage
    ) -> None:
        if not self.memory_properties.summary_enabled:
            return
        if message.role != Role.ASSISTANT:
            return
        # 1:1 翻译 Java CompletableFuture.runAsync(...).exceptionally(...)
        _executor.submit(self._safe_compress, conversation_id, user_id)

    def load_latest_summary(
        self, conversation_id: str, user_id: str
    ) -> ChatMessage | None:
        record = self.repo.find_latest_summary(conversation_id, user_id)
        return self._to_chat_message(record)

    def decorate_if_needed(self, summary: ChatMessage | None) -> ChatMessage | None:
        if summary is None or not (summary.content or "").strip():
            return summary
        wrapped = _render_summary_wrapper(summary.content.strip())
        return ChatMessage.system(wrapped)

    # ---- 核心压缩 ----
    def _safe_compress(self, conversation_id: str, user_id: str) -> None:
        try:
            self._do_compress_if_needed(conversation_id, user_id)
        except Exception as exc:  # pragma: no cover - 与 Java exceptionally 一致
            _logger.error("对话记忆摘要异步任务失败 - %s/%s: %s", conversation_id, user_id, exc)

    def _do_compress_if_needed(self, conversation_id: str, user_id: str) -> None:
        start_time = _now_ms()
        trigger_turns = self.memory_properties.summary_start_turns
        max_turns = self.memory_properties.history_keep_turns
        if max_turns <= 0 or trigger_turns <= 0:
            return

        lock = _lock_for(conversation_id, user_id)
        if not lock.acquire(blocking=False):
            return
        try:
            total = self.repo.count_user_messages(conversation_id, user_id)
            if total < trigger_turns:
                return

            latest_summary = self.repo.find_latest_summary(conversation_id, user_id)
            latest_user_turns = self.repo.list_latest_user_only_messages(
                conversation_id, user_id, max_turns
            )
            if not latest_user_turns:
                return
            history_start_id = latest_user_turns[-1]["id"]
            if not history_start_id:
                return

            after_id = self._resolve_summary_start_id(conversation_id, user_id, latest_summary)
            if after_id is not None and int(after_id) >= int(history_start_id):
                return

            summary_cutoff_id = latest_user_turns[(len(latest_user_turns) - 1) // 2]["id"]
            if not summary_cutoff_id:
                return

            to_summarize = self.repo.list_messages_between_ids(
                conversation_id, user_id, after_id, summary_cutoff_id
            )
            if not to_summarize:
                return

            last_message_id = self._resolve_last_message_id(to_summarize)
            if not last_message_id:
                return

            existing_summary = "" if latest_summary is None else latest_summary.get("content") or ""
            summary = self._summarize_messages(to_summarize, existing_summary)
            if not summary:
                return

            self.repo.add_message_summary(
                {
                    "conversation_id": conversation_id,
                    "user_id": user_id,
                    "content": summary,
                    "last_message_id": last_message_id,
                }
            )
            _logger.info(
                "摘要成功 - conversationId：%s，userId：%s，消息数：%d，耗时：%dms",
                conversation_id,
                user_id,
                len(to_summarize),
                _now_ms() - start_time,
            )
        except Exception as exc:  # pragma: no cover
            _logger.error("摘要失败 - %s/%s: %s", conversation_id, user_id, exc)
        finally:
            lock.release()

    def _resolve_summary_start_id(
        self, conversation_id: str, user_id: str, summary: dict | None
    ) -> str | None:
        if summary is None:
            return None
        if summary.get("last_message_id"):
            return str(summary["last_message_id"])
        after = summary.get("update_time") or summary.get("create_time")
        if after is None:
            return None
        return self.repo.find_max_message_id_at_or_before(conversation_id, user_id, after)

    @staticmethod
    def _resolve_last_message_id(to_summarize: list[dict]) -> str | None:
        for item in reversed(to_summarize):
            if item.get("id"):
                return str(item["id"])
        return None

    def _summarize_messages(
        self, messages: list[dict], existing_summary: str
    ) -> str:
        histories = [
            self._to_history_message(m)
            for m in messages
            if m.get("content") and m.get("role")
        ]
        histories = [h for h in histories if h is not None]
        if not histories:
            return existing_summary

        max_chars = self.memory_properties.summary_max_chars
        prompt_msgs: list[dict] = []
        prompt_msgs.append({"role": "system", "content": self._summary_prompt_text()})
        if existing_summary.strip():
            prompt_msgs.append(
                {
                    "role": "assistant",
                    "content": (
                        "历史摘要（仅用于合并去重，不得作为事实新增来源；若与本轮对话冲突，以本轮对话为准）：\n"
                        + existing_summary.strip()
                    ),
                }
            )
        prompt_msgs.extend(
            [{"role": h.role.value, "content": h.content} for h in histories]
        )
        prompt_msgs.append(
            {
                "role": "user",
                "content": f"合并以上对话与历史摘要，去重后输出更新摘要。要求：严格≤{max_chars}字符；仅一行。",
            }
        )

        if self.llm_chat is None:
            # 未接入 LLM 时跳过生成（与 Java 异常回退 existingSummary 一致）
            return existing_summary
        try:
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(self.llm_chat(prompt_msgs, "FAST"))
            finally:
                loop.close()
            _logger.info("对话摘要生成 - resultChars: %d", len(result or ""))
            return (result or existing_summary).strip()
        except Exception as exc:  # pragma: no cover
            _logger.error("对话记忆摘要生成失败, 消息数: %d: %s", len(messages), exc)
            return existing_summary

    def _summary_prompt_text(self) -> str:
        if self._summary_prompt:
            return self._summary_prompt
        return (
            "你是一个对话摘要助手。请将用户与助手的对话压缩为简洁的要点摘要，"
            "保留关键事实、用户意图与未决问题，去除寒暄与重复。"
        )

    @staticmethod
    def _to_history_message(row: dict) -> ChatMessage | None:
        role = (row.get("role") or "").lower()
        if role == "user":
            return ChatMessage.user(row["content"])
        if role == "assistant":
            from core.memory.types import _strip_citation

            return ChatMessage.assistant(_strip_citation(row["content"]))
        return None

    @staticmethod
    def _to_chat_message(record: dict | None) -> ChatMessage | None:
        if record is None or not (record.get("content") or "").strip():
            return None
        return ChatMessage(Role.SYSTEM, record["content"])


def _render_summary_wrapper(content: str) -> str:
    """1:1 翻译 ragent PromptTemplateLoader.renderSection(CONTEXT_FORMAT_PATH, 'summary-wrapper')。"""
    try:
        from core.prompt.template_loader import render_section

        return render_section("context-format.st", "summary-wrapper", {"content": content})
    except Exception as e:  # noqa: BLE001
        _logger.debug("摘要模板渲染失败，回退纯文本格式: %s", e)
        return f"[历史摘要]\n{content}"


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)
