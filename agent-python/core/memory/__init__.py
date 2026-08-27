"""对话记忆持久化包：1:1 翻译 ragent rag.core.memory。

导出与 Java 同名的类，便于 server.py / agent.py 直接引用：
  - ChatMessage, Role
  - MemoryProperties
  - ConversationMemoryStore, JdbcConversationMemoryStore
  - ConversationMemorySummaryService, JdbcConversationMemorySummaryService
  - ConversationMemoryService, DefaultConversationMemoryService
  - ConversationRepository
"""
from core.memory.config import MemoryProperties
from core.memory.store import (
    ConversationMemoryService,
    ConversationMemoryStore,
    ConversationRepository,
    DefaultConversationMemoryService,
    JdbcConversationMemoryStore,
    next_message_id,
)
from core.memory.summary import (
    ConversationMemorySummaryService,
    JdbcConversationMemorySummaryService,
)
from core.memory.types import ChatMessage, Role

__all__ = [
    "ChatMessage",
    "Role",
    "MemoryProperties",
    "ConversationMemoryStore",
    "JdbcConversationMemoryStore",
    "ConversationMemorySummaryService",
    "JdbcConversationMemorySummaryService",
    "ConversationMemoryService",
    "DefaultConversationMemoryService",
    "ConversationRepository",
    "next_message_id",
]
