"""bot 内置只读本地工具（非 MCP）：巡检工作流确定性子流程的对话暴露面。

- 与 MCP 工具同一白名单闸门：graph/bot_graph.py 的 _execute 先查本地注册表再走 MCP
  前缀路由；本地工具不受 bot.tool_allowlist / 技能 tool_filter 收窄（它们是 agent 本体
  能力，只读且不新增凭据暴露面）；
- 数字合规（红线 3）：quick_inspect 的数字全部来自 rules.evaluate_app 结构化产物，
  latest_report 的数字全部来自日报产物 JSON；LLM 只逐字引用，禁止自行按阈值下结论；
- 只读边界（红线 2）：快检 = sources/ REST 只读采集（双轨制 pipeline 路径），
  日报 = OSS 读对象；无任何写操作。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..config import AppConfig
from ..oss import OSSError, read_report
from ..pipeline.quickcheck import QuickCheckError, quick_check
from ..sources import SourceError, build_sources

logger = logging.getLogger(__name__)


QUICK_INSPECT_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "quick_inspect",
        "description": (
            "单应用实时快检：按巡检检查矩阵拉取最近窗口指标，由规则引擎判定异常与风险"
            "（CPU/内存/磁盘/Pod 重启/错误率/失败变更），返回结构化结论与证据链接。"
            "适合“XX 现在正常吗/有什么问题”类问题。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "应用名称或 app_code（子串匹配；多命中会列出候选要求精确指定）",
                },
                "hours": {
                    "type": "integer",
                    "description": "快检时间窗（小时，1-48，默认 3）",
                },
            },
            "required": ["app"],
        },
    },
}

LATEST_REPORT_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "latest_report",
        "description": (
            "读取巡检日报产物（结构化 JSON：异常列表/风险提示/变更回顾/整体结论），"
            "默认最新一期，可指定日期。完整 HTML 报告见日报推送消息中的 OSS 链接。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "报告日期 YYYY-MM-DD（默认最新一期）",
                },
            },
        },
    },
}


@dataclass(frozen=True)
class LocalTool:
    """内置本地工具：schema（OpenAI function）+ 异步 handler（args dict → 结构化结果）。"""

    schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[Any]]


def build_local_tools(config: AppConfig) -> dict[str, LocalTool]:
    """按配置装配本地工具（bot 启动时一次；handler 闭包持有 config）。"""

    async def quick_inspect(args: dict[str, Any]) -> Any:
        app = str(args.get("app") or "").strip()
        if not app:
            return {"error": "缺少 app 参数（应用名称或 app_code）"}
        try:
            hours = int(args.get("hours") or 0) or None
        except (TypeError, ValueError):
            hours = None
        try:
            sources = build_sources(config)  # 快检属 pipeline 数据路径（REST 直连）
        except SourceError as exc:
            return {"error": f"数据源不可用: {exc}"}
        try:
            return await quick_check(app, config, sources, hours=hours or 3)
        except QuickCheckError as exc:
            return {"error": str(exc)}
        except SourceError as exc:
            return {"error": f"快检数据拉取失败: {exc}"}

    async def latest_report(args: dict[str, Any]) -> Any:
        report_date = str(args.get("date") or "").strip() or None
        try:
            payload = read_report(config.oss, report_date)
        except OSSError as exc:
            return {"error": str(exc)}
        return _slim_report(payload)

    return {
        "quick_inspect": LocalTool(QUICK_INSPECT_SCHEMA, quick_inspect),
        "latest_report": LocalTool(LATEST_REPORT_SCHEMA, latest_report),
    }


def local_tool_schemas(local_tools: dict[str, LocalTool]) -> list[dict[str, Any]]:
    """本地工具 schemas（与 MCP schemas 合并后传入 LLM；名字即闸门 allowed_names 成员）。"""
    return [tool.schema for tool in local_tools.values()]


def _slim_report(payload: dict[str, Any]) -> dict[str, Any]:
    """日报产物瘦身（token 控制）：保留判定核心位，结构化字段原样透传（零数字改写）。"""

    def _change(c: dict[str, Any]) -> dict[str, Any]:
        return {
            "ticket_no": c.get("ticket_no"),
            "app_name": c.get("app_name"),
            "tag": c.get("tag"),
            "status": c.get("status"),
        }

    return {
        "report_date": payload.get("report_date"),
        "report_mode": payload.get("report_mode"),
        "apps_inspected": payload.get("apps_inspected"),
        "apps_missing_data": payload.get("apps_missing_data"),
        "anomalies": payload.get("anomalies"),
        "risks": payload.get("risks"),
        "changes": [_change(c) for c in (payload.get("changes") or []) if isinstance(c, dict)],
        "governance": payload.get("governance"),
    }
