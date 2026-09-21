"""步骤①-⑤ 数据采集编排。

容错红线（SKILL）：
- 单应用失败/超时 → 该应用标记"数据缺失"，其余照常出报，绝不中断整报；
- 单个数据域（告警/变更）失败 → 该域整体降级为空并记因；
- 全局 deadline：超时取消未完成应用任务，输出已完成部分 + 缺失声明。

联调校准点：
- get_app_overview 仅含服务级 CI：Pod/主机 selector 依赖 category_by_model + label_map，
  联调期按真实 fields 校准；
- REST list 端点无时间参数：客户端按自然日窗口精确过滤。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..config import AppConfig
from ..sources import Sources
from ..sources.bingops import BingopsSource
from ..sources.errors import SourceError
from ..sources.prometheus import (
    MetricSeries,
    PrometheusSource,
    WindowStats,
    window_stats,
)
from .trend import DailyStat, daily_stats

logger = logging.getLogger(__name__)


class CollectError(RuntimeError):
    """整报级失败（无法产出任何报告）。"""


@dataclass
class MetricSnapshot:
    """单资源（或应用级）单检查项的窗口观测。"""

    resource_id: Any
    resource_name: str
    category: str
    metric: str
    expr: str
    step_seconds: int
    stats: WindowStats | None
    series: list[MetricSeries]
    daily: list[DailyStat] = field(default_factory=list)  # P1.5 趋势日聚合（trend.enabled 时填充）
    prom_base_url: str = ""  # 采集来源实例 base_url（多监控源定向，证据链接用）
    evidence_path: str = "/graph"  # 来源实例图表 UI 路径（/graph=Prometheus，/vmui/=VM）


@dataclass
class AppData:
    app_id: Any
    app_name: str
    app_code: str
    team: str
    repo_url: str = ""
    resources: list[dict[str, Any]] = field(default_factory=list)
    metrics: list[MetricSnapshot] = field(default_factory=list)
    unmapped_resources: list[str] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    missing_reason: str | None = None  # 非 None = 数据缺失应用（容错产物）


@dataclass
class ReportData:
    report_date: str
    timezone: str
    window_start: datetime
    window_end: datetime
    teams: list[str]
    apps: list[AppData]
    ungrouped_changes: list[dict[str, Any]]
    changes_domain_error: str | None
    truncated_teams: list[str]


async def collect(
    config: AppConfig, sources: Sources, report_date: date, now: datetime | None = None
) -> ReportData:
    """执行步骤①-⑤，返回整报数据（失败域降级为空并记因，绝不中断）。

    注：步骤④ 告警统计已随夜莺下线移除（2026-09），接入新告警源时恢复该步骤。
    """
    tz = ZoneInfo(config.timezone)
    now = now.astimezone(tz) if now is not None else datetime.now(tz)
    window_start = datetime.combine(report_date, time.min, tzinfo=tz)
    window_end = window_start + timedelta(hours=config.window_hours)

    bingops = sources.bingops

    apps, truncated = await _collect_apps(config, bingops)
    if not apps:
        raise CollectError("步骤① 未获得任何巡检对象（团队清单全部失败或为空）")

    jobs, tickets, changes_err = await _collect_changes(
        config, bingops, window_start, window_end, now
    )

    await _collect_apps_metrics(apps, config, sources, window_start, window_end)

    ungrouped_changes = _attribute_changes(jobs, tickets, apps)

    return ReportData(
        report_date=report_date.isoformat(),
        timezone=config.timezone,
        window_start=window_start,
        window_end=window_end,
        teams=list(config.teams),
        apps=apps,
        ungrouped_changes=ungrouped_changes,
        changes_domain_error=changes_err,
        truncated_teams=truncated,
    )


# ---------------------------------------------------------------- 步骤① 对象清单


async def _collect_apps(
    config: AppConfig, bingops: BingopsSource
) -> tuple[list[AppData], list[str]]:
    apps: list[AppData] = []
    truncated: list[str] = []
    seen: set[Any] = set()
    for team in config.teams:
        try:
            items = await bingops.list_business_apps(team)
        except SourceError as exc:
            logger.error("步骤① 团队 %s 应用清单拉取失败: %s", team, exc)
            continue
        if len(items) >= config.query.list_limit_max:
            truncated.append(team)  # 服务端仅第 1 页：达到上限视为可能截断（治理提示）
        for item in items:
            if not isinstance(item, dict):
                continue
            app_id = item.get("id")
            if app_id is None or app_id in seen:
                continue
            seen.add(app_id)
            apps.append(
                AppData(
                    app_id=app_id,
                    app_name=str(item.get("name") or app_id),
                    app_code=str(item.get("app_code") or ""),
                    team=team,
                    repo_url=str(item.get("repo_url") or ""),
                )
            )
    return apps, truncated


# ---------------------------------------------------------------- 步骤⑤ 变更摘要


async def _collect_changes(
    config: AppConfig, bingops: BingopsSource,
    window_start: datetime, window_end: datetime, now: datetime,
) -> tuple[list[dict], list[dict], str | None]:
    """REST 无时间参数：拉首页后客户端按自然日窗口精确过滤。"""
    try:
        jobs = await bingops.list_job_executions()
        tickets = await bingops.list_change_tickets()
    except SourceError as exc:
        logger.error("步骤⑤ 变更数据拉取失败（降级为空）: %s", exc)
        return [], [], str(exc)
    jobs = [j for j in jobs if _in_window(_parse_dt(j.get("created_at")), window_start, window_end)]
    tickets = [
        t for t in tickets if _in_window(_parse_dt(t.get("created_at")), window_start, window_end)
    ]
    return jobs, tickets, None


# ---------------------------------------------------------------- 步骤②③ 检查项展开 + 指标


async def _collect_apps_metrics(
    apps: list[AppData],
    config: AppConfig,
    sources: Sources,
    window_start: datetime,
    window_end: datetime,
) -> None:
    """按应用并发采集；单应用失败/超时只标记该应用，绝不中断整报。"""
    sem = asyncio.Semaphore(max(1, config.query.app_concurrency))
    task_to_app = {
        asyncio.ensure_future(
            _collect_app(app, config, sources, window_start, window_end, sem)
        ): app
        for app in apps
    }
    done, pending = await asyncio.wait(task_to_app, timeout=config.query.report_deadline_sec)
    for task in done:
        exc = task.exception() if not task.cancelled() else None
        if exc is not None:  # _collect_app 内部已兑底；此处防御意外逃逸
            app = task_to_app[task]
            logger.error("步骤②③ 应用 %s 采集意外失败", app.app_name, exc_info=exc)
            app.missing_reason = f"数据缺失：意外错误 {exc}"
    if pending:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in pending:
            app = task_to_app[task]
            app.missing_reason = f"整报全局超时（deadline {config.query.report_deadline_sec:.0f}s）"
        logger.warning("整报全局超时：%d 个应用未在 deadline 内完成", len(pending))


async def _collect_app(
    app: AppData,
    config: AppConfig,
    sources: Sources,
    window_start: datetime,
    window_end: datetime,
    sem: asyncio.Semaphore,
) -> AppData:
    async with sem:
        try:
            await asyncio.wait_for(
                _app_inner(app, config, sources, window_start, window_end),
                timeout=config.query.per_app_timeout_sec,
            )
        except TimeoutError:
            app.missing_reason = f"单应用查询超时（{config.query.per_app_timeout_sec:.0f}s）"
            logger.warning("步骤②③ 应用 %s 单应用超时，标记数据缺失", app.app_name)
        except SourceError as exc:
            app.missing_reason = f"数据缺失：{exc}"
            logger.warning("步骤②③ 应用 %s 拉取失败: %s", app.app_name, exc)
        except Exception as exc:  # noqa: BLE001 单应用兜底，绝不上抛中断整报
            logger.exception("步骤②③ 应用 %s 意外错误", app.app_name)
            app.missing_reason = f"数据缺失：意外错误 {exc}"
    return app


async def _app_inner(
    app: AppData, config: AppConfig, sources: Sources,
    window_start: datetime, window_end: datetime,
) -> None:
    overview = await sources.bingops.get_app_overview(app.app_id)
    resources = overview.get("resources") if isinstance(overview, dict) else None
    app.resources = [r for r in (resources or []) if isinstance(r, dict)]

    # 多监控源定向巡检：按应用 team 路由到所属监控实例（指标与趋势同源）
    prom = sources.prometheus_for(app.team)

    for resource in app.resources:
        category = config.category_by_model.get(str(resource.get("model_code", "")).lower())
        if category is None:
            # 未映射资源：治理信号（待办建议输出），与场景 6 无主资源清单同源
            app.unmapped_resources.append(str(resource.get("name") or resource.get("id")))
            continue
        for check in config.checks.get(category, []):
            if check == "error_rate":
                continue  # 应用级检查，循环外统一处理
            template = (config.prom_queries.get(category) or {}).get(check)
            if not template:
                logger.debug("检查项 %s/%s 无 PromQL 模板，跳过", category, check)
                continue
            snapshot = await _query_metric(
                prom, config, app, resource, category, check, template, window_start, window_end
            )

            if snapshot is not None:
                if config.trend.enabled:
                    snapshot.daily = await _query_daily(
                        prom, app, snapshot.expr,
                        trend_start=window_start - timedelta(days=config.trend.days),
                        window_end=window_end,
                        days=config.trend.days + 1,
                        config=config,
                    )
                app.metrics.append(snapshot)

    error_rate_template = (config.prom_queries.get("app") or {}).get("error_rate")
    if error_rate_template and app.app_code:
        snapshot = await _query_error_rate(
            prom, config, app, error_rate_template, window_start, window_end
        )
        if config.trend.enabled:
            snapshot.daily = await _query_daily(
                prom, app, snapshot.expr,
                trend_start=window_start - timedelta(days=config.trend.days),
                window_end=window_end,
                days=config.trend.days + 1,
                config=config,
            )
        app.metrics.append(snapshot)


async def _query_daily(
    prom: PrometheusSource,
    app: AppData,
    expr: str,
    trend_start: datetime,
    window_end: datetime,
    days: int,
    config: AppConfig,
) -> list[DailyStat]:
    """趋势窗口查询（基线+昨日，step 1h）并按自然日聚合；失败按无趋势处理。"""
    try:
        series = await prom.query_range(expr, trend_start, window_end, config.trend.step_seconds)
    except SourceError as exc:
        logger.warning("趋势查询 %s 失败（按无趋势处理）: %s", app.app_name, exc)
        return []
    return daily_stats(series, trend_start, days)


async def _query_metric(
    prom: PrometheusSource,
    config: AppConfig,
    app: AppData,
    resource: dict[str, Any],
    category: str,
    check: str,
    template: str,
    window_start: datetime,
    window_end: datetime,
) -> MetricSnapshot | None:
    """单资源单检查项查询；无 selector/无模板返回 None，查询失败按无数据处理。"""
    selector = _build_selector(resource)
    if not selector:
        logger.debug("资源 %s 无法构建 selector，跳过 %s", resource.get("name"), check)
        return None
    expr = _render_template(template, selector, config.window_hours)
    return await _run_metric_query(
        prom, app, resource_id=resource.get("id"), resource_name=str(resource.get("name") or ""),
        category=category, metric=check, expr=expr, ws=window_start, we=window_end, config=config,
    )


async def _query_error_rate(
    prom: PrometheusSource,
    config: AppConfig,
    app: AppData,
    template: str,
    window_start: datetime,
    window_end: datetime,
) -> MetricSnapshot:
    label = config.label_map.get("app", "app_code")
    expr = _render_template(template, f"^{re.escape(app.app_code)}$", config.window_hours, label=label)
    return await _run_metric_query(
        prom, app, resource_id=app.app_id, resource_name=app.app_name,
        category="app", metric="error_rate", expr=expr, ws=window_start, we=window_end, config=config,
    )


async def _run_metric_query(
    prom: PrometheusSource,
    app: AppData,
    resource_id: Any,
    resource_name: str,
    category: str,
    metric: str,
    expr: str,
    ws: datetime,
    we: datetime,
    config: AppConfig,
) -> MetricSnapshot:
    try:
        series = await prom.query_range(expr, ws, we, config.step_seconds)
    except SourceError as exc:
        # 单指标失败按"无数据"处理（渲染为"无数据"，禁止编造），不中断该应用其他检查项
        logger.warning("指标 %s/%s 查询失败（按无数据处理）: %s", app.app_name, metric, exc)
        series = []
    return MetricSnapshot(
        resource_id=resource_id,
        resource_name=resource_name,
        category=category,
        metric=metric,
        expr=expr,
        step_seconds=config.step_seconds,
        stats=window_stats(series),
        series=series,
        prom_base_url=prom.base_url,    # 证据链接跟随应用所属实例（多监控源定向）
        evidence_path=prom.evidence_path,
    )


async def _query_daily(
    prom: PrometheusSource,
    app: AppData,
    expr: str,
    trend_start: datetime,
    window_end: datetime,
    days: int,
    config: AppConfig,
) -> list[DailyStat]:
    """趋势窗口查询（8 天/step 1h）并按自然日聚合；失败按无趋势处理，不影响异常判定。"""
    try:
        series = await prom.query_range(expr, trend_start, window_end, config.trend.step_seconds)
    except SourceError as exc:
        logger.warning("趋势查询 %s 失败（按无趋势处理）: %s", app.app_name, exc)
        return []
    return daily_stats(series, trend_start, days)


def _render_template(template: str, selector: str, window_hours: int, label: str = "") -> str:
    """渲染 PromQL 模板：用 replace 而非 str.format——模板含 PromQL 字面 {}，format 会解析失败。"""
    text = template.replace("{selector}", selector).replace("{window}", _window_token(window_hours))
    if label:
        text = text.replace("{label}", label)
    return text


def _build_selector(resource: dict[str, Any]) -> str:
    """联调校准点：selector 取值优先 fields.ip/private_ip/instance_id，退回资源名。"""
    fields = resource.get("fields") or {}
    value = (
        fields.get("ip")
        or fields.get("private_ip")
        or fields.get("instance_id")
        or resource.get("name")
    )
    if not value:
        return ""
    return f"^{re.escape(str(value))}$"


def _window_token(hours: int) -> str:
    return f"{hours}h"


# ---------------------------------------------------------------- 变更归属（→ 应用）


def _attribute_changes(
    jobs: list[dict], tickets: list[dict], apps: list[AppData]
) -> list[dict]:
    """变更归属：优先工单 business_app_id，其次 job.target_resources 与应用资源求交。"""
    by_id: dict[Any, AppData] = {}
    for app in apps:
        by_id.setdefault(app.app_id, app)
        by_id.setdefault(str(app.app_id), app)
    ticket_by_id = {
        t.get("id"): t for t in tickets if isinstance(t, dict) and t.get("id") is not None
    }
    used_tickets: set[Any] = set()
    ungrouped: list[dict] = []

    for job in jobs:
        if not isinstance(job, dict):
            continue
        ticket = ticket_by_id.get(job.get("ticket_id"))
        app = None
        if ticket is not None:
            used_tickets.add(ticket.get("id"))
            app = by_id.get(ticket.get("business_app_id")) or by_id.get(
                str(ticket.get("business_app_id"))
            )
        if app is None:
            app = _match_by_target_resources(job.get("target_resources") or [], apps)
        entry = _change_entry(job=job, ticket=ticket)
        entry["app_name"] = app.app_name if app is not None else ""
        if app is not None:
            app.changes.append(entry)
        else:
            ungrouped.append(entry)

    for ticket in tickets:  # 无执行记录的变更工单（如纯审批）也纳入回顾
        if not isinstance(ticket, dict) or ticket.get("id") in used_tickets:
            continue
        app = by_id.get(ticket.get("business_app_id")) or by_id.get(
            str(ticket.get("business_app_id"))
        )
        entry = _change_entry(job=None, ticket=ticket)
        entry["app_name"] = app.app_name if app is not None else ""
        if app is not None:
            app.changes.append(entry)
        else:
            ungrouped.append(entry)
    return ungrouped


def _match_by_target_resources(target_ids: list[Any], apps: list[AppData]) -> AppData | None:
    for candidate in apps:
        resource_ids = {r.get("id") for r in candidate.resources if isinstance(r, dict)}
        if resource_ids and any(t in resource_ids for t in target_ids):
            return candidate
    return None


def _change_entry(job: dict | None, ticket: dict | None) -> dict[str, Any]:
    job = job or {}
    ticket = ticket or {}
    return {
        "job_id": job.get("id"),
        "ticket_no": str(ticket.get("ticket_no") or ""),
        "app_name": "",
        "tag": str(job.get("code_ref") or ticket.get("code_ref") or ""),
        "status": str(job.get("status") or ticket.get("status") or ""),
        "triggered_by": str(job.get("triggered_by") or ""),  # 平台返回 user id，无双字段（联调校准）
        "started_at": _iso(job.get("started_at")),
        "finished_at": _iso(job.get("finished_at")),
        "created_at": _iso(job.get("created_at") or ticket.get("created_at")),
        "env": "",  # 待平台字段（联调校准）
        # P2 线索用：目标资源 ID（变更↔异常资源对齐）；工单侧为 target_resource_ids
        "target_ids": list(job.get("target_resources") or ticket.get("target_resource_ids") or []),
    }


# ---------------------------------------------------------------- 时间工具


def _parse_dt(value: Any) -> datetime | None:
    """兼容 ISO 字符串 / datetime / Unix 秒；naive 一律视为 UTC（平台返回 UTC）。"""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=ZoneInfo("UTC"))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=ZoneInfo("UTC"))
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo("UTC"))
    return None


def _in_window(value: datetime | None, window_start: datetime, window_end: datetime) -> bool:
    return value is not None and window_start <= value < window_end


def _iso(value: Any) -> str:
    parsed = _parse_dt(value)
    return parsed.isoformat(timespec="seconds") if parsed else ""
