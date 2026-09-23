"""MCP tools → OpenAI function schemas + 只读白名单（SKILL 红线 2）。

写工具（`add_ticket_comment`/`create_ticket` 与外部系统写工具集）无条件排除；
YAML `bot.tool_allowlist` 只能收窄默认白名单，不可扩写。
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import BotConfig
from ..mcpclient import MCPServerPool

logger = logging.getLogger(__name__)

DEFAULT_ALLOWLIST: dict[str, set[str]] = {
    "bingops": {
        "list_business_apps",
        "get_app_overview",
        "find_app_by_resource",
        "search_resources",
        "get_resource_detail",
        "search_assets",          # 平台新增：跨域全局搜索（应用+资源，IP/主机名先搜一遍）
        "get_models_overview",    # 平台新增：CMDB 模型分类与资源计数总览
        "list_tickets",
        "get_ticket_timeline",
        "list_job_executions",
        "get_change_context",
        "list_freezes",
        # get_ticket_stats / get_resource_stats：平台侧 D 组工具尚未实现（bingops/mcp 为 A/C 组），
        # 白名单交集语义下多列无害（tools/list 交集为空即自动缺席），平台补齐后自动生效
        # "get_ticket_stats",
        # "get_resource_stats",
    },
    # n9e 已下线（2026-09）：接入新告警源时按其 tools/list 恢复白名单条目
    "prometheus": {"execute_range_query", "execute_query", "query_range", "query_instant"},
    "gitlab": {"compare", "list_commits", "get_project"},  # P2 只读；以部署版本 tools/list 为准
}


def effective_allowlist(config: BotConfig) -> dict[str, set[str]]:
    """收窄语义：YAML 提供的 server 取交集，未提及的 server 保持默认白名单；永不扩写。"""
    narrowed = {server: set(tools) for server, tools in DEFAULT_ALLOWLIST.items()}
    for server, tools in (config.tool_allowlist or {}).items():
        narrowed[server] = narrowed.get(server, set()) & set(tools)
    return narrowed


async def load_tool_schemas(pool: MCPServerPool, config: BotConfig) -> list[dict[str, Any]]:
    """对各 MCP server 执行 tools/list，转换为 OpenAI function schemas。

    工具名加 server 前缀（如 bingops__list_business_apps）防跨 server 冲突；
    单 server 发现失败仅跳过，不阻断 bot 启动。
    """
    allowlist = effective_allowlist(config)
    schemas: list[dict[str, Any]] = []
    for server_name, conn in pool.connections().items():
        allowed = allowlist.get(server_name, set())
        if not allowed or conn.session is None:
            continue
        try:
            response = await conn.session.list_tools()
        except Exception as exc:  # noqa: BLE001 单 server 失败不阻断启动
            logger.warning("bot 工具发现失败（跳过 %s）: %s", server_name, exc)
            continue
        for tool in response.tools or []:
            if tool.name not in allowed:
                continue
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": f"{server_name}__{tool.name}",
                        "description": (tool.description or "").strip()[:500],
                        "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                    },
                }
            )
    logger.info("bot 工具发现完成：%d 个只读工具", len(schemas))
    return schemas
