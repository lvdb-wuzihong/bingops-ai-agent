"""bingops 平台 REST 数据源封装（A/C 组取数，巡检日报所需子集）。

双轨制（docs/project-charter.md §2）：pipeline 取数直连平台 REST（低延迟、可预测），
MCP 工具只留给 bot。端点/参数已按平台源码核对
（bingops/api/v1/{cmdb/apps,jobs,tickets,cmdb/relationships}.py）：
- GET /api/v1/cmdb/apps?team&page&page_size          （权限 cmdb_app:list）
- GET /api/v1/cmdb/apps/{id}、/cmdb/apps/{id}/resources
- GET /api/v1/jobs/executions?status&runbook_id&page&page_size  （权限 job:list；无时间参数，客户端过滤）
- GET /api/v1/tickets?ticket_type=change&page&page_size（权限 ticket:list）
- GET /api/v1/cmdb/resources/{id}/topology?depth     （权限 cmdb_resource:list；nodes+edges）
"""

from __future__ import annotations

from typing import Any

from .platform_client import PlatformClient


class BingopsSource:
    def __init__(self, client: PlatformClient, list_limit_max: int = 100) -> None:
        self._client = client
        self._limit = list_limit_max

    async def list_business_apps(self, team: str) -> list[dict[str, Any]]:
        """按团队拉取应用清单（page_size 取上限，超限依赖 team 收窄，见 SKILL 实现要点 1）。"""
        data = await self._client.get(
            "/api/v1/cmdb/apps", {"team": team, "page": 1, "page_size": self._limit}
        )
        return _page_items(data)

    async def get_app_overview(self, app_id: int | str) -> dict[str, Any]:
        """应用全貌：详情 + 资源清单（两次 REST 合并为 MCP 兼容形态，上层零改动）。"""
        app = await self._client.get(f"/api/v1/cmdb/apps/{app_id}")
        resources = await self._client.get(f"/api/v1/cmdb/apps/{app_id}/resources")
        overview = dict(app) if isinstance(app, dict) else {}
        overview["resources"] = _page_items(resources) if not isinstance(resources, list) else resources
        return overview

    async def get_topology(self, resource_id: int, depth: int = 2) -> dict[str, Any]:
        """资源拓扑子图（nodes + edges）；depth 由调用方收敛（≤3，平台硬顶 3）。"""
        return await self._client.get(
            f"/api/v1/cmdb/resources/{resource_id}/topology", {"depth": depth}
        )

    async def list_job_executions(self) -> list[dict[str, Any]]:
        """执行记录（含 code_ref/target_resources）；REST 无时间参数，collect 客户端按窗口过滤。"""
        data = await self._client.get(
            "/api/v1/jobs/executions", {"page": 1, "page_size": self._limit}
        )
        return _page_items(data)

    async def list_change_tickets(self) -> list[dict[str, Any]]:
        data = await self._client.get(
            "/api/v1/tickets",
            {"ticket_type": "change", "page": 1, "page_size": self._limit},
        )
        return _page_items(data)


def _page_items(data: Any) -> list[dict[str, Any]]:
    """分页响应归一化：data = {items, pagination} / {items | list} / 裸列表。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "list"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []
