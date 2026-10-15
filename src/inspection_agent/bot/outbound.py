"""bot 回复出站（P2-charter）：统一走平台飞书卡片/文本 MCP 写工具。

- 出站拓扑：编排层不持有飞书凭据、不处理飞书协议；回复经平台
  `send_feishu_card` / `update_feishu_card` / `send_feishu_message` 出口。
- 一期形态：先发占位卡（正在分析），回答生成后刷新为结果卡（富文本分块渲染）；
  占位卡发送失败或刷新失败时降级为纯文本回复。
- 失败仅记日志（fire-and-forget：平台转发语义下无重投）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..mcpclient import MCPError

if TYPE_CHECKING:
    from ..mcpclient import MCPServerPool

logger = logging.getLogger(__name__)

# 单元素 markdown 上限（飞书卡片元素长度约束）；块数上限防超平台 30KB 卡片体积
_MD_CHUNK_MAX = 3000
_MAX_CHUNKS = 18
_TRUNCATED_HINT = "…（内容过长已截断，完整结果请查看巡检报告/日报）"


def _split_markdown(text: str, max_len: int = _MD_CHUNK_MAX) -> list[str]:
    """按行切块（不在行中间截断）；单行超长硬切；块数超限截断并附提示。"""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > max_len:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:max_len])
            line = line[max_len:]
        if len(current) + len(line) > max_len:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    if len(chunks) > _MAX_CHUNKS:
        chunks = chunks[:_MAX_CHUNKS] + [_TRUNCATED_HINT]
    return chunks or ["（无内容）"]


def build_working_card(*, title: str = "巡检助手") -> dict:
    """占位卡：处理中状态（发送后经 update_card 刷新为结果卡）。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "grey"},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "正在分析，请稍候…"}},
        ],
    }


def build_answer_card(text: str, *, title: str = "巡检助手") -> dict:
    """结果卡：标题 + 分块 markdown 正文（富文本渲染，超长自动截断）。"""
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": chunk}}
        for chunk in _split_markdown(text)
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
        "elements": elements,
    }


async def send_card(
    pool: "MCPServerPool", chat_id: str, card: dict, target_type: str = "chat"
) -> str | None:
    """发送卡片；成功返回 message_id（刷新句柄），失败记日志返回 None。"""
    try:
        conn = pool["bingops"]
        result = await conn.call_tool(
            "send_feishu_card",
            {"target": chat_id, "target_type": target_type, "card": card},
        )
        message_id = (
            str((result or {}).get("message_id") or "") if isinstance(result, dict) else ""
        )
        logger.info("bot 占位卡已发送（chat=%s，msg=%s）", chat_id, message_id or "-")
        return message_id or None
    except MCPError as exc:
        logger.error("bot 卡片发送失败（chat=%s）: %s", chat_id, exc)
        return None


async def update_card(pool: "MCPServerPool", message_id: str, card: dict) -> bool:
    """刷新卡片为结果形态；失败记日志返回 False（调用方降级另行发送）。"""
    try:
        conn = pool["bingops"]
        await conn.call_tool("update_feishu_card", {"message_id": message_id, "card": card})
        logger.info("bot 结果卡已刷新（msg=%s）", message_id)
        return True
    except MCPError as exc:
        logger.error("bot 卡片刷新失败（msg=%s）: %s", message_id, exc)
        return False


async def send_reply(
    pool: "MCPServerPool", chat_id: str, text: str, target_type: str = "chat"
) -> None:
    """纯文本回复（卡片链路失败时的降级路径）；失败仅记日志。"""
    try:
        conn = pool["bingops"]
        await conn.call_tool(
            "send_feishu_message",
            {"target": chat_id, "target_type": target_type, "content": text},
        )
        logger.info("bot 文本回复已发送（chat=%s，%d 字符）", chat_id, len(text))
    except MCPError as exc:
        logger.error("bot 回复发送失败（chat=%s）: %s", chat_id, exc)
