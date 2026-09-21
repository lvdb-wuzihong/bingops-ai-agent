"""bot 常驻进程装配（python -m inspection_agent bot）。

P2-charter 后的形态：FastAPI 常驻服务（POST /feishu/events 接收平台转发，
GET /healthz 探针），MCP 连接池与请求处理同一事件循环（无跨循环桥接）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import uvicorn

from ..config import ConfigError, load_config
from .server import create_app

logger = logging.getLogger(__name__)


def serve_bot(config_path: Path) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        logger.error("配置加载失败: %s", exc)
        return 2
    if not config.bot.enabled:
        logger.error("bot 未启用（config bot.enabled=false）")
        return 2
    if not config.llm.enabled:
        logger.error("bot 依赖 LLM（config llm.enabled=false）")
        return 2

    logger.info("bot 启动：POST /feishu/events + GET /healthz，端口 %d", config.bot.port)
    uvicorn.run(
        create_app(config),
        host="0.0.0.0",
        port=config.bot.port,
        log_level="info",
    )
    return 0
