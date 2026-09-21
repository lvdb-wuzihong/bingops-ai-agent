"""P2 根因线索单测：三类线索构造、对齐逻辑、上一 tag 推断、容错（失败 clues=[]）。"""

from __future__ import annotations

from types import SimpleNamespace

from inspection_agent.config import (
    AppConfig,
    CluesConfig,
    ExternalEndpointConfig,
    FeishuOutboundConfig,
    PlatformConfig,
    Thresholds,
)
from inspection_agent.pipeline.clues import (
    _all_changes,
    _change_clue,
    _find_previous_tag,
    _normalize_upstream,
    attach_clues,
    parse_project,
)
from inspection_agent.pipeline.collect import AppData
from inspection_agent.sources import Sources
from inspection_agent.sources.gitlab import compare_web_url
from inspection_agent.sources.gitlab import parse_project as parse_project_gitlab


def _clues_cfg(**overrides) -> AppConfig:
    base = dict(
        teams=["t1"], checks={}, thresholds=Thresholds(), prom_queries={},
        category_by_model={}, label_map={}, window_hours=24, step="5m", step_seconds=300,
        timezone="UTC", mcp_servers={},
        platform=PlatformConfig(base_url="http://bingops.example.com"),
        external={
            "prometheus": ExternalEndpointConfig(base_url="http://prom.example.com"),
        },
        feishu=FeishuOutboundConfig(),
        query=None, output=None,
    )
    base["clues"] = CluesConfig(**overrides)
    return AppConfig(**base)  # type: ignore[arg-type]


def _sources(topology_result=None, gitlab=None):
    """测试用 Sources：bingops.get_topology 可编程；prometheus 仅占位。"""

    class FakeBingops:
        async def get_topology(self, resource_id, depth=2):
            return topology_result

    return Sources(
        bingops=FakeBingops(),
        prometheus=SimpleNamespace(base_url="http://prom.example.com"),
        gitlab=gitlab,
    )


CFG = _clues_cfg()


def make_app(**kwargs) -> AppData:
    defaults = dict(app_id=17, app_name="订单中心", app_code="order", team="t1")
    defaults.update(kwargs)
    return AppData(**defaults)


def make_anomaly(**kwargs) -> dict:
    base = {
        "rule": "cpu_high", "severity": "warning", "app_id": 17, "app_name": "订单中心",
        "resource_id": "r-1", "resource_name": "ecs-order-03", "metric": "cpu",
        "observed": 85.0, "threshold": 80.0, "duration_minutes": 60.0,
        "evidence_url": "http://prom.example.com/graph?x=1",
    }
    base.update(kwargs)
    return base


def make_change(**kwargs) -> dict:
    base = {"job_id": 11, "ticket_no": "T-77", "app_name": "订单中心", "tag": "v1.2.3",
            "status": "failed", "target_ids": []}
    base.update(kwargs)
    return base


# ---------------------------------------------------------------- 纯函数：变更线索


def test_change_clue_target_resource_hit():
    change = make_change(target_ids=["r-1"])
    clue = _change_clue(make_anomaly(), [change])
    assert clue is not None and clue["type"] == "change"
    assert "v1.2.3" in clue["text"] and "T-77" in clue["text"]
    assert clue["evidence_url"] == "T-77"


def test_change_clue_app_fallback():
    change = make_change(target_ids=[])  # 不带资源对齐 → 应用级回退
    clue = _change_clue(make_anomaly(), [change])
    assert clue is not None and "v1.2.3" in clue["text"]


def test_change_clue_no_match_returns_none():
    other = make_change(app_name="支付网关", target_ids=[])
    assert _change_clue(make_anomaly(), [other]) is None


# ---------------------------------------------------------------- 纯函数：git 线索辅助


def test_parse_project_normalized_repo_url():
    assert parse_project("https://git.example.com/group/order.git") == "group/order"
    assert parse_project("https://git.example.com/group/order/") == "group/order"
    assert parse_project("not-a-url") is None
    assert parse_project("https://git.example.com") is None


def test_compare_web_url():
    url = compare_web_url("https://git.example.com/group/order.git", "v1.2.2", "v1.2.3")
    assert url == "https://git.example.com/group/order/-/compare/v1.2.2...v1.2.3"


def test_find_previous_tag_skips_same_entry_and_empty_tags():
    changes = [
        make_change(tag="v1.2.3"),
        make_change(tag="", job_id=12),
        make_change(tag="v1.2.2", job_id=13),
    ]
    tags = _find_previous_tag(make_anomaly(), changes)
    assert tags == ("v1.2.3", "v1.2.2")


