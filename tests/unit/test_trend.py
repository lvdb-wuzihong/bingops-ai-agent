"""P1.5 趋势计算单测：日聚合、变化率命中/未命中/边界、min_delta 护栏、方向判定。"""

from __future__ import annotations

from datetime import datetime, timezone

from inspection_agent.pipeline.trend import DailyStat, daily_stats, trend_change
from inspection_agent.sources.prometheus import MetricSeries

TZ = timezone.utc
WS = datetime(2026, 8, 26, 0, 0, tzinfo=TZ)  # 8 天窗口起点（前 7 天基线 + 昨天）


def series_linear(start: float, end: float, days: int = 8, points_per_day: int = 24):
    count = points_per_day * days
    values = [start + (end - start) * i / (count - 1) for i in range(count)]
    times = [WS.timestamp() + i * 3600 for i in range(count)]
    return [MetricSeries(labels={}, times=times, values=values)]


def test_daily_stats_buckets_by_day():
    stats = daily_stats(series_linear(60, 96), WS, days=8)
    assert len(stats) == 8
    assert stats[0].date == "2026-08-26"
    assert stats[-1].date == "2026-09-02"
    assert stats[0].avg is not None and stats[-1].avg is not None


def test_trend_change_hits_up():
    change = trend_change(daily_stats(series_linear(60, 96), WS, days=8), 20, 5)
    assert change is not None
    assert change["direction"] == "up"
    assert change["change_pct"] >= 20
    assert change["current_value"] > change["base_value"]


def test_trend_change_flat_miss():
    count = 24 * 8
    flat = [
        MetricSeries(
            labels={},
            times=[WS.timestamp() + i * 3600 for i in range(count)],
            values=[50.0] * count,
        )
    ]
    assert trend_change(daily_stats(flat, WS, days=8), 20, 5) is None


def test_trend_change_small_delta_guard():
    """2% → 3%：变化率 50% 超阈值，但绝对差 1pp < min_delta → 不报（防小基数抖动）。"""
    daily = [DailyStat(date=f"2026-08-{d:02d}", avg=2.0, max=2.0, p95=2.0) for d in range(26, 33)]
    daily.append(DailyStat(date="2026-09-02", avg=3.0, max=3.0, p95=3.0))
    assert trend_change(daily, 20, 5) is None


def test_trend_change_down_direction():
    daily = [
        DailyStat(date=f"2026-08-{d:02d}", avg=90.0, max=90.0, p95=90.0)
        for d in range(26, 33)
    ]
    daily.append(DailyStat(date="2026-09-02", avg=60.0, max=60.0, p95=60.0))
    change = trend_change(daily, 20, 5)
    assert change is not None and change["direction"] == "down"


def test_trend_change_base_zero_returns_none():
    daily = [
        DailyStat(date=f"2026-08-{d:02d}", avg=0.0, max=0.0, p95=0.0) for d in range(26, 33)
    ]
    daily.append(DailyStat(date="2026-09-02", avg=50.0, max=50.0, p95=50.0))
    assert trend_change(daily, 20, 5) is None


def test_trend_change_empty_baseline_days_ignored():
    daily = [DailyStat(date="2026-08-26", avg=None, max=None, p95=None)]
    daily += [
        DailyStat(date=f"2026-08-{d:02d}", avg=80.0, max=80.0, p95=80.0)
        for d in range(27, 33)
    ]
    daily.append(DailyStat(date="2026-09-02", avg=100.0, max=100.0, p95=100.0))
    change = trend_change(daily, 20, 5)
    assert change is not None and change["base_value"] == 80.0


def test_trend_change_insufficient_days_returns_none():
    daily = [DailyStat(date="2026-09-02", avg=96.0, max=96.0, p95=96.0)]
    assert trend_change(daily, 20, 5) is None
