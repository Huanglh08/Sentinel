"""对象存储底层 SPI 与实现：1:1 翻译 ragent ObjectStorageClient 及其 S3/OSS 实现。

- ObjectStorageClient：SPI（只认 bucket/key 裸操作）
- S3ObjectStorageClient：boto3 实现（懒加载，未装 boto3 时抛清晰错误）
- OssObjectStorageClient：oss2 实现（懒加载）
- LocalObjectStorageClient：本地文件系统实现（Python 扩展，便于无云环境运行；
  语义与 S3 path-style 一致：{base}/{bucket}/{key}）
"""
from __future__ import annotations

import logging
import os
import shutil
from typing import BinaryIO, Protocol

from core.storage.config import OssConfig, RagStorageProperties, S3Config

_logger = logging.getLogger("sentinel.storage.client")


class ObjectStorageClient(Protocol):
    """1:1 翻译 ragent ObjectStorageClient。"""

    def stream_put(self, bucket: str, key: str, content: BinaryIO, size: int, content_type: str | None) -> None:
        ...

    def reliable_put(self, bucket: str, key: str, content: BinaryIO, size: int, content_type: str | None) -> None:
        ...

    def get_object(self, bucket: str, key: str) -> BinaryIO:
        ...

    def delete_object(self, bucket: str, key: str) -> None:
        ...

    def delete_by_prefix(self, bucket: str, prefix: str) -> None:
        ...

    def object_exists(self, bucket: str, key: str) -> bool:
        ...

    def bucket_exists(self, bucket: str) -> bool:
        ...

    def create_bucket(self, bucket: str) -> None:
        ...

    def set_bucket_public_read(self, bucket: str) -> None:
        ...

    def build_public_url(self, bucket: str, key: str) -> str:
        ...


# ---------------------------------------------------------------------------
# S3 实现
# ---------------------------------------------------------------------------
class S3ObjectStorageClient:
    """1:1 翻译 ragent S3ObjectStorageClient（boto3）。"""

    def __init__(self, props: RagStorageProperties) -> None:
        self.props = props
        self._s3 = None
        self.public_base_url = props.s3.resolve_public_url()

    @property
    def s3(self):
        if self._s3 is None:
            try:
                import boto3
            except ImportError as e:  # pragma: no cover
                raise ImportError("S3ObjectStorageClient 需要 boto3，请执行：pip install boto3") from e
            cfg = self.props.s3
            self._s3 = boto3.client(
                "s3",
                endpoint_url=cfg.endpoint or None,
                region_name=cfg.region or None,
                aws_access_key_id=cfg.access_key or None,
                aws_secret_access_key=cfg.secret_key or None,
            )
        return self._s3

    def stream_put(self, bucket, key, content, size, content_type):
        # boto3 put_object 会缓冲，但接口语义对齐；如需零堆可改预签名 URL（与 Java 一致）
        self.reliable_put(bucket, key, content, size, content_type)

    def reliable_put(self, bucket, key, content, size, content_type):
        extra = {"ContentType": content_type} if content_type else {}
        self.s3.put_object(Bucket=bucket, Key=key, Body=content, **extra)

    def get_object(self, bucket, key):
        resp = self.s3.get_object(Bucket=bucket, Key=key)
        return resp["Body"]

    def delete_object(self, bucket, key):
        self.s3.delete_object(Bucket=bucket, Key=key)

    def delete_by_prefix(self, bucket, prefix):
        if not self.bucket_exists(bucket):
            return
        cleared = 0
        token = None
        while True:
            kwargs = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self.s3.list_objects_v2(**kwargs)
            keys = [{"Key": o["Key"]} for o in resp.get("Contents", [])]
            if keys:
                self.s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})
                cleared += len(keys)
            token = resp.get("NextContinuationToken") if resp.get("IsTruncated") else None
            if not token:
                break
        _logger.info("已清空前缀 bucket=%s, prefix=%s, 对象数=%d", bucket, prefix, cleared)

    def object_exists(self, bucket, key):
        try:
            self.s3.head_object(Bucket=bucket, Key=key)
            return True
        except Exception as e:  # noqa: BLE001
            if getattr(e, "response", {}).get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return False
            raise

    def bucket_exists(self, bucket):
        try:
            self.s3.head_bucket(Bucket=bucket)
            return True
        except Exception:  # noqa: BLE001
            return False

    def create_bucket(self, bucket):
        try:
            self.s3.create_bucket(Bucket=bucket)
        except Exception as e:  # noqa: BLE001
            if "BucketAlreadyOwnedByYou" in str(e) or "BucketAlreadyExists" in str(e):
                return
            raise

    def set_bucket_public_read(self, bucket):
        policy = (
            '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
            '"Principal":{"AWS":["*"]},"Action":["s3:GetObject"],'
            '"Resource":["arn:aws:s3:::%s/*"]}]}' % bucket
        )
        self.s3.put_bucket_policy(Bucket=bucket, Policy=policy)

    def build_public_url(self, bucket, key):
        base = self.public_base_url.rstrip("/") if self.public_base_url else ""
        if not base:
            base = self.props.s3.endpoint.rstrip("/")
        return f"{base}/{bucket}/{key}"


