"""Block-aware 分块：严格对齐 ragent bootstrap core.chunk.blockaware.*（12 个核心类）。

java 端 12 个类（合并为 1 个 python 文件）：
  - Block/BlockChunker/ChunkContext/ChunkPacker 基础
  - 6 个 Block 子类：HeadingBlock/ParagraphBlock/CodeBlock/TableBlock/ListBlock/ImageBlock
  - 6 个 Chunker 子类：HeadingChunker/ParagraphChunker/CodeChunker/TableChunker/ListChunker/ImageChunker
  - HtmlTableChunker（= TableChunker 别名）
  - 2 个工具：HeadingHandler/ChunkPacker
  - 1 个分派：BlockAwareChunkerDispatcher

核心设计（与 ragent 一致）：
  - Block 抽象：每种文档结构（标题/段落/代码/表格/列表/图片）是一类 Block
  - Chunker 分派：每个 BlockChunker 知道自己能处理哪种 Block
  - Dispatcher 编排：按顺序遍历 Block，分派给对应 Chunker，HeadingChunker 维护 heading_path 状态
  - Packer 打包：按 max_tokens 预算（默认 512）将 chunk 文本打包
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# ============ 基础类型（严格对应 ragent ChunkContext / Block） ============

@dataclass
class ChunkContext:
    """严格对应 ragent ChunkContext（含 heading_path 状态）。"""

    heading_path: list[str] = field(default_factory=list)
    provenance: str = ""


@dataclass
class Block:
    """块抽象基类——严格对应 ragent Block 父类。"""

    content: str
    block_type: str = "block"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class HeadingBlock(Block):
    block_type: str = "heading"
    level: int = 1


@dataclass
class ParagraphBlock(Block):
    block_type: str = "paragraph"


@dataclass
class CodeBlock(Block):
    block_type: str = "code"
    language: str = ""


@dataclass
class TableBlock(Block):
    block_type: str = "table"
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class ListBlock(Block):
    block_type: str = "list"
    items: list[str] = field(default_factory=list)


@dataclass
class ImageBlock(Block):
    block_type: str = "image"
    src: str = ""
    alt: str = ""


# ============ Chunker 抽象（严格对应 ragent BlockChunker） ============

class BlockChunker(ABC):
    """严格对应 ragent BlockChunker 抽象基类。"""

    @abstractmethod
    def can_handle(self, block: Block) -> bool: ...

    @abstractmethod
    def chunk(self, block: Block, context: ChunkContext) -> list[str]: ...


# ============ 6 个 Chunker 子类（严格对应 ragent 6 个具体 Chunker） ============

class HeadingChunker(BlockChunker):
    """严格对应 ragent HeadingChunker——维护 heading_path 状态，不入 chunk。"""

    def can_handle(self, block: Block) -> bool:
        return isinstance(block, HeadingBlock)

    def chunk(self, block: HeadingBlock, context: ChunkContext) -> list[str]:
        # 维护 heading_path 栈
        while len(context.heading_path) >= block.level:
            context.heading_path.pop()
        context.heading_path.append(block.content.strip())
        return []  # heading 自身不入 chunk


class ParagraphChunker(BlockChunker):
    """严格对应 ragent ParagraphChunker。"""

    def can_handle(self, block: Block) -> bool:
        return isinstance(block, ParagraphBlock)

    def chunk(self, block: ParagraphBlock, context: ChunkContext) -> list[str]:
        return [block.content]


class CodeChunker(BlockChunker):
    """严格对应 ragent CodeChunker——代码块作为整体保留。"""

    def can_handle(self, block: Block) -> bool:
        return isinstance(block, CodeBlock)

    def chunk(self, block: CodeBlock, context: ChunkContext) -> list[str]:
        return [block.content]


class TableChunker(BlockChunker):
    """严格对应 ragent TableChunker——表格作为整体保留。"""

    def can_handle(self, block: Block) -> bool:
        return isinstance(block, TableBlock)

    def chunk(self, block: TableBlock, context: ChunkContext) -> list[str]:
        return ["\n".join(" | ".join(r) for r in block.rows)]


class ListChunker(BlockChunker):
    """严格对应 ragent ListChunker——列表项合并为一段。"""

    def can_handle(self, block: Block) -> bool:
        return isinstance(block, ListBlock)

    def chunk(self, block: ListBlock, context: ChunkContext) -> list[str]:
        return ["\n".join(f"- {item}" for item in block.items)]


class ImageChunker(BlockChunker):
    """严格对应 ragent ImageChunker——图片不入 chunk。"""

    def can_handle(self, block: Block) -> bool:
        return isinstance(block, ImageBlock)

    def chunk(self, block: ImageBlock, context: ChunkContext) -> list[str]:
        return []


# HtmlTableChunker：ragent 单独类，处理 HTML 表格 → 同样转 TableBlock
HtmlTableChunker = TableChunker


# ============ 工具类（严格对应 ragent HeadingHandler / ChunkPacker） ============

class HeadingHandler:
    """严格对应 ragent HeadingHandler——heading_path 状态封装。"""

    def __init__(self) -> None:
        self._path: list[str] = []

    def update(self, level: int, title: str) -> None:
        while len(self._path) >= level:
            self._path.pop()
        self._path.append(title)

    @property
    def path(self) -> list[str]:
        return list(self._path)


class ChunkPacker:
    """严格对应 ragent ChunkPacker（按 token 预算打包 chunks）。"""

    def __init__(self, max_tokens: int = 512, overlap: int = 50) -> None:
        self.max_tokens = max_tokens
        self.overlap = overlap

    def pack(self, chunks: list[str]) -> list[str]:
        """简单按字符数打包（python 端无 tokenizer，粗略 1 token ≈ 2 char）。"""
        packed: list[str] = []
        buf = ""
        for c in chunks:
            # 粗略 1 token ≈ 2 char
            if buf and len(buf) + len(c) > self.max_tokens * 2:
                packed.append(buf)
                buf = c
            else:
                buf = (buf + "\n\n" + c) if buf else c
        if buf:
            packed.append(buf)
        return packed


# ============ 分派器（严格对应 ragent BlockAwareChunkerDispatcher） ============

class BlockAwareChunkerDispatcher:
    """严格对应 ragent BlockAwareChunkerDispatcher（按 Block 类型分派 Chunker）。"""

    def __init__(
        self,
        chunkers: list[BlockChunker] | None = None,
        packer: ChunkPacker | None = None,
    ) -> None:
        self.chunkers = chunkers or [
            HeadingChunker(), ParagraphChunker(), CodeChunker(),
            TableChunker(), ListChunker(), ImageChunker(),
        ]
        self.context = ChunkContext()
        self.packer = packer or ChunkPacker()

    def dispatch(self, blocks: list[Block]) -> list[str]:
        """分派 + 打包——严格对齐 ragent BlockAwareChunkerDispatcher。

        返回打包后的 chunk 文本列表（不含 metadata，调用方需自行包装 ChunkMetadata）。
        """
        raw_chunks: list[str] = []
        for block in blocks:
            for chunker in self.chunkers:
                if chunker.can_handle(block):
                    pieces = chunker.chunk(block, self.context)
                    raw_chunks.extend(pieces)
                    break
        return self.packer.pack(raw_chunks)
