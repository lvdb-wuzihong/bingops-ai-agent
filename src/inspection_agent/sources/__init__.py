"""数据源封装层：pipeline 取数的唯一入口（双轨制，docs/project-charter.md §2）。

- pipeline（确定性规则流水线）→ sources/ 直连 REST：平台 REST + 外部系统原生 API；
- bot（LLM 对话 agent）→ mcpclient + MCP 工具白名单（bot/tools.py，只读）；
- 禁止交叉复制：新数据能力优先加在平台 service 层（REST 与 MCP 工具同源）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from ..config import AppConfig
from .bingops import BingopsSource
from .gitlab import GitLabSource
from .platform_client import PlatformClient
from .prometheus import PrometheusSource

logger = logging.getLogger(__name__)


class SourceError(RuntimeError):
    """数据源装配失败（配置/凭据缺失）。"""


@dataclass
class Sources:
    """pipeline 取数源集合；gitlab 允许为 None（git 线索降级）。"""

    bingops: BingopsSource
    prometheus: PrometheusSource
    gitlab: GitLabSource | None


def build_sources(config: AppConfig) -> Sources:
    """按配置装配数据源；凭据从环境变量读取（config 仅存 env 名）。

    bingops/prometheus 必需（缺失抛 SourceError）；gitlab 可选（缺失 git 线索降级）。
    """
    platform_client = PlatformClient.from_env(config.platform)
    bingops = BingopsSource(platform_client, list_limit_max=config.query.list_limit_max)

    prom_cfg = config.external.get("prometheus")
    if prom_cfg is None or not prom_cfg.base_url.strip():
        raise SourceError("external.prometheus.base_url 未配置（步骤③ 指标取数必需）")
    prometheus = PrometheusSource(prom_cfg.base_url, timeout_sec=prom_cfg.timeout_sec)

    gitlab: GitLabSource | None = None
    git_cfg = config.external.get("gitlab")
    git_token = os.environ.get(git_cfg.token_env) if (git_cfg and git_cfg.token_env) else None
    if git_cfg is not None and git_cfg.base_url.strip() and git_token:
        gitlab = GitLabSource(git_cfg.base_url, token=git_token, timeout_sec=git_cfg.timeout_sec)
    else:
        logger.info("external.gitlab 未配置或缺少 token：git 线索跳过")

    return Sources(bingops=bingops, prometheus=prometheus, gitlab=gitlab)


__all__ = [
    "SourceError",
    "build_sources",
    "PlatformClient",
    "BingopsSource",
    "PrometheusSource",
    "GitLabSource",
]
