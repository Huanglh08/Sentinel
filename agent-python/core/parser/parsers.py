"""文档解析器集合：对应 ragent-main core/parser 下 Tika/Markdown/Excel/CSV/MinerU/Image。

设计取舍（按 ARCHITECTURE_SPLIT 的"Python 生态优势"）：
  - 文本 / Markdown / CSV / 基础文本：标准库实现，零依赖即可跑。
  - PDF / DOCX / PPTX / XLSX：懒加载第三方库（pypdf / python-docx / python-pptx / openpyxl），
    未安装时给出明确报错，不阻塞骨架运行。
  - 图片：VLM 图生文占位（对应原 image/ 解析器），需接入模型客户端后启用。

解析器按 (MIME / 扩展名) 认领，由 registry 建表分发，语义对齐 Java ParserRegistry。
"""
from __future__ import annotations

import csv
import io
import os
import re
from typing import Callable

from core.parser.types import (
    Block, BlockType, ParsedDocument, Provenance,
)

# ---------------------------------------------------------------------------
# 纯标准库解析器
# ---------------------------------------------------------------------------

def _parse_text(content: bytes, source: str) -> ParsedDocument:
    text = content.decode("utf-8", errors="replace")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    blocks = [Block(BlockType.PARAGRAPH, seg.strip(), Provenance(source))
              for seg in text.split("\n\n") if seg.strip()]
    return ParsedDocument.of(blocks, {"parser": "text"})


def _parse_markdown(content: bytes, source: str) -> ParsedDocument:
    """Markdown：识别 # 标题层级 + 段落 + 代码块 + 表格。"""
    text = content.decode("utf-8", errors="replace")
    blocks: list[Block] = []
    lines = text.splitlines()
    i = 0
    buf: list[str] = []
    in_code = False

    def flush_para():
        if buf:
            joined = "\n".join(buf).strip()
            if joined:
                blocks.append(Block(BlockType.PARAGRAPH, joined, Provenance(source)))
            buf.clear()

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            flush_para()
            in_code = not in_code
            buf.append(line)
            if not in_code:
                blocks.append(Block(BlockType.CODE, "\n".join(buf).strip(), Provenance(source)))
                buf.clear()
            i += 1
            continue
        if in_code:
            buf.append(line); i += 1; continue
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            flush_para()
            level = len(m.group(1))
            blocks.append(Block(BlockType.HEADING, m.group(2).strip(),
                                 Provenance(source), {"level": level}))
            i += 1
            continue
        if line.strip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:-]+\|", lines[i + 1]):
            flush_para()
            tbl = [line]
            while i < len(lines) and lines[i].strip().startswith("|"):
                tbl.append(lines[i]); i += 1
            blocks.append(Block(BlockType.TABLE, "\n".join(tbl).strip(), Provenance(source)))
            continue
        buf.append(line); i += 1
    flush_para()
    return ParsedDocument.of(blocks, {"parser": "markdown"})


def _parse_csv(content: bytes, source: str) -> ParsedDocument:
    text = content.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return ParsedDocument.of([], {"parser": "csv"})
    header = rows[0]
    blocks: list[Block] = [Block(BlockType.TABLE, _table_to_md(header, rows[1:]), Provenance(source))]
    return ParsedDocument.of(blocks, {"parser": "csv"})


def _table_to_md(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |",
           "| " + " | ".join("---" for _ in header) + " |"]
    for r in rows:
        out.append("| " + " | ".join(c if c is not None else "" for c in r) + " |")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 懒加载第三方库解析器
# ---------------------------------------------------------------------------

def _parse_pdf(content: bytes, source: str) -> ParsedDocument:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("PDF 解析需安装 pypdf：pip install pypdf")
    reader = PdfReader(io.BytesIO(content))
    blocks: list[Block] = []
    for idx, page in enumerate(reader.pages, 1):
        txt = (page.extract_text() or "").strip()
        if txt:
            blocks.append(Block(BlockType.PARAGRAPH, txt, Provenance(source, page=idx)))
    return ParsedDocument.of(blocks, {"parser": "pdf"})


def _parse_docx(content: bytes, source: str) -> ParsedDocument:
    try:
        from docx import Document
    except ImportError:
        raise RuntimeError("DOCX 解析需安装 python-docx：pip install python-docx")
    doc = Document(io.BytesIO(content))
    blocks: list[Block] = []
    for p in doc.paragraphs:
        t = p.text.strip()
        if not t:
            continue
        if p.style.name.startswith("Heading"):
            blocks.append(Block(BlockType.HEADING, t, Provenance(source), {"level": 2}))
        else:
            blocks.append(Block(BlockType.PARAGRAPH, t, Provenance(source)))
    return ParsedDocument.of(blocks, {"parser": "docx"})


def _parse_xlsx(content: bytes, source: str) -> ParsedDocument:
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("XLSX 解析需安装 openpyxl：pip install openpyxl")
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    blocks: list[Block] = []
    for ws in wb.worksheets:
        rows = [[c.value for c in row] for row in ws.iter_rows()]
        if rows:
            blocks.append(Block(BlockType.TABLE, _table_to_md(rows[0], rows[1:]),
                                 Provenance(source, sheet=ws.title)))
    return ParsedDocument.of(blocks, {"parser": "xlsx"})


# ---------------------------------------------------------------------------
# 扩展名 -> 解析器 注册表（对应 Java ParserRegistry）
# ---------------------------------------------------------------------------

PARSERS: dict[str, Callable[[bytes, str], ParsedDocument]] = {
    ".txt": _parse_text,
    ".md": _parse_markdown,
    ".markdown": _parse_markdown,
    ".csv": _parse_csv,
    ".pdf": _parse_pdf,
    ".docx": _parse_docx,
    ".xlsx": _parse_xlsx,
}


def parse_file(content: bytes, filename: str) -> ParsedDocument:
    ext = os.path.splitext(filename)[1].lower()
    parser = PARSERS.get(ext)
    if parser is None:
        # 长尾文本格式兜底（对应 Java Tika 的 text/* 通配）
        if ext in (".json", ".xml", ".html", ".htm", ".log", ".rtf", ".text"):
            return _parse_text(content, filename)
        raise ValueError(f"不支持的文件类型: {ext or '(无扩展名)'}（支持: {', '.join(PARSERS)}）")
    return parser(content, filename)
