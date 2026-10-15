"""bot 实时快检（巡检形态核心）：单应用按检查矩阵的规则判定，对话触发的确定性子流程。

红线对齐：
- 双轨制（charter §2）：快检属于 pipeline 数据路径——取数走 sources/ REST 直连，
  判定复用 rules.evaluate_app（规则引擎算数）；bot LLM 只逐字引用产物 JSON（红线 3），
  禁止自行按阈值下结论；
- 容错（SKILL）：单指标失败按无数据处理、应用级失败以 data_missing 声明，绝不编造；
- 趋势跳过：快检聚焦实时判定（8 天趋势看日报，latest_report）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..config import AppConfig
from ..sources import SourceError, Sources
from .collect import AppData, _attribute_changes, _collect_app, _collect_changes
from .rules import RulesContext, evaluate_app

logger = logging.getLogger(__name__)

MIN_HOURS = 1
MAX_HOURS = 48
DEFAULT_HOURS = 3
MAX_CANDIDATES = 10


class QuickCheckError(RuntimeError):
    """快检入口失败（应用未找到/多命中/来源拉取失败）；消息可直接回给用户。"""


async def quick_check(
    app_query: str,
    config: AppConfig,
    sources: Sources,
    *,
    hours: int = DEFAULT_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """对单个应用执行一轮快检：定位应用 → 窗口采集 → 规则判定 → 结构化产物。

    产物与日报异常项同契约（rule/severity/observed/threshold/evidence_url），
    LLM 只做逐字引用与叙事。
    """
    try:
        hours = max(MIN_HOURS, min(MAX_HOURS, int(hours)))
    except (TypeError, ValueError) as exc:
        raise QuickCheckError(f"hours 需为整数（{MIN_HOURS}-{MAX_HOURS}）: {hours!r}") from exc
    tz = ZoneInfo(config.timezone)
    now = now.astimezone(tz) if now is not None else datetime.now(tz)
    window_start = now - timedelta(hours=hours)
    window_end = now

    app = await _find_app(app_query, config, sources)

    # 趋势跳过：快检不做 8 天趋势查询（提速；趋势判定看日报）
    quick_config = replace(config, trend=replace(config.trend, enabled=False))
    await _collect_app(app, quick_config, sources, window_start, window_end, asyncio.Semaphore(1))

    jobs, tickets, changes_err = await _collect_changes(
        config, sources.bingops, window_start, window_end, now
    )
    _attribute_changes(jobs, tickets, [app])  # app.changes 就地填充；无主变更不进快检结论

    ctx = RulesContext(
        thresholds=config.thresholds,
        window_start=window_start,
        window_end=window_end,
        trend=quick_config.trend,
    )
    anomalies, risks = evaluate_app(app, ctx)

    return {
        "app": {
            "id": app.app_id,
            "name": app.app_name,
            "code": app.app_code,
            "team": app.team,
        },
        "window": {
            "start": window_start.isoformat(timespec="seconds"),
            "end": window_end.isoformat(timespec="seconds"),
            "hours": hours,
        },
        "data_missing": app.missing_reason,
        "anomalies": anomalies,
        "risks": risks,
        "metrics_overview": _metrics_overview(app),
        "changes_recent": list(app.changes),
        "changes_domain_error": changes_err,
        "unmapped_resources": list(app.unmapped_resources),
        "notes": "快检不含趋势判定；近 N 日趋势请查巡检日报（latest_report）",
    }


async def _find_app(app_query: str, config: AppConfig, sources: Sources) -> AppData:
    """按名称/编码/ID 定位唯一应用；多命中列出候选，零命中给出检索建议。"""
    query = str(app_query or "").strip()
    if not query:
        raise QuickCheckError("缺少应用标识（应用名称或 app_code）")
    lowered = query.lower()
    candidates: list[AppData] = []
    for team in config.teams:
        try:
            items = await sources.bingops.list_business_apps(team)
        except SourceError as exc:
            raise QuickCheckError(f"应用清单拉取失败（team={team}）: {exc}") from exc
        for item in items:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            name = str(item.get("name") or "")
            code = str(item.get("app_code") or "")
            if lowered in name.lower() or lowered in code.lower() or query == str(item.get("id")):
                candidates.append(
                    AppData(
                        app_id=item.get("id"),
                        app_name=name or str(item.get("id")),
                        app_code=code,
                        team=team,
                        repo_url=str(item.get("repo_url") or ""),
                    )
                )
                if len(candidates) >= MAX_CANDIDATES:
                    break
        if len(candidates) >= MAX_CANDIDATES:
            break
    if not candidates:
        raise QuickCheckError(
            f"未找到匹配 '{query}' 的应用（可先用 search_assets 检索确认应用名称）"
        )
    if len(candidates) > 1:
        listing = "、".join(f"{c.app_name}({c.app_code or c.app_id})" for c in candidates)
        raise QuickCheckError(f"匹配到多个应用，请精确指定：{listing}")
    return candidates[0]


def _metrics_overview(app: AppData) -> list[dict[str, Any]]:
    """窗口指标一览（token 控制：每检查项一行；无数据显式标记，禁止编造）。"""
    overview: list[dict[str, Any]] = []
    for snapshot in app.metrics:
        entry: dict[str, Any] = {
            "resource": snapshot.resource_name,
            "metric": snapshot.metric,
        }
        if snapshot.stats is None:
            entry["data"] = "无数据"
        else:
            entry.update(
                {
                    "avg": snapshot.stats.avg,
                    "max": snapshot.stats.max,
                    "p95": snapshot.stats.p95,
                }
            )
        overview.append(entry)
    return overview
