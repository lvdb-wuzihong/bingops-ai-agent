"""日报八步链路图（LangGraph StateGraph；节点=现有确定性函数薄包装，行为等价）。

八步链路（SKILL：触发 → ①对象清单 → ②检查项展开 → ③并行拉指标 → ⑤变更摘要
→ ⑥异常判定 → ⑦报告生成 → ⑧输出）：
  START → collect（①②③⑤取数 + ⑥规则 + P2 线索；sources/ REST 直连，双轨制禁 MCP）
        → structure（结构化契约 JSON）
        → narrate（llm.enabled；校验失败/断供图内整篇降级，红线 4）／ template
        → persist（落盘 anomalies.json / report.txt / report.html）
        → publish（OSS 上传失败降级标注，红线 7 + 飞书短摘要渲染）
        → notify（平台 send_feishu_message 出站；dry-run/无目标跳过）→ END

退出码语义在 runner.py 保持：0 成功；1 运行/推送失败；2 配置错误。
数字与证据契约（红线 3/5）：所有数字由规则引擎产出的 struct 携带，图节点零数字产出。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
from langgraph.graph import END, START, StateGraph

from ..config import AppConfig
from ..llm.client import LLMError
from ..mcpclient import MCPServerPool
from ..oss import OSSError, upload_report
from ..pipeline.clues import attach_clues
from ..pipeline.collect import collect
from ..pipeline.rules import evaluate_all
from ..report.feishu import FeishuOutboundError, send_text
from ..report.html import render_html
from ..report.narrate import NarrateError, render_narrative
from ..report.render import render_report
from ..report.structured import build_structured
from ..report.summary import render_summary
from ..sources import build_sources
from .state import ReportState

logger = logging.getLogger(__name__)


def build_report_graph(
    config: AppConfig,
    *,
    now: datetime,
    report_date: date,
    dry_run: bool,
    report_target: tuple[str, str] | None,
) -> Any:
    """组装日报图；config/now/dry_run/report_target 为运行输入（闭包注入，不进 State）。"""

    async def collect_node(state: ReportState) -> dict[str, Any]:
        sources = build_sources(config)  # 双轨制：pipeline 取数走 REST（凭据缺失在此报错）
        data = await collect(config, sources, report_date, now=now)
        anomalies, risks = evaluate_all(data, config, sources)
        # P2 根因线索（只读 REST 查询，dry-run 同样执行；仅对命中异常，per-anomaly 超时保护）
        await attach_clues(anomalies, data, sources, config)
        return {"sources": sources, "data": data, "anomalies": anomalies, "risks": risks}

    async def structure_node(state: ReportState) -> dict[str, Any]:
        struct = build_structured(
            state["data"], config, state["anomalies"], state["risks"], generated_at=now
        )
        return {"struct": struct}

    async def narrate_node(state: ReportState) -> dict[str, Any]:
        """步骤⑦ P1 叙事；校验失败/LLM 断供 → 整篇降级 P0 模板（红线 4，不裁句修补）。"""
        struct = state["struct"]
        try:
            narrative = await render_narrative(
                struct, config.llm, window_hours=config.window_hours
            )
            return {
                "text": narrative,
                "narrative": narrative,
                "struct": {**struct, "report_mode": "llm"},
            }
        except (LLMError, NarrateError) as exc:
            logger.warning("步骤⑦ LLM 叙事失败，降级 P0 模板: %s", exc)
            return {
                "text": render_report(struct),
                "narrative": None,
                "struct": {**struct, "report_mode": "template"},
            }

    async def template_node(state: ReportState) -> dict[str, Any]:
        """步骤⑦ P0 模板拼装（llm.enabled=false 直达）。"""
        struct = state["struct"]
        return {
            "text": render_report(struct),
            "narrative": None,
            "struct": {**struct, "report_mode": "template"},
        }

    async def persist_node(state: ReportState) -> dict[str, Any]:
        out_dir = Path(config.output.out_dir) / state["data"].report_date
        out_dir.mkdir(parents=True, exist_ok=True)
        artifacts = {
            "anomalies": out_dir / "anomalies.json",
            "report": out_dir / "report.txt",
            "html": out_dir / "report.html",
        }
        artifacts["anomalies"].write_text(
            json.dumps(state["struct"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        artifacts["report"].write_text(state["text"], encoding="utf-8")
        artifacts["html"].write_text(
            render_html(state["struct"], state["narrative"]), encoding="utf-8"
        )
        return {"artifacts": artifacts}

    async def publish_node(state: ReportState) -> dict[str, Any]:
        # 步骤⑧：OSS 上传（失败降级，SKILL 红线 7）+ 飞书短摘要（趋势焦点）
        oss_url: str | None = None
        if not dry_run and config.oss.enabled:
            try:
                oss_url = upload_report(
                    config.oss, state["artifacts"]["html"], state["data"].report_date
                )
            except OSSError as exc:
                logger.warning("完整报告上传 OSS 失败（消息将标注）: %s", exc)
        return {"oss_url": oss_url, "summary_text": render_summary(state["struct"], oss_url)}

    async def notify_node(state: ReportState) -> dict[str, Any]:
        if dry_run or report_target is None:
            return {}
        # 出站统一走平台 send_feishu_message（需 bingops-mcp 连接 + 平台开写开关）
        bingops_mcp = config.mcp_servers.get("bingops")
        if bingops_mcp is None:
            logger.error("mcp_servers.bingops 未配置（飞书出站通道必需）")
            return {"notify_error": "mcp_servers.bingops 未配置"}
        try:
            async with MCPServerPool(
                {"bingops": bingops_mcp},
                timeout_sec=config.query.per_app_timeout_sec,
            ) as pool:
                await send_text(pool, config, report_target[0], state["summary_text"])
        except (FeishuOutboundError, httpx.HTTPError) as exc:
            logger.error("飞书推送失败: %s", exc)
            return {"notify_error": str(exc)}
        return {}

    def route_compose(state: ReportState) -> str:
        return "narrate" if config.llm.enabled else "template"

    graph = StateGraph(ReportState)
    graph.add_node("collect", collect_node)
    graph.add_node("structure", structure_node)
    graph.add_node("narrate", narrate_node)
    graph.add_node("template", template_node)
    graph.add_node("persist", persist_node)
    graph.add_node("publish", publish_node)
    graph.add_node("notify", notify_node)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "structure")
    graph.add_conditional_edges(
        "structure", route_compose, {"narrate": "narrate", "template": "template"}
    )
    graph.add_edge("narrate", "persist")
    graph.add_edge("template", "persist")
    graph.add_edge("persist", "publish")
    graph.add_edge("publish", "notify")
    graph.add_edge("notify", END)
    return graph.compile()
