"""完整 HTML 报告渲染（内联 CSS 单文件，上传 OSS 供运维查看）。

数字/证据一律取自结构化 JSON（SKILL 红线 3/5）；LLM 点评（narrative）已经过
P1 verify（数字/证据一致性），嵌入前做 html.escape，叙事文本不被 HTML 解析干扰。
段落：标题/总览卡/LLM 点评（可选）/异常表/趋势表/变更表/治理待办。
行内容先转义为局部变量再拼接，f-string 表达式只含简单变量名（避免嵌套引号）。
"""

from __future__ import annotations

import html as _html
from typing import Any

from .render import fmt, governance_todos, overview_line, report_title

_CSS = (
    "body{font-family:'Microsoft YaHei',system-ui,sans-serif;margin:24px;color:#222;"
    "max-width:1100px}"
    "h1{font-size:20px;border-bottom:2px solid #2c5aa0;padding-bottom:8px}"
    "h2{font-size:16px;margin:24px 0 8px;color:#2c5aa0}"
    ".card{background:#f0f5ff;border:1px solid #c5d9f1;border-radius:6px;padding:10px 14px;"
    "margin:12px 0}"
    ".narrative{white-space:pre-wrap;background:#fafafa;border:1px solid #eee;border-radius:6px;"
    "padding:12px;font-size:13px}"
    "table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}"
    "th,td{border:1px solid #ddd;padding:6px 10px;text-align:left;vertical-align:top}"
    "th{background:#f5f6f7}"
    ".critical{color:#c0392b;font-weight:600}"
    ".warning{color:#e67e22;font-weight:600}"
    ".info{color:#2471a3}"
    ".up{color:#c0392b;font-weight:600}.down{color:#27ae60;font-weight:600}"
    "a{color:#2c5aa0;word-break:break-all}"
    ".muted{color:#888;font-size:11px}"
    ".clue{margin:2px 0;font-size:12px}"
    ".clue-type{display:inline-block;background:#eef3fb;border:1px solid #c5d9f1;"
    "border-radius:3px;padding:0 6px;margin-right:4px;color:#2c5aa0}"
)

_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _esc(value: Any) -> str:
    return _html.escape(str(value))


def _clues_block(anomaly: dict) -> str:
    """异常行线索块（P2）：每条线索一行（类型标签 + 事实文本 + 证据链接）。"""
    clues = anomaly.get("clues") or []
    if not clues:
        return "<div class='muted'>无线索</div>"
    parts = []
    for clue in clues:
        ctype = _esc(clue.get("type"))
        text = _esc(clue.get("text"))
        evidence = _esc(clue.get("evidence_url"))
        parts.append(
            f"<div class='clue'><span class='clue-type'>{ctype}</span> {text} "
            f"<a href='{evidence}'>证据</a></div>"
        )
    return "".join(parts)


def _anomaly_row(anomaly: dict) -> str:
    severity = _esc(anomaly.get("severity"))
    app_name = _esc(anomaly.get("app_name"))
    resource = _esc(anomaly.get("resource_name"))
    metric = _esc(anomaly.get("metric"))
    observed = _esc(fmt(anomaly.get("observed")))
    threshold = _esc(fmt(anomaly.get("threshold"))) if anomaly.get("threshold") is not None else "-"
    if anomaly.get("duration_minutes") is not None:
        duration = _esc(fmt(anomaly.get("duration_minutes"))) + " 分钟"
    else:
        duration = "-"
    evidence = _esc(anomaly.get("evidence_url"))
    summary = _esc(anomaly.get("summary"))
    extra = f"<div>{summary}</div>" if summary else ""
    clues = _clues_block(anomaly)
    return (
        f"<tr><td class='{severity}'>{severity}</td>"
        f"<td>{app_name}</td><td>{resource}</td><td>{metric}</td>"
        f"<td>{observed}</td><td>{threshold}</td><td>{duration}</td>"
        f"<td><a href='{evidence}'>证据</a><div class='muted'>{evidence}</div>{extra}</td>"
        f"<td>{clues}</td></tr>"
    )


