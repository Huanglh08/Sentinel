"""解析层数据模型：与 ragent-main core/parser/model 对齐（Block / ParsedDocument / Provenance）。

对应 Java：
  Block / ParagraphBlock / TableBlock / HeadingBlock ... -> 统一为 Block(带 type)
  ParsedDocument -> ParsedDocument(blocks, metadata)
  Provenance -> Provenance(source, page, ...)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BlockType(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    IMAGE = "image"
    CODE = "code"
    LIST = "list"


@dataclass
class Provenance:
    source: str = ""
    page: int | None = None
    sheet: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Block:
    type: BlockType
    text: str
    provenance: Provenance = field(default_factory=Provenance)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedDocument:
    blocks: list[Block]
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def of(cls, blocks: list[Block], metadata: dict[str, Any] | None = None) -> "ParsedDocument":
        return cls(blocks=blocks, metadata=metadata or {})

    def render_text(self) -> str:
        """整文档扁平文本（对应 Java BlockTextRenderer.render）。"""
        return "\n\n".join(b.text for b in self.blocks if b.text.strip())
