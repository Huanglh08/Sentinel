"""External retrieval channels aligned with ragent Java channel backends."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from core.retrieval.types import ChannelType, RetrievedChunk

_logger = logging.getLogger("sentinel.retrieval.external_channels")


@dataclass(frozen=True)
class LightRagConfig:
    base_url: str = "http://127.0.0.1:9621"
    api_key: str = ""
    query_mode: str = "hybrid"
    timeout_ms: int = 15_000


class LightRagBackend:
    def __init__(self, config: LightRagConfig | None = None):
        self.config = config or LightRagConfig()

    async def retrieve(self, query: str, collections: list[str], top_k: int) -> list[RetrievedChunk]:
        if not query.strip():
            return []
        body = {
            "query": query,
            "mode": self.config.query_mode or "mix",
            "only_need_context": True,
            "include_references": True,
            "include_chunk_content": True,
        }
        if top_k > 0:
            body["top_k"] = top_k
        headers = {"X-API-Key": self.config.api_key} if self.config.api_key else None
        timeout = None if self.config.timeout_ms <= 0 else self.config.timeout_ms / 1000
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    self.config.base_url.rstrip("/") + "/query",
                    json=body,
                    headers=headers,
                )
            if response.status_code < 200 or response.status_code >= 300:
                _logger.warning("LightRAG 请求失败 path=/query, code=%s", response.status_code)
                return []
            root = response.json() if response.content else None
            return self._parse_references(root, collections)
        except Exception as exc:
            _logger.warning("LightRAG 检索失败，降级为空结果: %s", exc)
            return []

    # ---- 1:1 翻译 ragent LightRagClient.insertText / deleteByDoc（图谱写入，best-effort）----
    async def insert_text(self, text: str, file_source: str) -> None:
        """写入文档全文到 LightRAG 知识图谱（对应 Java insertText）。"""
        if not text or not text.strip():
            return
        headers = {"X-API-Key": self.config.api_key} if self.config.api_key else None
        timeout = None if self.config.timeout_ms <= 0 else self.config.timeout_ms / 1000
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    self.config.base_url.rstrip("/") + "/documents/text",
                    json={"text": text, "file_source": file_source},
                    headers=headers,
                )
            if resp.status_code < 200 or resp.status_code >= 300:
                _logger.warning("LightRAG 文档写入失败 code=%s", resp.status_code)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("LightRAG 文档写入失败，已跳过: %s", exc)

    async def delete_by_doc(self, doc_id: str) -> None:
        """按文档清除 LightRAG 知识图谱（对应 Java deleteByDoc）。"""
        if not doc_id:
            return
        headers = {"X-API-Key": self.config.api_key} if self.config.api_key else None
        timeout = None if self.config.timeout_ms <= 0 else self.config.timeout_ms / 1000
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.delete(
                    self.config.base_url.rstrip("/") + "/documents",
                    params={"doc_id": doc_id},
                    headers=headers,
                )
            if resp.status_code < 200 or resp.status_code >= 300:
                _logger.warning("LightRAG 文档删除失败 code=%s", resp.status_code)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("LightRAG 文档删除失败，已跳过: %s", exc)

    def _parse_references(self, root: Any, collections: list[str]) -> list[RetrievedChunk]:
        if not isinstance(root, dict):
            return []
        refs = root.get("references")
        filter_by_collection = bool(collections)
        chunks: list[RetrievedChunk] = []
        if isinstance(refs, list) and refs:
            rank = 0
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                file_path = str(ref.get("file_path") or "")
                source = GraphFileSource.parse(file_path)
                # 不再在此处按库丢弃未命中证据：命中/未命中切分与名额封顶统一上移到引擎
                # （1:1 翻译 ragent GraphSearchChannel + ScopeQuota），与 Java 行为一致
                content = ref.get("content")
                if isinstance(content, list):
                    text = "\n".join(str(item) for item in content if str(item).strip()).strip()
                else:
                    text = str(content or "").strip()
                if not text:
                    text = file_path.strip()
                if not text:
                    continue
                doc_id = source.doc_id if source else None
                chunks.append(RetrievedChunk(
                    id=str(ref.get("reference_id") or f"graph:{rank}"),
                    text=text,
                    score=1.0 / (rank + 1),
                    collection_name=source.collection_name if source else None,
                    doc_id=doc_id,
                    doc_name=None if doc_id else (file_path or None),
                    channel=ChannelType.GRAPH,
                ))
                rank += 1
            return chunks
        context = str(root.get("response") or "")
        if context.strip() and not filter_by_collection:
            return [RetrievedChunk(id="graph:context", text=context, score=1.0, channel=ChannelType.GRAPH)]
        if context.strip() and filter_by_collection:
            _logger.warning("LightRAG 未返回 references，兜底上下文无法按库归属已丢弃")
        return []


@dataclass(frozen=True)
class WebSearchConfig:
    api_url: str = "https://ydc-index.io/v1/search"
    api_key: str = ""
    count: int = 5
    timeout_seconds: int = 10


class YouComWebSearchBackend:
    def __init__(self, config: WebSearchConfig | None = None):
        self.config = config or WebSearchConfig()

    async def retrieve(self, query: str, collections: list[str], top_k: int) -> list[RetrievedChunk]:
        del collections
        if not query.strip() or not self.config.api_key:
            return []
        count = top_k if top_k > 0 else self.config.count
        headers = {"X-API-Key": self.config.api_key}
        params = {"query": query, "count": str(count)}
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                response = await client.get(self.config.api_url, params=params, headers=headers)
            if response.status_code < 200 or response.status_code >= 300:
                _logger.warning("You.com 检索失败 code=%s", response.status_code)
                return []
            return self._parse_results(response.json(), count)
        except Exception as exc:
            _logger.warning("You.com 检索失败，降级为空结果: %s", exc)
            return []

    def _parse_results(self, root: Any, count: int) -> list[RetrievedChunk]:
        """1:1 翻译 ragent WebSearchChannel.parseChunks + toChunk。

        You.com 响应结构 {"results": {"web": [...], "news": [...]}}；news 可能缺失，
        每条结果除 url/title/description/snippets 外字段均视为可选，防御式读取。
        count 是「每 section」语义（web、news 各最多 count 条），合并两段后统一截断到 count，
        使 count 对外表达「返回结果总条数上限」，与 Java 一致。
        """
        items: list[dict] = []
        if isinstance(root, dict):
            results = root.get("results")
            if isinstance(results, dict):
                for section in (results.get("web"), results.get("news")):
                    if isinstance(section, list):
                        items.extend(s for s in section if isinstance(s, dict))
            elif isinstance(results, list):
                # 防御：个别版本直接平铺为列表
                items.extend(s for s in results if isinstance(s, dict))
        elif isinstance(root, list):
            items.extend(s for s in root if isinstance(s, dict))
        if count > 0:
            items = items[:count]
        results: list[RetrievedChunk] = []
        for index, item in enumerate(items):
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            description = str(item.get("description") or "").strip()
            snippets = item.get("snippets")
            parts: list[str] = []
            if title:
                parts.append(f"【{title}】")
            if description:
                parts.append(description)
            if isinstance(snippets, list):
                for snippet in snippets:
                    s = str(snippet or "").strip()
                    if s:
                        parts.append(s)
            elif isinstance(snippets, str) and snippets.strip():
                parts.append(snippets.strip())
            if url:
                parts.append(f"来源: {url}")
            text = "\n".join(parts).strip()
            if not text:
                continue
            results.append(RetrievedChunk(
                id=url or f"web:{index}",
                text=text,
                score=1.0 / (index + 1),
                doc_id=url or f"web:{index}",
                doc_name=title or url or f"web:{index}",
                channel=ChannelType.WEB_SEARCH,
                source_type="WEB",
                url=url or None,
            ))
        return results


# 1:1 翻译 ragent rag.core.graph.GraphFileSource：{collectionName}_{docId} 编解码
_GRAPH_FILE_SOURCE_RE = re.compile(r"^(.+)_(\d+)((?:\.[A-Za-z0-9]+)*)$")


class GraphFileSource:
    """1:1 翻译 ragent rag.core.graph.GraphFileSource。

    图谱文档来源标识 {collectionName}_{docId} 的编解码。
    docId 为雪花纯数字，从右锚定「末段 _数字」即可唯一还原两个分量；
    库名用户自填、可含下划线且可互为前缀（kb 与 kb_hr 合法共存），从左做子串匹配必然串库，
    故读侧把别库证据划进主路、删侧连带删光别库数据，归属判定必须走这里的解析 + 全名等值。
    """

    __slots__ = ("collection_name", "doc_id")

    def __init__(self, collection_name: str, doc_id: str) -> None:
        self.collection_name = collection_name
        self.doc_id = doc_id

    @staticmethod
    def encode(collection_name: str, doc_id: str) -> str:
        """写入侧编码，与 parse 互为逆运算。"""
        return f"{collection_name}_{doc_id}"

    @staticmethod
    def parse(file_path: str) -> "GraphFileSource | None":
        """从 LightRAG 回传的 file_path 还原来源，先取 basename 以跳过可能的目录前缀；不符合编码返回 None。"""
        if not file_path or not file_path.strip():
            return None
        base_name = file_path[file_path.rfind("/") + 1:]
        matcher = _GRAPH_FILE_SOURCE_RE.fullmatch(base_name)
        return GraphFileSource(matcher.group(1), matcher.group(2)) if matcher else None