def test_find_previous_tag_none_when_no_earlier_tag():
    changes = [make_change(tag="v1.2.3")]
    assert _find_previous_tag(make_anomaly(), changes) is None


def test_normalize_upstream_known_keys():
    assert _normalize_upstream({"upstream": [{"name": "mysql-01"}, {"name": "redis-02"}]}) == [
        "mysql-01", "redis-02"
    ]
    assert _normalize_upstream({"parents": ["lb-01"]}) == ["lb-01"]
    assert _normalize_upstream("bad") == []


# ---------------------------------------------------------------- attach_clues（异步编排与容错）


class FakeGitLabSource:
    """REST 版 GitLabSource 替身：compare 可编程。"""

    def __init__(self, result: dict | None = None, error: Exception | None = None) -> None:
        self._result = result if result is not None else {"files_count": 0, "commits": []}
        self._error = error

    async def compare(self, project, ref_from, ref_to):
        if self._error is not None:
            raise self._error
        return self._result


class SlowBingops:
    """get_topology 挂起的替身（验证 per-anomaly 超时）。"""

    def __init__(self, delay: float) -> None:
        self._delay = delay

    async def get_topology(self, resource_id, depth=2):
        import asyncio

        await asyncio.sleep(self._delay)
        return {}


def _report_data(apps: list[AppData]):
    from datetime import datetime, timezone

    from inspection_agent.pipeline.collect import ReportData

    ws = datetime(2026, 9, 2, tzinfo=timezone.utc)
    return ReportData(
        report_date="2026-09-02", timezone="UTC", window_start=ws, window_end=ws,
        teams=["t1"], apps=apps, ungrouped_changes=[],
        changes_domain_error=None, truncated_teams=[],
    )


async def test_attach_clues_change():
    app = make_app(changes=[make_change(target_ids=["r-1"])])
    anomalies = [make_anomaly()]
    await attach_clues(anomalies, _report_data([app]), _sources(), CFG)
    types = [c["type"] for c in anomalies[0]["clues"]]
    assert "change" in types
    assert "topology" not in types  # 拓扑无上游 → 跳过
    assert "git" not in types  # git_compare 数据不满足（无更早 tag）


async def test_attach_clues_git_with_previous_tag():
    app = make_app(
        repo_url="https://git.example.com/group/order.git",
        changes=[make_change(tag="v1.2.3", target_ids=["r-1"]),
                 make_change(tag="v1.2.2", job_id=13, status="success")],
    )
    anomalies = [make_anomaly()]
    gitlab = FakeGitLabSource(
        result={"files_count": 12, "commits": ["fix: order bug"]}
    )
    await attach_clues(anomalies, _report_data([app]), _sources(gitlab=gitlab), CFG)
    clues = anomalies[0]["clues"]
    git = next(c for c in clues if c["type"] == "git")
    assert "v1.2.2→v1.2.3" in git["text"] and "12 个文件" in git["text"]
    assert git["evidence_url"].endswith("/-/compare/v1.2.2...v1.2.3")


async def test_attach_clues_topology_timeout_keeps_empty():
    app = make_app()
    anomalies = [make_anomaly(resource_id=101)]
    # get_topology 挂起 5s > per_anomaly_timeout 0.1s → clues 保持空
    sources = _sources(
        topology_result={"upstream": [{"name": "mysql-01"}]},
        gitlab=None,
    )
    sources.bingops = SlowBingops(delay=5.0)
    await attach_clues(anomalies, _report_data([app]), sources, _clues_cfg(per_anomaly_timeout_sec=0.1))
    assert anomalies[0]["clues"] == []


async def test_attach_clues_disabled_noop():
    app = make_app(changes=[make_change(target_ids=["r-1"])])
    anomalies = [make_anomaly()]
    await attach_clues(anomalies, _report_data([app]), _sources(), _clues_cfg(enabled=False))
    assert anomalies[0]["clues"] == []


def test_all_changes_includes_ungrouped():
    app = make_app(changes=[make_change()])
    data = _report_data([app])
    data.ungrouped_changes = [make_change(job_id=99)]
    assert len(_all_changes(data)) == 2


def test_parse_project_gitlab_alias():
    assert parse_project_gitlab("https://git.example.com/g/o.git") == "g/o"
