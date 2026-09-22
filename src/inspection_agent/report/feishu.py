"""飞书出站（P2-charter）：统一走平台 `send_feishu_message` MCP 写工具。

红线（charter §1.1/§5）：编排层不直连飞书、零飞书凭据；平台持 bot 身份，
`BINGOPS_MCP_WRITE_ENABLED` 总开关 + `BINGOPS_MCP_FEISHU_ALLOWED_CHATS` 白名单收窄目标群。
失败抛 FeishuOutboundError，由 runner 降级（产物已落盘，仅日志）。
"""

from __future__ import annotations

import logging
import os

from ..config import AppConfig
from ..mcpclient import MCPError, MCPServerPool

logger = logging.getLogger(__name__)


class FeishuOutboundError(RuntimeError):
    """飞书出站失败（配置缺失/未启用/平台拒绝）。"""


def resolve_report_chat_id(config: AppConfig) -> str | None:
    """日报推送目标群（从配置指定的环境变量读取）；未设置返回 None（报告仅落盘）。"""
    return os.environ.get(config.feishu.chat_id_env) or None


def resolve_report_target(config: AppConfig) -> tuple[str, str] | None:
    """日报推送目标：(target, target_type)；未设置返回 None（报告仅落盘）。

    target_type=chat → 目标为会话 ID（群/单聊）；user → 目标为对方飞书 open_id（按人直发，
    飞书自动落入与机器人的单聊，该目标不受平台 ALLOWED_CHATS 白名单约束）。
    """
    target = os.environ.get(config.feishu.chat_id_env) or None
    if target is None:
        return None
    target_type = config.feishu.report_target_type or config.feishu.target_type
    return target, target_type


async def send_text(
    pool: MCPServerPool, config: AppConfig, chat_id: str, text: str
) -> None:
    """经平台 send_feishu_message 写工具发送文本（确定性调用，非 LLM 工具）。"""
    try:
        conn = pool["bingops"]
    except MCPError as exc:
        raise FeishuOutboundError(str(exc)) from exc
    try:
        await conn.call_tool(
            "send_feishu_message",
            {
                "target": chat_id,
                "target_type": config.feishu.report_target_type or config.feishu.target_type,
                "content": text,
            },
        )
    except MCPError as exc:
        raise FeishuOutboundError(f"飞书出站失败: {exc}") from exc
    logger.info("飞书消息已发送（target=%s，%d 字符）", chat_id, len(text))
