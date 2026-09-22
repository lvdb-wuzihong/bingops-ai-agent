"""一次日报 = 一次 agent 运行（SKILL：运行单元）。

流程（双轨制，docs/project-charter.md §2）：
  加载配置 → sources REST 装配 → 步骤①-⑤ 采集 → 步骤⑥ 规则 → P2 线索
  → 契约 JSON → 步骤⑦ 渲染（P1 叙事可降级）→ HTML → OSS → 步骤⑧ 飞书短摘要
  （出站统一走平台 send_feishu_message，经 bingops-mcp 写工具）。
退出码：0 成功；1 运行/推送失败；2 配置错误（供 cron 告警）。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .config import ConfigError, load_config
from .llm.client import LLMError
from .mcpclient import MCPServerPool
from .oss import OSSError, upload_report
from .pipeline.clues import attach_clues
from .pipeline.collect import CollectError, collect
from .pipeline.rules import evaluate_all
from .report.feishu import FeishuOutboundError, resolve_report_target, send_text
from .report.html import render_html
from .report.narrate import NarrateError, render_narrative
from .report.render import render_report
from .report.structured import build_structured
from .report.summary import render_summary
from .sources import SourceError, build_sources

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

    try:
        sources = build_sources(config)  # 双轨制：pipeline 取数走 REST（凭据缺失在此报错）
        data = await collect(config, sources, report_date, now=now)
        anomalies, risks = evaluate_all(data, config, sources)
        # P2 根因线索（只读 REST 查询，dry-run 同样执行；仅对命中异常，per-anomaly 超时保护）
        await attach_clues(anomalies, data, sources, config)
    except (CollectError, SourceError) as exc:
        logger.error("日报生成失败: %s", exc)
        return 1

    struct = build_structured(data, config, anomalies, risks, generated_at=now)

    # 步骤⑦：文本基线 + P1 叙事（校验失败/LLM 断供 → 整篇降级 P0 模板，SKILL 红线 4）
    report_mode = "template"
    text = render_report(struct)
    narrative: str | None = None
    if config.llm.enabled:
        try:
            narrative = await render_narrative(struct, config.llm, window_hours=config.window_hours)
            text = narrative
            report_mode = "llm"
        except (LLMError, NarrateError) as exc:
            logger.warning("步骤⑦ LLM 叙事失败，降级 P0 模板: %s", exc)
    struct["report_mode"] = report_mode

    out_dir = Path(config.output.out_dir) / data.report_date
    out_dir.mkdir(parents=True, exist_ok=True)
    anomalies_path = out_dir / "anomalies.json"
    report_path = out_dir / "report.txt"
    html_path = out_dir / "report.html"
    anomalies_path.write_text(json.dumps(struct, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(text, encoding="utf-8")
    html_path.write_text(render_html(struct, narrative), encoding="utf-8")

    # 步骤⑧：OSS 上传（失败降级，SKILL 红线 7）+ 飞书短摘要（趋势焦点）
    oss_url: str | None = None
    if not dry_run and config.oss.enabled:
        try:
            oss_url = upload_report(config.oss, html_path, data.report_date)
        except OSSError as exc:
            logger.warning("完整报告上传 OSS 失败（消息将标注）: %s", exc)
    summary_text = render_summary(struct, oss_url)

    if dry_run:
        print(summary_text)
        print(
            f"\n[dry-run] 已生成 {anomalies_path} / {report_path} / {html_path}"
            f"（未上传 OSS/未推送飞书）"
        )
        return 0

    report_target = resolve_report_target(config)
    if report_target is None:
        print(f"[warn] 未配置环境变量 {config.feishu.chat_id_env}，报告仅落盘：{report_path}")
        return 0

    # 出站统一走平台 send_feishu_message（需 bingops-mcp 连接 + 平台开写开关）
    bingops_mcp = config.mcp_servers.get("bingops")
    if bingops_mcp is None:
        logger.error("mcp_servers.bingops 未配置（飞书出站通道必需）")
        return 1
    try:
        async with MCPServerPool(
            {"bingops": bingops_mcp},
            timeout_sec=config.query.per_app_timeout_sec,
        ) as pool:
            await send_text(pool, config, report_target[0], summary_text)
    except (FeishuOutboundError, httpx.HTTPError) as exc:
        logger.error("飞书推送失败: %s", exc)
        return 1
    suffix = "+OSS 链接" if oss_url else ""
    print(f"日报已推送飞书（短摘要{suffix}）；产物：{anomalies_path} / {report_path} / {html_path}")
    return 0
