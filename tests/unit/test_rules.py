"""步骤⑥ 规则引擎单测：每规则命中/未命中/阈值临界（数字与证据契约同验）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from inspection_agent.config import (
    AppConfig,
    ExternalEndpointConfig,
    FeishuOutboundConfig,
    PlatformConfig,
    Thresholds,
    TrendConfig,
)
from inspection_agent.pipeline.collect import AppData, MetricSnapshot, ReportData
from inspection_agent.pipeline.rules import RulesContext, evaluate_all, evaluate_app
from inspection_agent.sources.prometheus import MetricSeries, window_stats

TZ = ZoneInfo("UTC")
WS = datetime(2026, 9, 2, 0, 0, tzinfo=TZ)
WE = WS + timedelta(hours=24)
STEP = 300  # 5m

CTX = RulesContext(
    thresholds=Thresholds(), window_start=WS, window_end=WE,
    trend=TrendConfig(),
)


def make_app(**kwargs) -> AppData:
    defaults = dict(app_id=1, app_name="订单中心", app_code="order", team="t1")
    defaults.update(kwargs)
    return AppData(**defaults)


def series(*values: float) -> list[MetricSeries]:
    return [MetricSeries(labels={"instance": "node-1"}, values=list(values))]


def full_series(value: float, count: int = 288) -> list[MetricSeries]:
    return series(*([value] * count))


def make_snapshot(
    metric: str, values: list[float], resource_name: str = "node-1",
    prom_base_url: str = "",
) -> MetricSnapshot:
    series = [MetricSeries(labels={"instance": resource_name}, values=values)]
    return MetricSnapshot(
        resource_id="r-1",
        resource_name=resource_name,
        category="host",
        metric=metric,
        expr="up{instance=\"node-1\"}",
        step_seconds=STEP,
        stats=window_stats(series),
        series=series,
        prom_base_url=prom_base_url,
    )


def make_report_data(apps: list[AppData]) -> ReportData:
    return ReportData(
        report_date="2026-09-02",
        timezone="UTC",
        window_start=WS,
        window_end=WE,
        teams=["t1"],
        apps=apps,
        ungrouped_changes=[],
        changes_domain_error=None,
        truncated_teams=[],
    )


def assert_valid_anomaly(anomaly: dict) -> None:
    """与 validate_anomalies.py 契约一致的快速断言。"""
    for field in ("rule", "severity", "app_id", "app_name", "resource_id", "resource_name",
                  "metric", "observed", "threshold", "duration_minutes", "evidence_url"):
        assert field in anomaly, f"缺少字段 {field}"
    assert isinstance(anomaly["observed"], (int, float)) and not isinstance(anomaly["observed"], bool)
    for field in ("threshold", "duration_minutes"):
        value = anomaly[field]
        assert value is None or (isinstance(value, (int, float)) and not isinstance(value, bool))
    assert isinstance(anomaly["evidence_url"], str) and anomaly["evidence_url"].strip()


# ---------------------------------------------------------------- cpu_high


def test_cpu_sustained_high_hits():
    app = make_app(metrics=[make_snapshot("cpu", [85.0] * 288)])
    anomalies, risks = evaluate_app(app, CTX)
    assert len(anomalies) == 1 and not risks
    a = anomalies[0]
    assert_valid_anomaly(a)
    assert a["rule"] == "cpu_high" and a["severity"] == "warning"
    assert a["observed"] == 85.0 and a["threshold"] == 80.0
    assert a["duration_minutes"] >= 1439  # 全窗口持续


def test_cpu_short_spike_becomes_risk():
    app = make_app(metrics=[make_snapshot("cpu", [85.0] * 5 + [10.0] * 283)])
    anomalies, risks = evaluate_app(app, CTX)
    assert not anomalies
    assert len(risks) == 1 and risks[0]["observed"] == 85.0 and risks[0]["evidence_url"].strip()


def test_cpu_duration_boundary_exactly_30min_hits():
    app = make_app(metrics=[make_snapshot("cpu", [85.0] * 6 + [10.0] * 282)])
    anomalies, _ = evaluate_app(app, CTX)
    assert len(anomalies) == 1 and anomalies[0]["duration_minutes"] == 30.0


def test_cpu_below_threshold_no_output():
    app = make_app(metrics=[make_snapshot("cpu", [79.0] * 288)])
    anomalies, risks = evaluate_app(app, CTX)
    assert not anomalies and risks  # 79 落在接近阈值带（80-10），只产风险提示


def test_cpu_far_below_nothing():
    app = make_app(metrics=[make_snapshot("cpu", [40.0] * 288)])
    anomalies, risks = evaluate_app(app, CTX)
    assert not anomalies and not risks


# ---------------------------------------------------------------- memory_high


def test_memory_sustained_high_hits():
    app = make_app(metrics=[make_snapshot("memory", [92.0] * 288)])
    anomalies, _ = evaluate_app(app, CTX)
    assert len(anomalies) == 1 and anomalies[0]["rule"] == "memory_high"
    assert anomalies[0]["threshold"] == 90.0


def test_memory_short_spike_becomes_risk():
    app = make_app(metrics=[make_snapshot("memory", [92.0] * 4 + [50.0] * 284)])
    anomalies, risks = evaluate_app(app, CTX)
    assert not anomalies and len(risks) == 1


# ---------------------------------------------------------------- disk_usage


def test_disk_warning_level():
    app = make_app(metrics=[make_snapshot("disk", [86.0] * 288)])
    anomalies, _ = evaluate_app(app, CTX)
    assert len(anomalies) == 1
    assert anomalies[0]["rule"] == "disk_usage" and anomalies[0]["severity"] == "warning"
    assert anomalies[0]["threshold"] == 85.0 and anomalies[0]["duration_minutes"] is None


def test_disk_critical_level():
    app = make_app(metrics=[make_snapshot("disk", [96.0] * 288)])
    anomalies, _ = evaluate_app(app, CTX)
    assert len(anomalies) == 1
    assert anomalies[0]["severity"] == "critical" and anomalies[0]["threshold"] == 95.0


def test_disk_between_mountpoints_takes_worst():
    snapshot = make_snapshot("disk", [96.0] * 288)  # stats.max 跨挂载点取最差
    app = make_app(metrics=[snapshot])
    anomalies, _ = evaluate_app(app, CTX)
    assert anomalies[0]["severity"] == "critical"


# ---------------------------------------------------------------- pod_restart


def test_pod_restart_hit_at_threshold():
    app = make_app(metrics=[make_snapshot("restarts", [0.0, 1.0, 2.0, 3.0])])
    anomalies, _ = evaluate_app(app, CTX)
    assert len(anomalies) == 1 and anomalies[0]["rule"] == "pod_restart"
    assert anomalies[0]["observed"] == 3.0 and anomalies[0]["threshold"] == 3.0


def test_pod_restart_below_threshold_miss():
    app = make_app(metrics=[make_snapshot("restarts", [0.0, 1.0, 2.0])])
    anomalies, _ = evaluate_app(app, CTX)
    assert not anomalies


# ---------------------------------------------------------------- error_rate


def test_error_rate_hit_is_critical():
    app = make_app(metrics=[make_snapshot("error_rate", [0.02] * 288)])
    anomalies, _ = evaluate_app(app, CTX)
    assert len(anomalies) == 1
    assert anomalies[0]["rule"] == "error_rate" and anomalies[0]["severity"] == "critical"
    assert anomalies[0]["observed"] == 0.02 and anomalies[0]["threshold"] == 0.01


def test_error_rate_missing_metric_skips():
    app = make_app()  # 无数据 → 无异常（数据缺失语义）
    anomalies, _ = evaluate_app(app, CTX)
    assert not anomalies


# ---------------------------------------------------------------- change_failed


def test_change_failed_aggregates():
    app = make_app(changes=[
        {"job_id": 11, "ticket_no": "T-77", "status": "failed", "tag": "v1.2.3"},
        {"job_id": 12, "ticket_no": "T-78", "status": "failed", "tag": "v1.2.4"},
        {"job_id": 13, "ticket_no": "T-79", "status": "success", "tag": "v1.2.5"},
    ])
    anomalies, _ = evaluate_app(app, CTX)
    assert len(anomalies) == 1 and anomalies[0]["rule"] == "change_failed"
    assert anomalies[0]["observed"] == 2 and anomalies[0]["evidence_url"] == "T-77"


def test_change_success_only_no_anomaly():
    app = make_app(changes=[{"job_id": 13, "ticket_no": "T-79", "status": "success", "tag": "v"}])
    anomalies, _ = evaluate_app(app, CTX)
    assert not anomalies


# ---------------------------------------------------------------- 整报行为


def test_healthy_app_produces_nothing():
    app = make_app(metrics=[make_snapshot("cpu", [30.0] * 288), make_snapshot("disk", [50.0] * 288)])
    anomalies, risks = evaluate_app(app, CTX)
    assert not anomalies and not risks


def test_evaluate_all_sorts_critical_first():
    from types import SimpleNamespace

    critical_app = make_app(app_id=1, app_name="A应用", metrics=[make_snapshot("disk", [96.0] * 288)])
    warning_app = make_app(app_id=2, app_name="B应用", metrics=[make_snapshot("cpu", [85.0] * 288)])
    sources = SimpleNamespace()
    anomalies, _ = evaluate_all(
        make_report_data([warning_app, critical_app]), _config_stub(), sources
    )
    assert [a["severity"] for a in anomalies] == ["critical", "warning"]


def test_prom_evidence_uses_graph_url():
    ctx = RulesContext(
        thresholds=Thresholds(),
        window_start=WS, window_end=WE, trend=TrendConfig(),
    )
    app = make_app(metrics=[make_snapshot("disk", [96.0] * 288, prom_base_url="http://prom.example.com")])
    anomalies, _ = evaluate_app(app, ctx)
    assert anomalies[0]["evidence_url"].startswith("http://prom.example.com/graph?")
    assert "g0.expr" in anomalies[0]["evidence_url"]


def test_prom_evidence_follows_snapshot_instance():
    """多监控源定向：证据链接指向采集快照所属实例（而非全局地址）。"""
    ctx = RulesContext(
        thresholds=Thresholds(),
        window_start=WS, window_end=WE, trend=TrendConfig(),
    )
    app = make_app(metrics=[
        make_snapshot("disk", [96.0] * 288, prom_base_url="https://vm-waibu.example.com"),
        make_snapshot("cpu", [85.0] * 288, prom_base_url="http://vm-neibu:8428"),
    ])
    anomalies, _ = evaluate_app(app, ctx)
    disk = next(a for a in anomalies if a["rule"] == "disk_usage")
    cpu = next(a for a in anomalies if a["rule"] == "cpu_high")
    assert disk["evidence_url"].startswith("https://vm-waibu.example.com/graph?")
    assert cpu["evidence_url"].startswith("http://vm-neibu:8428/graph?")


def _config_stub():
    return AppConfig(
        teams=["t1"], checks={}, thresholds=Thresholds(), prom_queries={},
        category_by_model={}, label_map={}, window_hours=24, step="5m", step_seconds=300,
        timezone="UTC", mcp_servers={},
        platform=PlatformConfig(), external={"prometheus": ExternalEndpointConfig()},
        feishu=FeishuOutboundConfig(),
        query=None, output=None,  # type: ignore[arg-type]
    )
