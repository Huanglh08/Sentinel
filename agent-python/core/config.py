"""配置中心与 Agent 运行配置。"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

import httpx
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """服务边界配置，环境变量优先，适合本地与容器部署。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False)

    app_name: str = "sentinel-agent"
    app_version: str = "0.2.0"
    host: str = "0.0.0.0"
    port: int = Field(default=8001, ge=1, le=65535)
    require_auth: bool = False
    api_key: str = ""
    jwt_secret: str = ""
    jwt_ttl_s: int = Field(default=3600, ge=60, le=86400 * 30)
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    rate_limit_per_minute: int = Field(default=60, ge=1, le=100000)
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)
    documents_path: str = ".data/documents.json"
    redis_url: str | None = None
    intent_db_url: str | None = None

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_origins(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


class ConfigurationClient:
    def get(self, key, default=None): ...
    def get_prefix(self, prefix): ...


class NacosConfigClient(ConfigurationClient):
    def __init__(self, server_addr, namespace="", data_id="agent-python.yml", group="DEFAULT_GROUP"):
        self._server = server_addr.rstrip("/")
        self._namespace = namespace
        self._data_id = data_id
        self._group = group
        self._cache = {}
        self._last_load = 0.0
        self._lock = threading.RLock()
        self._ttl_s = 60.0

    def _fetch(self):
        url = f"{self._server}/nacos/v1/cs/configs"
        params = {"dataId": self._data_id, "group": self._group}
        if self._namespace:
            params["namespace"] = self._namespace
        with httpx.Client(timeout=5.0) as c:
            r = c.get(url, params=params)
            r.raise_for_status()
            return self._parse(r.text)

    def _parse(self, text):
        out = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
        return out

    def _ensure_fresh(self):
        with self._lock:
            if time.monotonic() - self._last_load > self._ttl_s:
                try:
                    self._cache = self._fetch()
                except Exception:
                    pass
                self._last_load = time.monotonic()

    def get(self, key, default=None):
        self._ensure_fresh()
        return self._cache.get(key, os.getenv(key, default))

    def get_prefix(self, prefix):
        self._ensure_fresh()
        return {k: v for k, v in self._cache.items() if k.startswith(prefix)}
