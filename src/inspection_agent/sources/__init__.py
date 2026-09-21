"""数据源封装层：pipeline 取数的唯一入口（双轨制，docs/project-charter.md §2）。

- pipeline（确定性规则流水线）→ sources/ 直连 REST：平台 REST + 外部系统原生 API；
- bot（LLM 对话 agent）→ mcpclient + MCP 工具白名单（bot/tools.py，只读）；
- 禁止交叉复制：新数据能力优先加在平台 service 层（REST 与 MCP 工具同源）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from ..config import AppConfig, ExternalEndpointConfig
from .bingops import BingopsSource
from .gitlab import GitLabSource
from .platform_client import PlatformClient
from .prometheus import PrometheusSource

logger = logging.getLogger(__name__)


class SourceError(RuntimeError):
    """数据源装配失败（配置/凭据缺失）。"""


@dataclass
class Sources:
    """pipeline 取数源集合。

    多监控源（单 agent 巡检多业务）：prometheus 为命名实例表，
    按 app.team 经 prometheus_for() 定向路由；gitlab 允许为 None（git 线索降级）。
    """

    bingops: BingopsSource
    prometheus: dict[str, PrometheusSource]
    default_prometheus: str
    prometheus_routes: dict[str, str]
    gitlab: GitLabSource | None

    def prometheus_for(self, team: str) -> PrometheusSource:
        """按应用 team 定向取监控实例；未路由/路由缺失时兑底 default 实例。"""
        name = self.prometheus_routes.get(team, self.default_prometheus)
        source = self.prometheus.get(name)
        if source is None:  # 路由表指向不存在的实例（config 层已拦，双保险）
            name = self.default_prometheus
            source = self.prometheus[name]
        return source

    @property
    def prometheus_default(self) -> PrometheusSource:
        """默认实例（无路由语义的场景使用）。"""
        return self.prometheus[self.default_prometheus]


def build_sources(config: AppConfig) -> Sources:
    """按配置装配数据源；凭据从环境变量读取（config 仅存 env 名）。

    bingops/prometheus 必需（缺失抛 SourceError）；gitlab 可选（缺失 git 线索降级）。
    """
    platform_client = PlatformClient.from_env(config.platform)
    bingops = BingopsSource(platform_client, list_limit_max=config.query.list_limit_max)

    if not config.prometheus_instances:
        raise SourceError("external.prometheus 未配置（步骤③ 指标取数必需）")

    def _evidence_path(cfg: ExternalEndpointConfig) -> str:
        # 图表 UI 路径：Prometheus=/graph；VictoriaMetrics=/vmui/（集群版 vmselect 同理，
        # base_url 直接填 /select/<accountID>/prometheus 前缀，多租户每租户一个实例条目）
        if cfg.evidence_ui not in ("graph", "vmui"):
            raise SourceError(
                f"external.prometheus 实例 evidence_ui 仅支持 graph / vmui（当前值: {cfg.evidence_ui}）"
            )
        return "/vmui/" if cfg.evidence_ui == "vmui" else "/graph"

    prometheus = {
        name: PrometheusSource(
            cfg.base_url,
            timeout_sec=cfg.timeout_sec,
            evidence_path=_evidence_path(cfg),
        )
        for name, cfg in config.prometheus_instances.items()
    }

    gitlab: GitLabSource | None = None
    git_cfg = config.external.get("gitlab")
    git_token = os.environ.get(git_cfg.token_env) if (git_cfg and git_cfg.token_env) else None
    if git_cfg is not None and git_cfg.base_url.strip() and git_token:
        gitlab = GitLabSource(git_cfg.base_url, token=git_token, timeout_sec=git_cfg.timeout_sec)
    else:
        logger.info("external.gitlab 未配置或缺少 token：git 线索跳过")

    return Sources(
        bingops=bingops,
        prometheus=prometheus,
        default_prometheus=config.default_prometheus,
        prometheus_routes=dict(config.prometheus_routes),
        gitlab=gitlab,
    )


__all__ = [
    "SourceError",
    "build_sources",
    "PlatformClient",
    "BingopsSource",
    "PrometheusSource",
    "GitLabSource",
]