def _trend_row(risk: dict) -> str:
    app_name = _esc(risk.get("app_name"))
    resource = _esc(risk.get("resource_name"))
    metric = _esc(risk.get("metric"))
    observed = _esc(fmt(risk.get("observed")))
    base = _esc(fmt(risk.get("base_value")))
    direction = str(risk.get("direction") or "up")
    arrow = "&#9650;" if direction == "up" else "&#9660;"
    change = _esc(fmt(risk.get("change_pct")))
    text = _esc(risk.get("trend"))
    evidence = _esc(risk.get("evidence_url"))
    return (
        f"<tr><td>{app_name}</td><td>{resource}</td><td>{metric}</td>"
        f"<td>{observed}</td><td>{base}</td>"
        f"<td class='{direction}'>{arrow} {change}%</td>"
        f"<td>{text}</td><td><a href='{evidence}'>证据</a></td></tr>"
    )


def _change_row(change: dict) -> str:
    app_name = _esc(change.get("app_name") or "未归集")
    tag = _esc(change.get("tag") or "-")
    env = _esc(change.get("env") or "-")
    status = _esc(change.get("status") or "-")
    triggered_by = _esc(change.get("triggered_by") or "-")
    ticket_no = _esc(change.get("ticket_no") or "-")
    return (
        f"<tr><td>{app_name}</td><td>{tag}</td><td>{env}</td>"
        f"<td>{status}</td><td>{triggered_by}</td><td>{ticket_no}</td></tr>"
    )


def render_html(struct: dict, narrative: str | None = None) -> str:
    parts: list[str] = [
        "<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>",
        f"<title>{_esc(struct.get('report_date'))} 巡检日报</title>",
        f"<style>{_CSS}</style></head><body>",
        f"<h1>{_esc(report_title(struct))}</h1>",
        f"<div class='card'>{_esc(overview_line(struct))}</div>",
    ]
    if narrative and narrative.strip():
        parts.append("<h2>点评</h2>")
        parts.append(f"<div class='narrative'>{_esc(narrative.strip())}</div>")

    anomalies = sorted(
        struct.get("anomalies") or [],
        key=lambda a: _SEVERITY_ORDER.get(str(a.get("severity")), 9),
    )
    parts.append("<h2>异常项</h2>")
    if anomalies:
        rows = "".join(_anomaly_row(a) for a in anomalies)
        parts.append(
            "<table><tr><th>级别</th><th>应用</th><th>资源</th><th>指标</th>"
            "<th>观测</th><th>阈值</th><th>持续</th><th>证据</th><th>线索</th></tr>"
            + rows
            + "</table>"
        )
    else:
        parts.append("<p>无</p>")

    trends = [r for r in struct.get("risks") or [] if r.get("type") == "trend"]
    parts.append("<h2>趋势关注（昨日 vs 近 7 日均值）</h2>")
    if trends:
        rows = "".join(_trend_row(r) for r in trends)
        parts.append(
            "<table><tr><th>应用</th><th>资源</th><th>指标</th><th>昨日</th>"
            "<th>近 7 日均值</th><th>变化</th><th>说明</th><th>证据</th></tr>"
            + rows
            + "</table>"
        )
    else:
        parts.append("<p>无显著趋势变动</p>")

    changes = struct.get("changes") or []
    parts.append("<h2>昨日变更回顾</h2>")
    if changes:
        rows = "".join(_change_row(c) for c in changes)
        parts.append(
            "<table><tr><th>应用</th><th>tag</th><th>环境</th><th>结果</th>"
            "<th>执行人</th><th>工单</th></tr>"
            + rows
            + "</table>"
        )
    else:
        parts.append("<p>无</p>")

    parts.append("<h2>待办建议</h2>")
    todos = governance_todos(struct, struct.get("governance") or {})
    if todos:
        parts.append("<ul>" + "".join(f"<li>{_esc(todo)}</li>" for todo in todos) + "</ul>")
    else:
        parts.append("<p>无</p>")

    generated = _esc(struct.get("generated_at"))
    parts.append(f"<p class='muted'>生成时间：{generated}</p></body></html>")
    return "".join(parts)