# ---------------------------------------------------------------------------
# OSS 实现
# ---------------------------------------------------------------------------
class OssObjectStorageClient:
    """1:1 翻译 ragent OssObjectStorageClient（oss2）。"""

    def __init__(self, props: RagStorageProperties) -> None:
        self.props = props
        self._oss = None

    @property
    def oss(self):
        if self._oss is None:
            try:
                import oss2
            except ImportError as e:  # pragma: no cover
                raise ImportError("OssObjectStorageClient 需要 oss2，请执行：pip install oss2") from e
            cfg: OssConfig = self.props.oss
            auth = oss2.Auth(cfg.access_key, cfg.secret_key)
            self._oss = oss2.Bucket(auth, cfg.endpoint, self.props.kb_bucket)
        return self._oss

    def stream_put(self, bucket, key, content, size, content_type):
        self.reliable_put(bucket, key, content, size, content_type)

    def reliable_put(self, bucket, key, content, size, content_type):
        headers = {"Content-Length": str(size)}
        if content_type:
            headers["Content-Type"] = content_type
        self.oss.put_object(key, content, headers=headers)

    def get_object(self, bucket, key):
        return self.oss.get_object(key).read()

    def delete_object(self, bucket, key):
        self.oss.delete_object(key)

    def delete_by_prefix(self, bucket, prefix):
        if not self.bucket_exists(bucket):
            return
        cleared = 0
        while True:
            keys = [o.key for o in self.oss.list_objects(prefix=prefix).object_list]
            if not keys:
                break
            self.oss.batch_delete_objects(keys)
            cleared += len(keys)
        _logger.info("已清空前缀 bucket=%s, prefix=%s, 对象数=%d", bucket, prefix, cleared)

    def object_exists(self, bucket, key):
        return self.oss.object_exists(key)

    def bucket_exists(self, bucket):
        try:
            self.oss.get_bucket_info()
            return True
        except Exception:  # noqa: BLE001
            return False

    def create_bucket(self, bucket):
        try:
            self.oss.create_bucket()
        except Exception as e:  # noqa: BLE001
            if "BucketAlreadyExists" in str(e):
                return
            raise

    def set_bucket_public_read(self, bucket):
        self.oss.put_bucket_acl(oss2.OBJECT_ACL_PUBLIC_READ)

    def build_public_url(self, bucket, key):
        base = self.props.oss.public_url.rstrip("/") if self.props.oss.public_url else self._virtual_host(bucket)
        return f"{base}/{key}"

    def _virtual_host(self, bucket):
        endpoint = self.props.oss.endpoint.rstrip("/")
        if "://" in endpoint:
            scheme, host = endpoint.split("://", 1)
            return f"{scheme}://{bucket}.{host}"
        return f"https://{bucket}.{endpoint}"


# ---------------------------------------------------------------------------
# 本地文件系统实现（Python 扩展，便于无云环境运行）
# ---------------------------------------------------------------------------
class LocalObjectStorageClient:
    """1:1 语义对齐 S3 path-style：{local_base}/{bucket}/{key}。"""

    def __init__(self, props: RagStorageProperties) -> None:
        self.base = props.local_base

    def _path(self, bucket, key):
        return os.path.join(self.base, bucket, key)

    def stream_put(self, bucket, key, content, size, content_type):
        self.reliable_put(bucket, key, content, size, content_type)

    def reliable_put(self, bucket, key, content, size, content_type):
        path = self._path(bucket, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if hasattr(content, "read"):
            with open(path, "wb") as f:
                shutil.copyfileobj(content, f)
        else:
            with open(path, "wb") as f:
                f.write(content)

    def get_object(self, bucket, key):
        return open(self._path(bucket, key), "rb")

    def delete_object(self, bucket, key):
        path = self._path(bucket, key)
        if os.path.exists(path):
            os.remove(path)

    def delete_by_prefix(self, bucket, prefix):
        root = os.path.join(self.base, bucket)
        target = os.path.join(root, prefix)
        if not os.path.exists(target):
            return
        cleared = 0
        for dirpath, _, files in os.walk(target):
            for fn in files:
                os.remove(os.path.join(dirpath, fn))
                cleared += 1
        _logger.info("已清空前缀 bucket=%s, prefix=%s, 对象数=%d", bucket, prefix, cleared)

    def object_exists(self, bucket, key):
        return os.path.exists(self._path(bucket, key))

    def bucket_exists(self, bucket):
        return os.path.isdir(os.path.join(self.base, bucket))

    def create_bucket(self, bucket):
        os.makedirs(os.path.join(self.base, bucket), exist_ok=True)

    def set_bucket_public_read(self, bucket):
        return None  # 本地文件系统天然可读

    def build_public_url(self, bucket, key):
        return f"file://{self._path(bucket, key)}"


def build_client(props: RagStorageProperties) -> ObjectStorageClient:
    """1:1 翻译 ragent @ConditionalOnProperty 二选一注册逻辑。"""
    t = (props.type or "local").lower()
    if t == "s3":
        return S3ObjectStorageClient(props)
    if t == "oss":
        return OssObjectStorageClient(props)
    return LocalObjectStorageClient(props)
