"""快检单测：应用定位（唯一/多命中/零命中）、规则判定产物、无数据与数据缺失容错。

端到端（REST 假服务）覆盖见 tests/test_integration.py 的 quick_check 用例。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from inspection_agent.config import (
    AppConfig,
    ExternalEndpointConfig,
    FeishuOutboundConfig,
    OutputConfig,
    PlatformConfig,
    QueryConfig,
    Thresholds,
    TrendConfig,
)
from inspection_agent.pipeline.quickcheck import QuickCheckError, quick_check
from inspection_agent.sources import Sources
from inspection_agent.sources.prometheus import MetricSeries

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)

APPS = [
    {"id": 1, "name": "订单中心", "app_code": "order", "team": "t1", "repo_url": ""},
    {"id": 2, "name": "支付网关", "app_code": "pay", "team": "t1", "repo_url": ""},
]

RESOURCES_BY_APP = {
    1: [{"id": 101, "name": "ecs-order-03", "model_code": "ecs", "fields": {"ip": "10.0.0.3"}}],
    2: [{"id": 103, "name": "ecs-pay-01", "model_code": "ecs", "fields": {"ip": "10.0.0.4"}}],
}


def make_config() -> AppConfig:
    return AppConfig(
        teams=["t1"],
        checks={"host": ["cpu"]},
        thresholds=Thresholds(),
        prom_queries={
            "host": {
                "cpu": "100 * (1 - avg by (instance) (rate(node_cpu_seconds_total"
                "{instance=~\"{selector}\", mode=\"idle\"}[5m]))",
            }
        },
        category_by_model={"ecs": "host"},
        label_map={},
        window_hours=24,
        step="5m",
        step_seconds=300,
        timezone="UTC",
        mcp_servers={},
        query=QueryConfig(),
        output=OutputConfig(out_dir="out"),
        platform=PlatformConfig(),
        external={"prometheus": ExternalEndpointConfig(base_url="http://prom")},
        prometheus_instances={"default": ExternalEndpointConfig(base_url="http://prom")},
        feishu=FeishuOutboundConfig(),
        trend=TrendConfig(enabled=True),
    )


class FakeBingops:
    def __init__(self, apps: list[dict], fail_overview: bool = False) -> None:
        self._apps = apps
        self._fail_overview = fail_overview

    async def list_business_apps(self, team: str | None = None, **_: Any) -> list[dict]:
        return [a for a in self._apps if team is None or a["team"] == team]

    async def get_app_overview(self, app_id: int) -> dict:
        if self._fail_overview:
            raise RuntimeError("platform 500")
        app = next(a for a in self._apps if a["id"] == app_id)
        return {**app, "resources": RESOURCES_BY_APP.get(app_id, [])}

    async def list_job_executions(self) -> list[dict]:
        return []

    async def list_change_tickets(self) -> list[dict]:
        return []


class FakeProm:
    """恒值指标源；raise_on_query=True 时模拟监控端不可达（单指标按无数据处理）。"""

    base_url = "http://prom"
    evidence_path = "/graph"

    def __init__(self, values: list[float] | None = None, raise_on_query: bool = False) -> None:
        self._values = values or []
        self._raise = raise_on_query

    async def query_range(self, expr, start, end, step_seconds):
        if self._raise:
            from inspection_agent.sources.errors import SourceError

            raise SourceError("prometheus down")
        points = max(1, int((end - start).total_seconds() // step_seconds))
        return [MetricSeries(labels={"instance": "x"}, values=list(self._values[:points]))]


def make_sources(
    values: list[float] | None = None,
    fail_overview: bool = False,
    raise_on_query: bool = False,
) -> Sources:
    return Sources(
        bingops=FakeBingops(APPS, fail_overview=fail_overview),
        prometheus={"default": FakeProm(values, raise_on_query=raise_on_query)},
        default_prometheus="default",
        prometheus_routes={},
        gitlab=None,
    )


async def test_quick_check_reports_rule_anomaly_with_evidence():
    """CPU 恒 85（>80 持续超 30min）→ 规则引擎判定 cpu_high warning，附证据链接。"""
    result = await quick_check(
        "订单中心", make_config(), make_sources([85.0] * 64), hours=3, now=NOW
    )
    assert result["app"]["name"] == "订单中心"
    assert result["data_missing"] is None
    assert result["window"]["hours"] == 3
    cpu = next(a for a in result["anomalies"] if a["rule"] == "cpu_high")
    assert cpu["severity"] == "warning" and cpu["observed"] == 85.0 and cpu["threshold"] == 80.0
    assert cpu["evidence_url"].startswith("http://prom/graph?")
    overview = {o["metric"]: o for o in result["metrics_overview"]}
    assert overview["cpu"]["max"] == 85.0


async def test_quick_check_healthy_app_has_no_anomaly():
    result = await quick_check(
        "支付网关", make_config(), make_sources([30.0] * 64), hours=3, now=NOW
    )
    assert result["anomalies"] == []
    assert result["metrics_overview"][0]["max"] == 30.0


async def test_quick_check_metric_down_marks_no_data_not_missing():
    """监控查询失败：单指标按无数据处理（不编造），应用级不标缺失。"""
    result = await quick_check("order", make_config(), make_sources(None, raise_on_query=True), now=NOW)
    assert result["data_missing"] is None
    assert result["anomalies"] == []
    assert result["metrics_overview"] == [{"resource": "ecs-order-03", "metric": "cpu", "data": "无数据"}]


async def test_quick_check_overview_down_marks_data_missing():
    """应用全貌拉取失败：应用级数据缺失声明（容错红线）。"""
    result = await quick_check("订单中心", make_config(), make_sources([], fail_overview=True), now=NOW)
    assert result["data_missing"] and "数据缺失" in result["data_missing"]
    assert result["anomalies"] == []


async def test_quick_check_multiple_matches_lists_candidates():
    """子串命中多个应用（订单中心 / 订单中心灰度）→ 多命中列出候选要求精确指定。"""
    apps = [
        {"id": 1, "name": "订单中心", "app_code": "order", "team": "t1", "repo_url": ""},
        {"id": 3, "name": "订单中心灰度", "app_code": "order-gray", "team": "t1", "repo_url": ""},
    ]
    sources = Sources(
        bingops=FakeBingops(apps),
        prometheus={"default": FakeProm([85.0] * 64)},
        default_prometheus="default",
        prometheus_routes={},
        gitlab=None,
    )
    with pytest.raises(QuickCheckError, match="匹配到多个应用") as exc_info:
        await quick_check("订单中心", make_config(), sources, now=NOW)
    assert "订单中心灰度" in str(exc_info.value)


async def test_quick_check_by_app_id():
    """数字串按应用 ID 精确匹配。"""
    result = await quick_check("2", make_config(), make_sources([30.0] * 64), now=NOW)
    assert result["app"]["id"] == 2 and result["app"]["name"] == "支付网关"


async def test_quick_check_unknown_app_hints_search():
    with pytest.raises(QuickCheckError, match="未找到匹配"):
        await quick_check("不存在应用", make_config(), make_sources([]), now=NOW)
