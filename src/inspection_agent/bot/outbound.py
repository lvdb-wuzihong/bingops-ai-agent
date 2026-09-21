"""bot 回复出站（P2-charter）：统一走平台 `send_feishu_message` MCP 写工具。

与 report 出站同通道；chat_id 来自平台转发事件（编排层不持有飞书凭据）。
失败仅记日志（fire-and-forget：平台转发语义下无重投）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..mcpclient import MCPError

if TYPE_CHECKING:
    from ..mcpclient import MCPServerPool

logger = logging.getLogger(__name__)


async def send_reply(
    pool: "MCPServerPool", chat_id: str, text: str, target_type: str = "chat"
) -> None:
    """发送 bot 回复到目标会话；失败仅记日志（平台转发为 fire-and-forget）。"""
    try:
        conn = pool["bingops"]
        await conn.call_tool(
            "send_feishu_message",
            {"target": chat_id, "target_type": target_type, "content": text},
        )
        logger.info("bot 回复已发送（chat=%s，%d 字符）", chat_id, len(text))
    except MCPError as exc:
        logger.error("bot 回复发送失败（chat=%s）: %s", chat_id, exc)
