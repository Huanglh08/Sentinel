"""安全认证与权限依赖。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from .config import AppSettings


_bearer = HTTPBearer(auto_error=False)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


class UserContext(BaseModel):
    sub: str
    roles: list[str] = []
    scopes: list[str] = []


class SecurityService:
    def __init__(self, settings: AppSettings):
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self._settings.require_auth)

    def issue_token(self, subject: str, roles: Optional[list[str]] = None, scopes: Optional[list[str]] = None) -> str:
        if not self._settings.jwt_secret:
            raise RuntimeError("JWT secret is not configured")
        now = datetime.now(timezone.utc)
        payload = {
            "sub": subject,
            "roles": roles or [],
            "scopes": scopes or [],
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=self._settings.jwt_ttl_s)).timestamp()),
        }
        return jwt.encode(payload, self._settings.jwt_secret, algorithm="HS256")

    def decode_token(self, token: str) -> UserContext:
        if not self._settings.jwt_secret:
            raise RuntimeError("JWT secret is not configured")
        payload = jwt.decode(token, self._settings.jwt_secret, algorithms=["HS256"])
        return UserContext(
            sub=str(payload.get("sub", "")),
            roles=list(payload.get("roles", [])),
            scopes=list(payload.get("scopes", [])),
        )

    def verify_api_key(self, api_key: str) -> bool:
        return bool(self._settings.api_key) and api_key == self._settings.api_key



def get_settings(request: Request) -> AppSettings:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        settings = AppSettings()
        request.app.state.settings = settings
    return settings



def get_security_service(settings: AppSettings = Depends(get_settings)) -> SecurityService:
    return SecurityService(settings)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    api_key: str | None = Depends(_api_key_header),
    security: SecurityService = Depends(get_security_service),
) -> UserContext:
    if not security.enabled:
        # 匿名模式读网关注入的身份头构建 UserContext（信任边界=内网网关，公网必须 JWT）。
        name = (request.headers.get("X-User-Name") or "").strip()
        if name:
            role = (request.headers.get("X-User-Role") or "").strip()
            return UserContext(sub=name, roles=[role] if role else [])
        return UserContext(sub="anonymous")

    if api_key and security.verify_api_key(api_key):
        # API Key 持有者同样透传身份头（信任边界=key 持有者，无身份头退 sub=api-key）。
        name = (request.headers.get("X-User-Name") or "").strip()
        if name:
            role = (request.headers.get("X-User-Role") or "").strip()
            return UserContext(sub=name, roles=[role] if role else [])
        return UserContext(sub="api-key")

    if credentials and credentials.scheme.lower() == "bearer":
        try:
            return security.decode_token(credentials.credentials)
        except (jwt.InvalidTokenError, RuntimeError) as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")



def require_scopes(*required_scopes: str):
    async def _guard(user: UserContext = Depends(get_current_user)) -> UserContext:
        if not required_scopes:
            return user
        granted = set(user.scopes) | set(user.roles)
        if not all(scope in granted for scope in required_scopes):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return user

    return _guard
