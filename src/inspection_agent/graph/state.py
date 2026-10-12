"""LangGraph 图状态定义（TypedDict）。

- BotState：bot ReAct 对话循环（graph/bot_graph.py）。messages 保持 OpenAI dict 形态
  （LLMClient 契约、测试 FakeLLM mock 点），reducer 为自定义追加合并——不用 langgraph
  的 add_messages（它会把 dict 隐式转成 BaseMessage，破坏 dict 契约）；
- ReportState：日报八步链路（graph/report_graph.py）。各键 LastValue 整体替换，
  节点返回部分更新。

State 只做节点间数据载体；数字与证据契约（SKILL 红线 3/5）由 pipeline/report 模块保证。
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict


def append_messages(
    left: list[dict[str, Any]] | None, right: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """消息追加合并（LangGraph 自定义 reducer）：节点返回增量，既有历史不丢。"""
    return [*(left or []), *(right or [])]


class BotState(TypedDict):
    messages: Annotated[list[dict[str, Any]], append_messages]
    tool_rounds: int   # 已执行的工具轮次（达上限路由 finalize）
    answer: str        # 最终回复文本（agent 无工具调用轮或 finalize 写入）


class ReportState(TypedDict, total=False):
    sources: Any            # sources.Sources（REST 装配产物）
    data: Any               # pipeline.collect 产物（含 report_date）
    anomalies: list         # 规则引擎结构化异常（输出契约，SKILL 红线 3）
    risks: list             # 风险项（含趋势类）
    struct: dict            # 结构化报告 JSON（含 report_mode）
    text: str               # 报告正文（叙事或模板）
    narrative: str | None   # LLM 叙事全文（模板模式为 None）
    artifacts: dict         # 落盘产物路径 {anomalies, report, html}
    oss_url: str | None     # OSS 签名 URL（未上传为 None）
    summary_text: str       # 飞书短摘要
    notify_error: str | None  # 飞书出站失败标记（runner 据此返回退出码 1）
