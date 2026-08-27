"""分块服务：对应 ragent-main core/chunk/ChunkingService + blockaware。

两种模式（分支依据是预算而非用户策略，与 Java 一致）：
  1. 整文档单块 (ChunkBudget.whole_document)
  2. block-aware 语义分块：按标题层级切分，单块受 max_tokens 预算约束，块间 overlap 重叠

Python 实现比 Java 简洁：无需 BlockAwareChunkerDispatcher 的 12 个分发类，用统一的
"标题边界 + token 预算"即可覆盖大多数场景。
"""
from __future__ import annotations

import re
from typing import Any

from core.chunk.types import Chunk, ChunkBudget, ChunkMetadata
from core.parser.types import Block, BlockType, ParsedDocument, Provenance


def _approx_tokens(text: str) -> int:
    """粗粒度 token 估算：中文按字、英文按词。"""
    return max(1, len(re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9]+", text)))


def _provenance_text(provenance: Provenance | None) -> str:
    return provenance.source if provenance else ""


def chunk(doc: ParsedDocument, budget: ChunkBudget) -> list[Chunk]:
    if budget.whole_document:
        return _whole_document(doc)
    return _block_aware(doc, budget)


def _whole_document(doc: ParsedDocument) -> list[Chunk]:
    if not doc.blocks:
        return []
    text = doc.render_text()
    if not text.strip():
        return []
    provenance = _provenance_text(doc.blocks[0].provenance)
    meta = ChunkMetadata(provenance=provenance)
    return [Chunk(id=f"{provenance}#0", index=0, content=text, metadata=meta)]


def _block_aware(doc: ParsedDocument, budget: ChunkBudget) -> list[Chunk]:
    """按标题层级组织，遇超预算则拆分段落；维护 heading_path 溯源。"""
    chunks: list[Chunk] = []
    idx = 0
    heading_path: list[str] = []
    cur_parts: list[str] = []
    cur_heading: list[str] = []
    cur_prov = doc.blocks[0].provenance if doc.blocks else None

    def flush():
        nonlocal idx
        body = "\n\n".join(cur_parts).strip()
        if not body:
            return
        provenance = _provenance_text(cur_prov)
        meta = ChunkMetadata(provenance=provenance,
                             heading_path=list(cur_heading))
        # id 字段：与 java Chunk.chunkId 对齐，provenance+index 拼接保证唯一
        chunks.append(Chunk(
            id=f"{provenance}#{idx}",
            index=idx, content=body, metadata=meta,
        ))
        # 重叠：保留尾部若干 token 到下一个块开头
        if budget.overlap > 0 and cur_parts:
            tail = cur_parts[-1].split("\n")[-1]
            overlap_text = tail[:budget.overlap]
            cur_parts.clear()
            if overlap_text.strip():
                cur_parts.append(overlap_text)
        else:
            cur_parts.clear()
        idx += 1

    for b in doc.blocks:
        if b.type == BlockType.HEADING:
            level = b.metadata.get("level", 1)
            # 裁剪比当前更深/同级的标题路径
            while len(heading_path) >= level:
                heading_path = heading_path[: level - 1]
                break
            heading_path = heading_path[: level - 1] + [b.text]
            cur_heading = list(heading_path)
            # 标题本身作为分块开头（带上下文）
            if cur_parts and _approx_tokens("\n\n".join(cur_parts)) >= budget.max_tokens:
                flush()
            cur_parts.append(f"{'#' * level} {b.text}")
        else:
            cur_parts.append(b.text)
            if _approx_tokens("\n\n".join(cur_parts)) >= budget.max_tokens:
                flush()
    flush()
    return chunks


def _to_blockaware(b: Block) -> Any:
    """适配器：core.parser.types.Block → core.chunk.blockaware.Block 子类。"""
    from core.chunk.blockaware import (
        HeadingBlock, ParagraphBlock, CodeBlock, TableBlock, ListBlock, ImageBlock,
    )
    meta = {"type": b.type, "level": b.metadata.get("level", 1)}
    if b.type == BlockType.HEADING:
        return HeadingBlock(content=b.text, level=b.metadata.get("level", 1), metadata=meta)
    if b.type == BlockType.CODE:
        return CodeBlock(content=b.text, language=b.metadata.get("language", ""), metadata=meta)
    if b.type == BlockType.TABLE:
        return TableBlock(content=b.text, rows=b.metadata.get("rows", []), metadata=meta)
    if b.type == BlockType.LIST:
        return ListBlock(content=b.text, items=b.metadata.get("items", [b.text]), metadata=meta)
    if b.type == BlockType.IMAGE:
        return ImageBlock(content=b.text, src=b.metadata.get("src", ""), alt=b.metadata.get("alt", ""), metadata=meta)
    return ParagraphBlock(content=b.text, metadata=meta)


def dispatch_chunk(doc: ParsedDocument, budget: ChunkBudget) -> list[Chunk]:
    """BlockAwareChunkerDispatcher 集成入口——严格对齐 ragent blockaware 12 类设计。

    与 chunk() 的差异（向后兼容，调用方可选用）：
      - chunk()：手写 flush 逻辑（cur_parts/cur_heading/cur_prov 维护）
      - dispatch_chunk()：用 BlockAwareChunkerDispatcher + 6 Chunker 子类分派
    """
    from core.chunk.blockaware import BlockAwareChunkerDispatcher, ChunkPacker
    dispatcher = BlockAwareChunkerDispatcher(
        packer=ChunkPacker(max_tokens=budget.max_tokens, overlap=budget.overlap),
    )
    blocks_aw = [_to_blockaware(b) for b in doc.blocks]
    packed_texts = dispatcher.dispatch(blocks_aw)
    chunks: list[Chunk] = []
    for idx, text in enumerate(packed_texts):
        meta = ChunkMetadata(
            provenance=_provenance_text(doc.blocks[0].provenance) if doc.blocks else "",
            heading_path=list(dispatcher.context.heading_path),
        )
        chunks.append(Chunk(
            id=f"{meta.provenance}#{idx}",
            index=idx, content=text, metadata=meta,
        ))
    return chunks


def chunk_file(content: bytes, filename: str, budget: ChunkBudget | None = None) -> list[dict[str, Any]]:
    """便捷入口：解析 + 分块，返回可序列化 dict 列表（供 /ingest 返回）。"""
    from core.parser.parsers import parse_file
    budget = budget or ChunkBudget()
    doc = parse_file(content, filename)
    return [c.to_dict() for c in chunk(doc, budget)]
