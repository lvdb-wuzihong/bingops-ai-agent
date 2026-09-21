"""MCP 客户端池：streamable-http 连接管理。

使用官方 mcp SDK 1.x 客户端（与 bingops 平台 bingops/mcp/server.py 的 1.x FastMCP 同代，
pyproject 已 pin mcp<2）。
错误约定：任何失败抛 MCPError/MCPToolError，由 pipeline 层转成"数据缺失"语义，绝不中断整报。
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from mcp import ClientSession

# 1.29 起新名 streamable_http_client 不再接收 headers/timeout（改为 http_client 工厂），
# 迁移需同步重连逻辑；pin mcp<2 期间继续使用稳定的旧名别名
from mcp.client.streamable_http import streamablehttp_client

from .config import McpServerConfig

logger = logging.getLogger(__name__)


class MCPError(RuntimeError):
    """MCP 连接/配置失败。"""


class MCPToolError(MCPError):
    """工具执行失败（传输异常或 isError 结果）。"""


class MCPConnection:
    """单个 MCP server 的 streamable-http 连接（异步上下文管理器）。"""

    def __init__(self, name: str, cfg: McpServerConfig, timeout_sec: float = 30.0) -> None:
        self.name = name
        self._url = cfg.url
        self._headers = cfg.headers or None
        self._timeout_sec = timeout_sec
        self._http_cm: Any = None
        self._session_cm: Any = None
        self.session: ClientSession | None = None

    async def __aenter__(self) -> "MCPConnection":
        self._http_cm = streamablehttp_client(
            self._url, headers=self._headers, timeout=self._timeout_sec
        )
        read, write, _get_session_id = await self._http_cm.__aenter__()
        self._session_cm = ClientSession(read, write)
        self.session = await self._session_cm.__aenter__()
        await self.session.initialize()
        logger.info("MCP 已连接: %s (%s)", self.name, self._url)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        for cm in (self._session_cm, self._http_cm):
            if cm is not None:
                await cm.__aexit__(exc_type, exc, tb)
        self.session = None

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_sec: float | None = None,
    ) -> Any:
        """调用工具并解析返回（structuredContent 优先，text JSON 兜底）。"""
        if self.session is None:
            raise MCPError(f"[{self.name}] 未连接（请通过 async with 打开连接）")
        timeout = timedelta(seconds=read_timeout_sec) if read_timeout_sec else None
        try:
            result = await self.session.call_tool(
                tool_name, arguments or {}, read_timeout_seconds=timeout
            )
        except Exception as exc:  # 传输层/超时/协议错误统一转换
            raise MCPToolError(f"[{self.name}] 调用 {tool_name} 失败: {exc}") from exc
        if getattr(result, "isError", False):
            raise MCPToolError(f"[{self.name}] {tool_name} 返回错误: {_content_text(result)}")
        return _parse_payload(result, self.name, tool_name)


def _content_text(result: Any) -> str:
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "; ".join(parts) or "(无错误详情)"


def _parse_payload(result: Any, server_name: str, tool_name: str) -> Any:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and set(structured) == {"result"}:
        structured = structured["result"]  # FastMCP 对非 dict 标注返回的包装形态
    if structured is not None:
        return structured
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is None:
            continue
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return {"text": text}
    logger.warning("[%s] %s 无可解析返回内容", server_name, tool_name)
    return None


class MCPServerPool:
    """按名管理多个 MCP server 连接：async with pool: await pool["bingops"].call_tool(...)。"""

    def __init__(self, servers: dict[str, McpServerConfig], timeout_sec: float = 30.0) -> None:
        self._specs = dict(servers)
        self._timeout_sec = timeout_sec
        self._conns: dict[str, MCPConnection] = {}

    async def __aenter__(self) -> "MCPServerPool":
        for name, cfg in self._specs.items():
            conn = MCPConnection(name, cfg, self._timeout_sec)
            await conn.__aenter__()
            self._conns[name] = conn
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        for conn in reversed(list(self._conns.values())):
            try:
                await conn.__aexit__(exc_type, exc, tb)
            except Exception:  # noqa: BLE001 关闭失败不影响主流程
                logger.exception("关闭 MCP 连接失败: %s", conn.name)
        self._conns = {}

    def __getitem__(self, name: str) -> MCPConnection:
        try:
            return self._conns[name]
        except KeyError:
            raise MCPError(f"未配置名为 {name!r} 的 MCP server") from None

    def connections(self) -> dict[str, "MCPConnection"]:
        """已建立连接的只读视图（bot 工具发现用）。"""
        return dict(self._conns)
