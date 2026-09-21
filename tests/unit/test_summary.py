"""P1.5 飞书短摘要单测：趋势 Top5 排序截断、无趋势占位、OSS 链接/失败标注。"""

from __future__ import annotations

from inspection_agent.report.summary import render_summary


def make_struct(trends: list[dict] | None = None) -> dict:
    return {
        "report_date": "2026-09-05",
        "teams": ["平台组"],
        "apps_inspected": 3,
        "anomalies": [{"rule": "disk_usage"}],
        "changes": [{"ticket_no": "T-1"}],
        "alerts": {"unrecovered_total": 1},
        "risks": trends or [],
    }


def make_trend(change_pct: float, *, metric: str = "disk", direction: str = "up") -> dict:
    return {
        "type": "trend",
        "app_name": "订单中心",
        "resource_name": "ecs-order-03",
        "metric": metric,
        "observed": 96.0,
        "base_value": 80.0,
        "change_pct": change_pct,
        "direction": direction,
        "window_days": 7,
        "trend": f"近 7 日均值 80.0 → 昨日 96.0，{'上升' if direction == 'up' else '回落'}",
        "evidence_url": "http://prom.example.com/graph?x=1",
    }


def test_summary_overview_and_trend_top5_sorted():
    trends = [make_trend(10.0, metric=f"m{i}") for i in range(6)]
    trends.append(make_trend(45.0, metric="big"))
    text = render_summary(make_struct(trends), "http://oss/report.html?sig=1")
    assert "巡检应用 3 个" in text
    assert "趋势关注" in text
    assert text.count("▲") + text.count("▼") == 5  # Top5 截断
    assert text.index("big") < text.index("m0")  # 按 |change_pct| 降序，45% 居首
    assert "完整报告：http://oss/report.html?sig=1" in text


def test_summary_no_trend_and_oss_failed():
    text = render_summary(make_struct(), None)
    assert "无显著趋势变动" in text
    assert "完整报告：生成失败，请检查 OSS 配置" in text


def test_summary_down_direction_uses_down_arrow():
    text = render_summary(make_struct([make_trend(-30.0, direction="down")]), "http://oss/r")
    assert "▼" in text
    assert "▲" not in text
    assert "完整报告：http://oss/r" in text
