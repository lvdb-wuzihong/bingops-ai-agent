"""步骤⑥ 异常判定（纯函数）。

红线（SKILL 3/5）：所有数字（观测值/阈值/计数/时长）只在此链路产出，
结构化 JSON 是唯一来源；渲染层与任何 LLM 路径只做叙事，不得改写数字与证据链接。
规则与默认阈值对应 SKILL.md 异常判定规则表；阈值全部来自 YAML。
注：alert_unrecovered 规则已随夜莺下线移除（2026-09），接入新告警源时恢复（git 历史可查）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from ..config import AppConfig, Thresholds, TrendConfig
from ..sources import Sources
from ..sources.prometheus import duration_above_minutes_with_step
from .collect import AppData, MetricSnapshot, ReportData
from .trend import trend_change

logger = logging.getLogger(__name__)

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
NEAR_MISS_MARGIN = 10.0  # "接近阈值"风险提示带宽度（百分比点）
_FAILED_STATUS = {"failed", "error", "failure", "fail"}  # 联调校准：平台执行状态枚举


@dataclass(frozen=True)
class RulesContext:
    thresholds: Thresholds
    window_start: datetime
    window_end: datetime
    trend: TrendConfig


def evaluate_all(data: ReportData, config: AppConfig, sources: Sources) -> tuple[list[dict], list[dict]]:
    """整报规则评估：异常列表（critical→warning→info 排序）+ 风险提示列表。"""
    ctx = RulesContext(
        thresholds=config.thresholds,
        window_start=data.window_start,
        window_end=data.window_end,
        trend=config.trend,
    )
    anomalies: list[dict] = []
    risks: list[dict] = []
    for app in data.apps:
        app_anomalies, app_risks = evaluate_app(app, ctx)
        anomalies.extend(app_anomalies)
        risks.extend(app_risks)
    anomalies.sort(key=lambda a: (SEVERITY_ORDER.get(str(a.get("severity")), 9), str(a.get("app_name"))))
    return anomalies, risks


def evaluate_app(app: AppData, ctx: RulesContext) -> tuple[list[dict], list[dict]]:
    """单应用规则评估：指标六规则 + 失败变更聚合。"""
    anomalies: list[dict] = []
    risks: list[dict] = []
    thresholds = ctx.thresholds

    for snapshot in app.metrics:
        if snapshot.stats is None:
            continue  # 无数据语义：渲染层写"无数据"，不产出异常，禁止编造
        evidence = _prom_evidence(ctx, snapshot, snapshot.expr)
        metric = snapshot.metric
        if snapshot.daily and ctx.trend.enabled:
            # P1.5 趋势判定（昨日 vs 前 N 日均值）→ 风险提示（趋势类）；证据用 8 天窗口
            change = trend_change(
                snapshot.daily, ctx.trend.change_pct_threshold, ctx.trend.min_delta
            )
            if change is not None:
                trend_evidence = _prom_evidence(ctx, snapshot, snapshot.expr, days=ctx.trend.days + 1)
                risks.append(_trend_risk(app, snapshot, trend_evidence, change, ctx.trend))
        if metric == "cpu":
            _pct_sustained(
                anomalies, risks, app, snapshot, evidence,
                thresholds.cpu_high_pct, thresholds.cpu_high_duration_min, "cpu_high",
            )
        elif metric == "memory":
            _pct_sustained(
                anomalies, risks, app, snapshot, evidence,
                thresholds.mem_high_pct, thresholds.mem_high_duration_min, "memory_high",
            )
        elif metric == "disk":
            _disk(anomalies, risks, app, snapshot, evidence, thresholds)
        elif metric == "restarts":
            _restarts(anomalies, app, snapshot, evidence, thresholds)
        elif metric == "error_rate":
            _error_rate(anomalies, app, snapshot, evidence, thresholds)
        else:
            logger.debug("未绑定规则的检查项 %s，跳过", metric)

    failed = [c for c in app.changes if str(c.get("status", "")).lower() in _FAILED_STATUS]
    if failed:
        latest = failed[0]
        anomalies.append(
            _anomaly(
                rule="change_failed",
                severity="warning",
                app=app,
                resource_id=app.app_id,
                resource_name="应用级",
                metric="job_failed",
                observed=len(failed),
                threshold=None,
                duration_minutes=None,
                evidence=str(latest.get("ticket_no") or latest.get("job_id") or "job"),
                summary=f"昨日 {len(failed)} 次执行失败（最近 tag: {latest.get('tag') or '无'}）",
            )
        )
    return anomalies, risks


def _pct_sustained(
    anomalies: list[dict],
    risks: list[dict],
    app: AppData,
    snapshot: MetricSnapshot,
    evidence: str,
    pct: float,
    duration_min: float,
    rule: str,
) -> None:
    """CPU/内存：≥阈值持续 N 分钟 → warning；短时超阈值或接近阈值 → 风险提示。"""
    assert snapshot.stats is not None
    duration = duration_above_minutes_with_step(snapshot.series, pct, snapshot.step_seconds)
    if duration >= duration_min:
        anomalies.append(
            _anomaly(
                rule=rule,
                severity="warning",
                app=app,
                resource_id=snapshot.resource_id,
                resource_name=snapshot.resource_name,
                metric=snapshot.metric,
                observed=snapshot.stats.max,
                threshold=pct,
                duration_minutes=round(duration, 1),
                evidence=evidence,
            )
        )
    elif snapshot.stats.max >= pct:
        risks.append(
            _risk(
                app, snapshot, evidence, observed=snapshot.stats.max,
                trend=f"短时超过 {pct:g}%，最长持续 {duration:.0f} 分钟（阈值 {duration_min:g} 分钟）",
            )
        )
    elif snapshot.stats.p95 >= pct - NEAR_MISS_MARGIN:
        risks.append(
            _risk(
                app, snapshot, evidence, observed=snapshot.stats.p95,
                trend=f"p95 接近阈值 {pct:g}%",
            )
        )


def _disk(
    anomalies: list[dict],
    risks: list[dict],
    app: AppData,
    snapshot: MetricSnapshot,
    evidence: str,
    thresholds: Thresholds,
) -> None:
    """磁盘使用率：>crit → critical；>warn → warning；接近阈值 → 风险提示。"""
    assert snapshot.stats is not None
    stats = snapshot.stats
    if stats.max >= thresholds.disk_crit_pct:
        anomalies.append(
            _anomaly(
                rule="disk_usage", severity="critical", app=app,
                resource_id=snapshot.resource_id, resource_name=snapshot.resource_name,
                metric=snapshot.metric, observed=stats.max,
                threshold=thresholds.disk_crit_pct, duration_minutes=None, evidence=evidence,
            )
        )
    elif stats.max >= thresholds.disk_warn_pct:
        anomalies.append(
            _anomaly(
                rule="disk_usage", severity="warning", app=app,
                resource_id=snapshot.resource_id, resource_name=snapshot.resource_name,
                metric=snapshot.metric, observed=stats.max,
                threshold=thresholds.disk_warn_pct, duration_minutes=None, evidence=evidence,
            )
        )
    elif stats.p95 >= thresholds.disk_warn_pct - NEAR_MISS_MARGIN:
        risks.append(
            _risk(app, snapshot, evidence, observed=stats.p95,
                  trend=f"p95 接近阈值 {thresholds.disk_warn_pct:g}%（磁盘增长关注）")
        )


def _restarts(
    anomalies: list[dict], app: AppData, snapshot: MetricSnapshot, evidence: str,
    thresholds: Thresholds,
) -> None:
    """Pod 重启：窗口内 increase 最大值 ≥ 次数阈值 → warning。"""
    assert snapshot.stats is not None
    if snapshot.stats.max >= thresholds.pod_restart_min_count:
        anomalies.append(
            _anomaly(
                rule="pod_restart", severity="warning", app=app,
                resource_id=snapshot.resource_id, resource_name=snapshot.resource_name,
                metric=snapshot.metric, observed=snapshot.stats.max,
                threshold=thresholds.pod_restart_min_count, duration_minutes=None, evidence=evidence,
            )
        )


def _error_rate(
    anomalies: list[dict], app: AppData, snapshot: MetricSnapshot, evidence: str,
    thresholds: Thresholds,
) -> None:
    """服务错误率：窗口内最大比例 ≥ 阈值 → critical（按应用配置，未配置自动跳过）。"""
    assert snapshot.stats is not None
    if snapshot.stats.max >= thresholds.error_rate_5xx:
        anomalies.append(
            _anomaly(
                rule="error_rate", severity="critical", app=app,
                resource_id=snapshot.resource_id, resource_name=snapshot.resource_name,
                metric=snapshot.metric, observed=snapshot.stats.max,
                threshold=thresholds.error_rate_5xx, duration_minutes=None, evidence=evidence,
            )
        )


def _anomaly(
    *,
    rule: str,
    severity: str,
    app: AppData,
    resource_id: Any,
    resource_name: str,
    metric: str,
    observed: float,
    threshold: float | None,
    duration_minutes: float | None,
    evidence: str,
    summary: str = "",
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "rule": rule,
        "severity": severity,
        "app_id": app.app_id,
        "app_name": app.app_name,
        "resource_id": resource_id if resource_id is not None else app.app_id,
        "resource_name": str(resource_name or app.app_name),
        "metric": metric,
        "observed": _num(observed),
        "threshold": _num(threshold) if threshold is not None else None,
        "duration_minutes": _num(duration_minutes) if duration_minutes is not None else None,
        "evidence_url": str(evidence),
    }
    if summary:
        entry["summary"] = str(summary)
    return entry


def _risk(
    app: AppData, snapshot: MetricSnapshot, evidence: str, observed: float, trend: str
) -> dict[str, Any]:
    return {
        "app_id": app.app_id,
        "app_name": app.app_name,
        "resource_name": snapshot.resource_name,
        "metric": snapshot.metric,
        "observed": _num(observed),
        "trend": str(trend),
        "evidence_url": str(evidence),
    }


def _trend_risk(
    app: AppData,
    snapshot: MetricSnapshot,
    evidence: str,
    change: dict[str, float | str],
    trend_cfg: TrendConfig,
) -> dict[str, Any]:
    """趋势类风险提示（P1.5）：落入报告"三、风险提示（趋势类）"。"""
    direction = str(change["direction"])
    change_pct = float(change["change_pct"])  # type: ignore[arg-type]
    days = trend_cfg.days
    text = (
        f"近 {days} 日均值 {float(change['base_value']):.1f} → 昨日 {float(change['current_value']):.1f}，"
        f"{'上升' if direction == 'up' else '回落'} {abs(change_pct):.1f}%"
    )
    risk = _risk(app, snapshot, evidence, observed=float(change["current_value"]), trend=text)  # type: ignore[arg-type]
    risk.update(
        {
            "type": "trend",
            "base_value": _num(change["base_value"]),
            "change_pct": _num(change_pct),
            "direction": direction,
            "window_days": days,
        }
    )
    return risk


def _num(value: float) -> float:
    """强制 JSON number（红线 3 出口约束：字符串数字不允许流出）。"""
    return float(value)


def _prom_evidence(ctx: RulesContext, snapshot: MetricSnapshot, expr: str, days: int = 1) -> str:
    """Prom 图证据链接，指向应用所属监控实例（多监控源定向）；

    窗口时长来自评估上下文，base_url 来自采集快照；
    未配置 base_url 时以表达式本身为证据（契约要求非空，禁止编造）。
    """
    base_url = snapshot.prom_base_url
    if not base_url:
        return expr
    hours = max(1, int((ctx.window_end - ctx.window_start).total_seconds() // 3600)) * days
    params = urlencode({"g0.expr": expr, "g0.range_input": f"{hours}h", "g0.tab": 0})
    return f"{base_url.rstrip('/')}/graph?{params}"
