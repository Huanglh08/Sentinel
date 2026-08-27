"""文件存储业务层：1:1 翻译 ragent FileStorageService / DefaultFileStorageService / StoredFileDTO。

负责 namespace/key 组装、桶语义归属（私有文档→kbBucket，公共资产→assetBucket）、
类型探测与 StoredFileDTO 装配，底层裸操作委托 ObjectStorageClient。
"""
from __future__ import annotations

import logging
import mimetypes
import os
import threading
import uuid
from dataclasses import dataclass
from typing import BinaryIO, Protocol

from core.storage.client import ObjectStorageClient, build_client
from core.storage.config import RagStorageProperties

_logger = logging.getLogger("sentinel.storage.service")

_LOCK_PREFIX = "ragent:kb:space:init:"
_space_locks: dict[str, threading.Lock] = {}
_space_locks_guard = threading.Lock()


@dataclass
class StoredFileDTO:
    """1:1 翻译 ragent StoredFileDTO。"""

    url: str
    detected_type: str
    mime_type: str | None
    size: int
    original_filename: str | None = None


class FileStorageService(Protocol):
    """1:1 翻译 ragent FileStorageService。"""

    def upload(self, namespace: str, content: BinaryIO, size: int,
               original_filename: str, content_type: str | None = None) -> StoredFileDTO:
        ...

    def upload_asset(self, content: bytes, original_filename: str,
                     content_type: str | None = None) -> StoredFileDTO:
        ...

    def open_stream(self, key: str) -> BinaryIO:
        ...

    def delete_by_url(self, key: str) -> None:
        ...

    def get_public_url(self, key: str) -> str:
        ...

    def create_knowledge_space(self, namespace: str) -> None:
        ...

    def delete_knowledge_space(self, namespace: str) -> None:
        ...


class DefaultFileStorageService:
    """1:1 翻译 ragent DefaultFileStorageService。"""

    def __init__(self, props: RagStorageProperties | None = None,
                 client: ObjectStorageClient | None = None) -> None:
        self.props = props or RagStorageProperties()
        self.client = client or build_client(self.props)
        self.kb_bucket = self.props.kb_bucket
        self.asset_bucket = self.props.asset_bucket

    # ---- 上传（文档，落到 kbBucket）----
    def upload(self, namespace: str, content: BinaryIO, size: int,
               original_filename: str, content_type: str | None = None) -> StoredFileDTO:
        self._validate_namespace(namespace)
        if content is None:
            raise ValueError("上传内容不能为空")
        if size < 0:
            raise ValueError("上传内容大小不能小于 0")
        detected = self._resolve_content_type(original_filename, content_type)
        key = self._document_key(namespace, original_filename)
        self.client.stream_put(self.kb_bucket, key, content, size, detected)
        return self._build_dto(key, original_filename, detected, size)

    def reliable_upload(self, namespace: str, content: BinaryIO, size: int,
                        original_filename: str, content_type: str | None = None) -> StoredFileDTO:
        self._validate_namespace(namespace)
        detected = self._resolve_content_type(original_filename, content_type)
        key = self._document_key(namespace, original_filename)
        self.client.reliable_put(self.kb_bucket, key, content, size, detected)
        return self._build_dto(key, original_filename, detected, size)

    # ---- 上传（公共资产，落到 assetBucket）----
    def upload_asset(self, content: bytes, original_filename: str,
                     content_type: str | None = None) -> StoredFileDTO:
        if content is None:
            raise ValueError("上传内容不能为空")
        detected = self._resolve_content_type(original_filename, content_type)
        key = self._random_key(original_filename)
        self.client.stream_put(self.asset_bucket, key, content, len(content), detected)
        return self._build_dto(key, original_filename, detected, len(content))

    # ---- 读取 / 删除 / URL ----
    def open_stream(self, key: str) -> BinaryIO:
        if not key:
            raise ValueError("对象 key 不能为空")
        return self.client.get_object(self.kb_bucket, key)

    def delete_by_url(self, key: str) -> None:
        if not key:
            raise ValueError("对象 key 不能为空")
        self.client.delete_object(self.kb_bucket, key)

    def get_public_url(self, key: str) -> str:
        if not key:
            raise ValueError("对象 key 不能为空")
        return self.client.build_public_url(self.asset_bucket, key)

    # ---- 知识库空间（目录）----
    def create_knowledge_space(self, namespace: str) -> None:
        self._validate_namespace(namespace)
        marker_key = namespace + "/"
        if self.client.object_exists(self.kb_bucket, marker_key):
            return
        lock = self._space_lock(namespace)
        if not lock.acquire(blocking=False):
            return  # 并发下他方正在建，直接返回
        try:
            if self.client.object_exists(self.kb_bucket, marker_key):
                return
            self.client.stream_put(self.kb_bucket, marker_key, b"", 0, None)
            _logger.info("知识库目录创建成功 bucket=%s, namespace=%s", self.kb_bucket, namespace)
        finally:
            lock.release()

    def delete_knowledge_space(self, namespace: str) -> None:
        self._validate_namespace(namespace)
        self.client.delete_by_prefix(self.kb_bucket, namespace + "/")

    # ---- 内部工具 ----
    @staticmethod
    def _document_key(namespace: str, original_filename: str) -> str:
        return namespace + "/" + DefaultFileStorageService._random_key(original_filename)

    @staticmethod
    def _random_key(original_filename: str) -> str:
        suffix = DefaultFileStorageService._extract_suffix(original_filename)
        key = uuid.uuid4().hex
        return key if not suffix else f"{key}.{suffix}"

    @staticmethod
    def _extract_suffix(filename: str) -> str:
        if not filename:
            return ""
        idx = filename.rfind(".")
        return "" if (idx < 0 or idx == len(filename) - 1) else filename[idx + 1:].strip()

    @staticmethod
    def _validate_namespace(namespace: str) -> None:
        if not namespace or not namespace.strip():
            raise ValueError("namespace 不能为空")

    @staticmethod
    def _resolve_content_type(original_filename: str | None, content_type: str | None) -> str | None:
        if content_type and content_type.strip():
            return content_type.strip()
        if original_filename and original_filename.strip():
            guessed, _ = mimetypes.guess_type(original_filename)
            return guessed
        return None

    @staticmethod
    def _build_dto(url: str, original_filename: str, content_type: str | None, size: int) -> StoredFileDTO:
        detected_type = DisplayType.of(original_filename, content_type).code
        return StoredFileDTO(
            url=url,
            detected_type=detected_type,
            mime_type=content_type,
            size=size,
            original_filename=original_filename,
        )

    @staticmethod
    def _space_lock(namespace: str) -> threading.Lock:
        key = f"{namespace.strip()}"
        with _space_locks_guard:
            lock = _space_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                _space_locks[key] = lock
            return lock


