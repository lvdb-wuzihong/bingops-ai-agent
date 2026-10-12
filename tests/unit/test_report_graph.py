"""report_graph 单测：compose 路由（llm 开关）、叙事图内降级、落盘 / dry-run / 出站闸门。

collect 及其取数/规则/线索的端到端行为由 tests/test_integration.py 全链路覆盖；
本文件用轻量 fake 验证图的路由与状态流（节点=现有函数薄包装，路由语义是本次改造增量）。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

import inspection_agent.graph.report_graph as rg
from inspection_agent.config import (
    AppConfig,
    FeishuOutboundConfig,
    LLMConfig,
    OutputConfig,
    PlatformConfig,
    QueryConfig,
    Thresholds,
)
from inspection_agent.graph.report_graph import build_report_graph
from inspection_agent.report.narrate import NarrateError

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
REPORT_DATE = date(2026, 9, 2)


def make_config(tmp_path, *, llm_enabled: bool = False) -> AppConfig:
    return AppConfig(
        teams=["t1"],
        checks={"host": ["cpu"]},
        thresholds=Thresholds(),
        prom_queries={},
        category_by_model={},
        label_map={},
        window_hours=24,
        step="5m",
        step_seconds=300,
        timezone="UTC",
        mcp_servers={},
        query=QueryConfig(),
        output=OutputConfig(out_dir=str(tmp_path / "out")),
        platform=PlatformConfig(),
        external={},
        feishu=FeishuOutboundConfig(),
        llm=LLMConfig(enabled=llm_enabled),
    )


@pytest.fixture()
def fake_pipeline(monkeypatch):
    """collect/规则/线索/结构化打桩：graph 节点只做装配，端到端行为归集成测试。"""
    data = SimpleNamespace(report_date="2026-09-02")

    async def fake_collect(config, sources, report_date, now=None):
        return data

    async def fake_attach_clues(anomalies, data, sources, config):
        return None

    monkeypatch.setattr(rg, "build_sources", lambda config: object())
    monkeypatch.setattr(rg, "collect", fake_collect)
    monkeypatch.setattr(rg, "evaluate_all", lambda data, config, sources: ([], []))
    monkeypatch.setattr(rg, "attach_clues", fake_attach_clues)
    monkeypatch.setattr(
        rg,
        "build_structured",
        lambda data, config, anomalies, risks, generated_at: {"report_date": data.report_date},
    )
    monkeypatch.setattr(rg, "render_report", lambda struct: "模板正文")
    monkeypatch.setattr(rg, "render_html", lambda struct, narrative: "<html>报告</html>")
    monkeypatch.setattr(rg, "render_summary", lambda struct, oss_url: f"摘要（oss={oss_url}）")
    return data


def run_graph(config, *, dry_run, report_target=None):
    return build_report_graph(
        config,
        now=NOW,
        report_date=REPORT_DATE,
        dry_run=dry_run,
        report_target=report_target,
    )


async def test_template_route_when_llm_disabled(tmp_path, fake_pipeline):
    config = make_config(tmp_path)
    result = await run_graph(config, dry_run=True).ainvoke({})
    assert result["struct"]["report_mode"] == "template"
    assert result["text"] == "模板正文"
    assert result["narrative"] is None
    assert result["oss_url"] is None  # dry-run 不上传
    assert "oss=None" in result["summary_text"]
    report_dir = tmp_path / "out" / "2026-09-02"
    assert (report_dir / "anomalies.json").exists()
    assert (report_dir / "report.txt").read_text(encoding="utf-8") == "模板正文"
    assert (report_dir / "report.html").read_text(encoding="utf-8") == "<html>报告</html>"


async def test_narrate_route_when_llm_enabled(tmp_path, fake_pipeline, monkeypatch):
    async def fake_narrative(struct, llm, window_hours=None):
        return "LLM 叙事正文"

    monkeypatch.setattr(rg, "render_narrative", fake_narrative)
    config = make_config(tmp_path, llm_enabled=True)
    result = await run_graph(config, dry_run=True).ainvoke({})
    assert result["struct"]["report_mode"] == "llm"
    assert result["text"] == "LLM 叙事正文"
    assert result["narrative"] == "LLM 叙事正文"


async def test_narrate_failure_degrades_in_graph(tmp_path, fake_pipeline, monkeypatch):
    """LLM 叙事失败 → 图内整篇降级 P0 模板（红线 4），不令整图失败。"""

    async def broken_narrative(struct, llm, window_hours=None):
        raise NarrateError("校验失败")

    monkeypatch.setattr(rg, "render_narrative", broken_narrative)
    config = make_config(tmp_path, llm_enabled=True)
    result = await run_graph(config, dry_run=True).ainvoke({})
    assert result["struct"]["report_mode"] == "template"
    assert result["text"] == "模板正文"
    assert result["narrative"] is None


async def test_notify_skipped_without_target(tmp_path, fake_pipeline):
    """未配置推送目标（env 缺失）：notify 跳过，无 notify_error（报告仅落盘）。"""
    config = make_config(tmp_path)
    result = await run_graph(config, dry_run=False, report_target=None).ainvoke({})
    assert "notify_error" not in result


async def test_notify_error_when_bingops_mcp_missing(tmp_path, fake_pipeline):
    """非 dry-run 且有目标但 mcp_servers.bingops 缺失：notify_error 置位（runner 退出码 1）。"""
    config = make_config(tmp_path)
    result = await run_graph(
        config, dry_run=False, report_target=("oc_test", "chat")
    ).ainvoke({})
    assert result["notify_error"] and "bingops" in result["notify_error"]


async def test_notify_skipped_when_dry_run_even_with_target(tmp_path, fake_pipeline):
    config = make_config(tmp_path)
    result = await run_graph(
        config, dry_run=True, report_target=("oc_test", "chat")
    ).ainvoke({})
    assert "notify_error" not in result
