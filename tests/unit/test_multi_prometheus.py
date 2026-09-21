"""多监控源定向巡检单测：配置两形态解析、路由表校验、prometheus_for 兜底。"""

from __future__ import annotations

from pathlib import Path

import pytest

from inspection_agent.config import ConfigError, load_config
from inspection_agent.sources import Sources

_MIN_YAML = """
teams: [对内团队]
checks: {host: [cpu]}
thresholds: {cpu_high_pct: 80, cpu_high_duration_min: 30, disk_warn_pct: 85,
  disk_crit_pct: 95, mem_high_pct: 90, mem_high_duration_min: 30, pod_restart_min_count: 3}
window: {hours: 24, step: 5m}
platform: {base_url: "http://p:8000"}
external:
  prometheus:
    base_url: http://prom-old:9090
"""


def _write(tmp_path: Path, text: str) -> Path:
    cfg = tmp_path / "inspection.yaml"
    cfg.write_text(text, encoding="utf-8")
    return cfg


def test_single_instance_yaml_wraps_default(tmp_path):
    """旧单实例写法自动包装为名为 default 的实例。"""
    cfg = load_config(_write(tmp_path, _MIN_YAML))
    assert set(cfg.prometheus_instances) == {"default"}
    assert cfg.default_prometheus == "default"
    assert cfg.prometheus_instances["default"].base_url == "http://prom-old:9090"
    assert cfg.prometheus_routes == {}


def test_multi_instance_yaml_with_routes(tmp_path):
    yaml_text = _MIN_YAML.replace(
        "external:\n  prometheus:\n    base_url: http://prom-old:9090",
        "external:\n  prometheus:\n    neibu: {base_url: 'http://vm-neibu:8428'}\n"
        "    waibu: {base_url: 'https://vm-waibu.example.com'}\n"
        "prometheus_routes:\n  对外团队: waibu",
    )
    cfg = load_config(_write(tmp_path, yaml_text))
    assert set(cfg.prometheus_instances) == {"neibu", "waibu"}
    # 无名为 default 的实例：兜底 = 声明顺序第一个
    assert cfg.default_prometheus == "neibu"
    assert cfg.prometheus_routes == {"对外团队": "waibu"}


def test_route_to_unknown_instance_rejected(tmp_path):
    yaml_text = _MIN_YAML.replace(
        "external:\n  prometheus:\n    base_url: http://prom-old:9090",
        "external:\n  prometheus:\n    neibu: {base_url: 'http://vm-neibu:8428'}\n"
        "prometheus_routes:\n  对外团队: 不存在的实例",
    )
    with pytest.raises(ConfigError, match="不存在于 external.prometheus"):
        load_config(_write(tmp_path, yaml_text))


def test_instance_missing_base_url_rejected(tmp_path):
    yaml_text = _MIN_YAML.replace(
        "external:\n  prometheus:\n    base_url: http://prom-old:9090",
        "external:\n  prometheus:\n    neibu: {timeout_sec: 30}",
    )
    with pytest.raises(ConfigError, match="base_url 必填"):
        load_config(_write(tmp_path, yaml_text))


# ---------------------------------------------------------------- prometheus_for 路由


def _sources(instances, routes, default):
    return Sources(
        bingops=None,  # type: ignore[arg-type]  # 路由逻辑不依赖 bingops
        prometheus={
            name: f"prom-source-{name}" for name in instances  # type: ignore[misc]
        },
        default_prometheus=default,
        prometheus_routes=routes,
        gitlab=None,
    )


def test_prometheus_for_routes_by_team():
    sources = _sources(["neibu", "waibu"], {"对外团队": "waibu"}, "neibu")
    assert sources.prometheus_for("对内团队") == "prom-source-neibu"   # 未路由 → 兑底
    assert sources.prometheus_for("对外团队") == "prom-source-waibu"   # 命中路由
    assert sources.prometheus_default == "prom-source-neibu"


def test_prometheus_for_bad_route_falls_back():
    """路由指向不存在实例（config 层已拦，双保险兑底）不抛错。"""
    sources = _sources(["neibu"], {"某团队": "ghost"}, "neibu")
    assert sources.prometheus_for("某团队") == "prom-source-neibu"


def test_single_instance_config_builds_default_sources(tmp_path):
    """build_sources 兼容旧单实例配置。"""
    import os

    from inspection_agent.sources import build_sources

    cfg = load_config(_write(tmp_path, _MIN_YAML))
    # PlatformClient.from_env 需要 token：注入环境变量（平台地址可任意，本测试不发起请求）
    old = os.environ.get("BINGOPS_AGENT_TOKEN")
    os.environ["BINGOPS_AGENT_TOKEN"] = "x" * 10
    try:
        sources = build_sources(cfg)
    finally:
        if old is None:
            os.environ.pop("BINGOPS_AGENT_TOKEN", None)
        else:
            os.environ["BINGOPS_AGENT_TOKEN"] = old
    assert sources.default_prometheus == "default"
    assert set(sources.prometheus) == {"default"}
    assert sources.prometheus_for("任何团队").base_url == "http://prom-old:9090"
