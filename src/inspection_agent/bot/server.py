"""bot HTTP 服务（P2-charter）：接收平台事件转发，编排层不直连飞书。

契约（联调对齐项）：
- 平台 `feishu_event_service` 已完成验签/解密/命令分流（如 /ai 前缀），将属于 agent 的
  消息按规范化事件 POST 到本端点（fire-and-forget，失败仅平台侧记日志）；
- 请求 body：{"event_id": "...", "chat_id": "...", "text": "..."}（schema 联调定稿）；
- 编排层不持有 app_secret、不处理飞书协议；回复经平台 send_feishu_message 出站。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import JSONResponse

from ..config import AppConfig
from ..llm.client import LLMClient
from ..mcpclient import MCPServerPool
from .agent import ChatAgent
from .outbound import send_reply
from .session import SessionStore
from .tools import load_tool_schemas

logger = logging.getLogger(__name__)


async def build_agent(config: AppConfig, pool: MCPServerPool) -> ChatAgent:
    """构建 ChatAgent（生产 lifespan 与测试共用）。"""
    llm = LLMClient.from_config(config.llm)
    schemas = await load_tool_schemas(pool, config.bot)
    return ChatAgent(
        pool,
        llm,
        schemas,
        max_iterations=config.bot.max_tool_iterations,
        tool_result_max_chars=config.bot.tool_result_max_chars,
        session=SessionStore(config.bot.max_history_per_chat),
    )


def create_app(
    config: AppConfig,
    pool: MCPServerPool | None = None,
    agent: ChatAgent | None = None,
) -> FastAPI:
    """组装 bot FastAPI 应用。

    pool/agent 可注入（测试/复用已建实例）；默认在 lifespan 内自建（bot 进程唯一
    事件循环，MCP 连接与请求处理同循环——旧 WS 桥接的跨循环问题随桥接方式切换自然消失）。
    """
    state: dict[str, Any] = {"agent": agent}  # 闭包持有；lifespan 补齐

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        own_pool = pool is None
        active_pool = pool if pool is not None else MCPServerPool(
            config.mcp_servers, timeout_sec=config.query.per_app_timeout_sec
        )
        if own_pool:
            await active_pool.__aenter__()
        try:
            if state["agent"] is None:
                state["agent"] = await build_agent(config, active_pool)
            app.state.agent = state["agent"]
            app.state.pool = active_pool
            logger.info("bot 就绪")
            yield
        finally:
            if own_pool:
                await active_pool.__aexit__(None, None, None)

    app = FastAPI(title="bingops inspection bot", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    @app.post("/feishu/events", status_code=202)
    async def feishu_events(
        payload: dict[str, Any], background: BackgroundTasks
    ) -> JSONResponse:
        event = _parse_event(payload)
        if event is None:
            return JSONResponse(status_code=400, content={"error": "invalid event payload"})
        chat_id, text = event
        bound_agent = state["agent"]
        if bound_agent is None:
            return JSONResponse(status_code=503, content={"error": "bot not ready"})
        bound_pool = pool
        background.add_task(
            _handle, bound_agent, bound_pool, config.feishu.target_type, chat_id, text
        )
        return JSONResponse(status_code=202, content={"accepted": True})

    async def _handle(
        agent: ChatAgent, pool: MCPServerPool, target_type: str,
        chat_id: str, text: str,
    ) -> None:
        try:
            answer = await agent.answer(chat_id, text)
            await send_reply(pool, chat_id, answer, target_type=target_type)
        except Exception:  # noqa: BLE001 fire-and-forget：失败仅记日志，无重投
            logger.exception("bot 处理转发事件失败（chat=%s）", chat_id)

    def _parse_event(payload: dict[str, Any]) -> tuple[str, str] | None:
        """平台转发规范化事件（联调校准）：{"event_id", "chat_id", "text"}。"""
        chat_id = str(payload.get("chat_id") or "").strip()
        text = str(payload.get("text") or "").strip()
        if not chat_id or not text:
            return None
        return chat_id, text

    return app
