"""一次日报 = 一次 agent 运行（SKILL：运行单元）。

流程（双轨制，docs/project-charter.md §2）：LangGraph 图编排（graph/report_graph.py）——
  加载配置 → 步骤①-⑤ 采集（sources REST 直连）→ 步骤⑥ 规则 → P2 线索
  → 契约 JSON → 步骤⑦ 渲染（P1 叙事可降级）→ HTML → OSS → 步骤⑧ 飞书短摘要
  （出站统一走平台 send_feishu_message，经 bingops-mcp 写工具）。
退出码：0 成功；1 运行/推送失败；2 配置错误（供 cron 告警）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import ConfigError, load_config
from .graph.report_graph import build_report_graph
from .pipeline.collect import CollectError
from .report.feishu import resolve_report_target
from .sources import SourceError

logger = logging.getLogger(__name__)


async def run_report(
    config_path: Path,
    report_date: date | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> int:
    """执行一次日报；now 参数仅供测试注入。"""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        logger.error("配置加载失败: %s", exc)
        return 2

    tz = ZoneInfo(config.timezone)
    now = now.astimezone(tz) if now is not None else datetime.now(tz)
    report_date = report_date or (now.date() - timedelta(days=1))

    report_target = None if dry_run else resolve_report_target(config)
    graph = build_report_graph(
        config,
        now=now,
        report_date=report_date,
        dry_run=dry_run,
        report_target=report_target,
    )
    try:
        result = await graph.ainvoke({})
    except (CollectError, SourceError) as exc:
        logger.error("日报生成失败: %s", exc)
        return 1

    artifacts = result["artifacts"]
    paths = f"{artifacts['anomalies']} / {artifacts['report']} / {artifacts['html']}"

    if dry_run:
        print(result["summary_text"])
        print(f"\n[dry-run] 已生成 {paths}（未上传 OSS/未推送飞书）")
        return 0

    if report_target is None:
        print(f"[warn] 未配置环境变量 {config.feishu.chat_id_env}，报告仅落盘：{artifacts['report']}")
        return 0

    if result.get("notify_error"):
        return 1

    suffix = "+OSS 链接" if result.get("oss_url") else ""
    print(f"日报已推送飞书（短摘要{suffix}）；产物：{paths}")
    return 0