class DisplayType:
    """1:1 翻译 ragent util.DisplayType：展示标签唯一产生点（扩展名优先，取值集合封闭）。"""

    # 封闭的取值集合（与 Java DisplayType 对齐）
    DOC = "doc"
    SHEET = "sheet"
    PDF = "pdf"
    SLIDE = "slide"
    IMAGE = "image"
    TEXT = "text"
    AUDIO = "audio"
    VIDEO = "video"
    OTHER = "other"

    _EXT_MAP = {
        "pdf": PDF,
        "doc": DOC, "docx": DOC, "wps": DOC, "rtf": DOC, "txt": TEXT, "md": TEXT,
        "xls": SHEET, "xlsx": SHEET, "csv": SHEET, "et": SHEET,
        "ppt": SLIDE, "pptx": SLIDE,
        "png": IMAGE, "jpg": IMAGE, "jpeg": IMAGE, "gif": IMAGE, "bmp": IMAGE, "webp": IMAGE,
        "mp3": AUDIO, "wav": AUDIO, "m4a": AUDIO,
        "mp4": VIDEO, "mov": VIDEO, "avi": VIDEO,
    }
    _MIME_MAP = {
        "application/pdf": PDF,
        "application/msword": DOC,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DOC,
        "application/vnd.ms-excel": SHEET,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": SHEET,
        "application/vnd.ms-powerpoint": SLIDE,
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": SLIDE,
        "text/plain": TEXT, "text/markdown": TEXT,
        "image/": IMAGE, "audio/": AUDIO, "video/": VIDEO,
    }

    def __init__(self, code: str) -> None:
        self.code = code

    @classmethod
    def of(cls, filename: str | None, content_type: str | None) -> "DisplayType":
        if filename:
            ext = filename.rfind(".")
            if ext >= 0:
                code = cls._EXT_MAP.get(filename[ext + 1:].lower())
                if code:
                    return cls(code)
        if content_type:
            ct = content_type.lower()
            for prefix, code in cls._MIME_MAP.items():
                if ct.startswith(prefix):
                    return cls(code)
        return cls(cls.OTHER)
