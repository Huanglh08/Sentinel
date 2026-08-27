"""存储配置：1:1 翻译 ragent rag.config.RagStorageProperties。

字段严格对齐 Java：
  - type: "s3"（默认）| "oss" | "local"（Python 扩展，便于无云环境本地运行）
  - kb_bucket / asset_bucket：部署级常量，不写进数据
  - s3 / oss：各自端点与凭证
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class S3Config:
    endpoint: str = ""
    region: str = ""
    access_key: str = ""
    secret_key: str = ""
    public_url: str = ""

    def resolve_public_url(self) -> str:
        return self.public_url or self.endpoint


@dataclass
class OssConfig:
    endpoint: str = ""
    access_key: str = ""
    secret_key: str = ""
    public_url: str = ""


@dataclass
class RagStorageProperties:
    """1:1 翻译 ragent RagStorageProperties。"""

    type: str = "local"
    kb_bucket: str = "ragent-kb"
    asset_bucket: str = "ragent-asset"
    s3: S3Config = field(default_factory=S3Config)
    oss: OssConfig = field(default_factory=OssConfig)
    local_base: str = "./data/storage"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "RagStorageProperties":
        """1:1 翻译 ragent @ConfigurationProperties 注入（环境变量优先）。"""
        env = env if env is not None else dict(os.environ)

        def _get(name: str, default: str) -> str:
            return env.get(name, default)

        return cls(
            type=_get("RAG_STORAGE_TYPE", "local").lower(),
            kb_bucket=_get("RAG_STORAGE_KB_BUCKET", "ragent-kb"),
            asset_bucket=_get("RAG_STORAGE_ASSET_BUCKET", "ragent-asset"),
            s3=S3Config(
                endpoint=_get("RAG_STORAGE_S3_ENDPOINT", ""),
                region=_get("RAG_STORAGE_S3_REGION", ""),
                access_key=_get("RAG_STORAGE_S3_ACCESS_KEY", ""),
                secret_key=_get("RAG_STORAGE_S3_SECRET_KEY", ""),
                public_url=_get("RAG_STORAGE_S3_PUBLIC_URL", ""),
            ),
            oss=OssConfig(
                endpoint=_get("RAG_STORAGE_OSS_ENDPOINT", ""),
                access_key=_get("RAG_STORAGE_OSS_ACCESS_KEY", ""),
                secret_key=_get("RAG_STORAGE_OSS_SECRET_KEY", ""),
                public_url=_get("RAG_STORAGE_OSS_PUBLIC_URL", ""),
            ),
            local_base=_get("RAG_STORAGE_LOCAL_BASE", "./data/storage"),
        )
