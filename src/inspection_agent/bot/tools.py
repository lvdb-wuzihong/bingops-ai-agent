"""MCP tools → OpenAI function schemas + 只读白名单（SKILL 红线 2）。

写工具（`add_ticket_comment`/`create_ticket` 与外部系统写工具集）无条件排除；
YAML `bot.tool_allowlist` 只能收窄默认白名单，不可扩写；运行时技能（bot/skillregistry.py）
可在此之上按技能声明进一步收窄暴露面（tool_filter），同样永不扩写。
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
        "get_app_topology",       # 平台新增：应用拓扑子图（依赖/被依赖应用+入口/中间件/存储）
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


def effective_allow_names(config: BotConfig) -> set[str]:
    """白名单全集（裸名 ∪ server__name）：运行时技能声明校验与过滤的上界。"""
    names: set[str] = set()
    for server, tools in effective_allowlist(config).items():
        names.update(tools)
        names.update(f"{server}__{tool}" for tool in tools)
    return names


async def load_tool_schemas(
    pool: MCPServerPool,
    config: BotConfig,
    tool_filter: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """对各 MCP server 执行 tools/list，转换为 OpenAI function schemas。

    工具名加 server 前缀（如 bingops__list_business_apps）防跨 server 冲突；
    单 server 发现失败仅跳过，不阻断 bot 启动；tool_filter 非空时为技能声明后的
    暴露面（∩白名单已在 skillregistry 完成），未声明的白名单内工具不暴露给 LLM。
    """
    allowlist = effective_allowlist(config)
    schemas: list[dict[str, Any]] = []
    filtered_out = 0
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
            full_name = f"{server_name}__{tool.name}"
            if (
                tool_filter is not None
                and tool.name not in tool_filter
                and full_name not in tool_filter
            ):
                filtered_out += 1
                continue
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": full_name,
                        "description": (tool.description or "").strip()[:500],
                        "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                    },
                }
            )
    if tool_filter is not None and filtered_out:
        logger.info("bot 技能收窄：%d 个白名单内工具未被技能声明，不暴露给 LLM", filtered_out)
    logger.info("bot 工具发现完成：%d 个只读工具", len(schemas))
    return schemas
