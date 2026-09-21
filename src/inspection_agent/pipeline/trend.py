"""趋势对比计算（纯函数）：昨日 vs 前 N 日均值（口径 = reference.md §9）。

- base_value = 前 N 日均值（不含昨日，空日忽略）；
- observed（current）= 昨日均值；
- change_pct = (current-base)/base×100，base=0 时不产出趋势项；
- 命中：|change_pct| ≥ threshold 且 |current-base| ≥ min_delta（防小基数抖动）。
所有数字来自规则链路（SKILL 红线 3）；趋势查询失败/数据缺失按无趋势处理。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..sources.prometheus import MetricSeries, percentile_nearest_rank


@dataclass(frozen=True)
class DailyStat:
    """单自然日聚合（无数据日各值为 None）。"""

    date: str
    avg: float | None
    max: float | None
    p95: float | None


def daily_stats(
    series: list[MetricSeries], window_start: datetime, days: int
) -> list[DailyStat]:
    """按自然日聚合趋势窗口序列（日界按 window_start 起点推算，时区由调用方保证）。"""
    start_ts = window_start.timestamp()
    buckets: dict[int, list[float]] = {}
    for s in series:
        for ts, value in zip(s.times, s.values):
            index = int((ts - start_ts) // 86400)
            if 0 <= index < days:
                buckets.setdefault(index, []).append(value)
    stats: list[DailyStat] = []
    for index in range(days):
        day = (window_start + timedelta(days=index)).date().isoformat()
        values = buckets.get(index) or []
        if not values:
            stats.append(DailyStat(date=day, avg=None, max=None, p95=None))
            continue
        stats.append(
            DailyStat(
                date=day,
                avg=sum(values) / len(values),
                max=max(values),
                p95=percentile_nearest_rank(sorted(values), 0.95),
            )
        )
    return stats


def trend_change(
    daily: list[DailyStat], threshold_pct: float, min_delta: float
) -> dict[str, float | str] | None:
    """昨日 vs 前 N 日均值；未命中阈值/护栏或数据不足返回 None。

    daily 顺序必须为窗口日起点顺序（最后一个元素 = 昨日）。
    """
    if len(daily) < 2:
        return None
    current = daily[-1]
    base_days = [d for d in daily[:-1] if d.avg is not None]
    if current.avg is None or not base_days:
        return None
    base = sum(d.avg for d in base_days) / len(base_days)
    delta = current.avg - base
    if base == 0:
        return None  # 基线为 0 无法计算变化率（如新增实例），不产出趋势项
    change_pct = delta / base * 100.0
    if abs(change_pct) < threshold_pct or abs(delta) < min_delta:
        return None
    return {
        "current_value": current.avg,
        "base_value": base,
        "change_pct": change_pct,
        "direction": "up" if delta > 0 else "down",
    }
