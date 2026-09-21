"""bingops 平台 REST 客户端（pipeline 取数路径，双轨制：pipeline 禁用 MCP 取数）。

鉴权（源码核对 bingops/api/v1/auth.py + api/dependencies.py）：
- JWT Bearer：Authorization: Bearer <token>；权限码由 ai_agent 角色承载
  （cmdb_app:list / job:list / ticket:list / cmdb_resource:list）；
- 两种凭据模式：
  1) 直接 token：BINGOPS_AGENT_TOKEN（联调期）；
  2) 用户名/密码：BINGOPS_AGENT_USERNAME/PASSWORD，POST /api/v1/auth/login 换 JWT，
     401 自动重登录并重试一次（生产长期运行）；
- 统一响应体 {code, message, data, request_id}（core/response.py）：code!=0 视为业务失败；
  分页端点 data = {items, pagination}。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

from .errors import PlatformError

logger = logging.getLogger(__name__)


class PlatformClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_sec: float = 30.0,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_sec
        self._token = token or None
        self._username = username or None
        self._password = password or None
        self._login_lock = asyncio.Lock()

    @classmethod
    def from_env(cls, config: Any) -> "PlatformClient":
        """从 AppConfig.platform 构造；凭据按 env 名读取，二选一模式。"""
        base_url = (config.base_url or "").strip()
        if not base_url:
            raise PlatformError("platform.base_url 未配置（pipeline 取数路径）")
        token = os.environ.get(config.agent_token_env) or None
        username = os.environ.get(config.username_env) or None
        password = os.environ.get(config.password_env) or None
        if not token and not (username and password):
            raise PlatformError(
                "平台凭据未配置：需 "
                f"{config.agent_token_env}（直接 token 模式）或 "
                f"{config.username_env}/{config.password_env}（自动登录模式）"
            )
        return cls(
            base_url,
            timeout_sec=config.timeout_sec,
            token=token,
            username=username,
            password=password,
        )

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET 并解统一响应体 data；401 自动重登录并重试一次。"""
        return await self._request("GET", path, params=params)

    async def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        url = f"{self._base}{path}"
        for attempt in (1, 2):
            if self._token is None and self._username:
                await self._ensure_token()
            headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.request(method, url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                raise PlatformError(f"平台请求失败 {path}: {exc}") from exc
            if resp.status_code == 401 and attempt == 1 and self._username:
                logger.info("平台 token 失效，重新登录后重试: %s", path)
                self._token = None
                continue
            try:
                body = resp.json()
            except ValueError as exc:
                raise PlatformError(f"平台返回非 JSON {path}: {resp.text[:200]}") from exc
            if resp.status_code >= 400:
                raise PlatformError(f"平台 {path} HTTP {resp.status_code}: {str(body)[:200]}")
            if isinstance(body, dict) and body.get("code") not in (0, None):
                raise PlatformError(
                    f"平台 {path} 业务失败 code={body.get('code')}: {body.get('message')}"
                )
            return body.get("data") if isinstance(body, dict) else body
        raise PlatformError(f"平台 {path} 鉴权重试后仍失败")

    async def _ensure_token(self) -> None:
        async with self._login_lock:
            if self._token:
                return
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(
                        f"{self._base}/api/v1/auth/login",
                        json={"username": self._username, "password": self._password},
                    )
                    body = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise PlatformError(f"平台登录失败: {exc}") from exc
            data = (body or {}).get("data") or {}
            token = data.get("access_token")
            if not token:
                raise PlatformError(f"平台登录响应缺少 access_token: {str(body)[:200]}")
            self._token = str(token)
            logger.info("平台登录成功（JWT 已缓存）")
