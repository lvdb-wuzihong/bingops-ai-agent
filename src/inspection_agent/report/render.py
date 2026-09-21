"""步骤⑦ P0 渲染：五段固定大纲，零 LLM。

数字与证据一律取自结构化 JSON（红线 3/5：渲染只做叙事与格式化，不改写数字/链接）。
大纲（设计文档 §5）：
  一、总览  二、异常项（critical → warning，附证据）  三、风险提示
  四、昨日变更回顾  五、待办建议
"""

from __future__ import annotations

from ..pipeline.rules import SEVERITY_ORDER


def overview_line(struct: dict) -> str:
    """总览行（单一来源：文本渲染、HTML 与 P1 叙事 payload 共用，保证数字一致）。"""
    return (
        f"一、总览：巡检应用 {struct.get('apps_inspected', 0)} 个"
        f" / 异常 {len(struct.get('anomalies') or [])} 项"
        f" / 昨日变更 {len(struct.get('changes') or [])} 次"
    )


def report_title(struct: dict) -> str:
    """报告标题（单一来源：文本渲染与飞书短摘要共用）。"""
    teams = [t for t in (struct.get("teams") or []) if t]
    if not teams:
        title = "全量"
    elif len(teams) == 1:
        title = teams[0]
    else:
        title = f"{teams[0]} 等 {len(teams)} 团队"
    return f"【{title} 巡检日报 {struct.get('report_date')}】"


def render_report(struct: dict) -> str:
    governance = struct.get("governance") or {}
    lines: list[str] = [report_title(struct), ""]
    lines.append(overview_line(struct))

    lines += ["", "二、异常项（critical → warning）："]
    anomalies = sorted(
        struct.get("anomalies") or [],
        key=lambda a: SEVERITY_ORDER.get(str(a.get("severity")), 9),
    )
    if not anomalies:
        lines.append("  无")
    for a in anomalies:
        line = (
            f"  [{a.get('severity')}] {a.get('app_name')} / {a.get('resource_name')}"
            f" {a.get('metric')} 观测 {fmt(a.get('observed'))}"
        )
        if a.get("threshold") is not None:
            line += f"（阈值 {fmt(a['threshold'])}）"
        if a.get("duration_minutes") is not None:
            line += f" 持续 {fmt(a['duration_minutes'])} 分钟"
        if a.get("summary"):
            line += f" — {a['summary']}"
        lines.append(line)
        lines.append(f"    证据: {a.get('evidence_url')}")

    lines += ["", "三、风险提示："]
    risks = struct.get("risks") or []
    if not risks:
        lines.append("  无")
    for r in risks:
        lines.append(
            f"  {r.get('app_name')} {r.get('metric')} 观测 {fmt(r.get('observed'))} — {r.get('trend')}"
        )
        lines.append(f"    证据: {r.get('evidence_url')}")

    lines += ["", "四、昨日变更回顾："]
    changes = struct.get("changes") or []
    if not changes:
        lines.append("  无")
    for c in changes:
        parts = [f"{c.get('app_name') or '未归集'}: tag {c.get('tag') or '-'}",
                 f"结果 {c.get('status') or '-'}"]
        if c.get("env"):
            parts.append(f"环境 {c['env']}")
        if c.get("triggered_by"):
            parts.append(f"执行人 {c['triggered_by']}")
        if c.get("ticket_no"):
            parts.append(f"（工单 {c['ticket_no']}）")
        lines.append("  " + " ".join(parts))

    lines += ["", "五、待办建议："]
    todos = governance_todos(struct, governance)
    if not todos:
        lines.append("  无")
    for todo in todos:
        lines.append(f"  - {todo}")
    return "\n".join(lines)


def governance_todos(struct: dict, governance: dict) -> list[str]:
    todos: list[str] = []
    for name in struct.get("apps_missing_data") or []:
        reason = (governance.get("missing_reasons") or {}).get(name)
        todos.append(f"数据缺失补采：{name}" + (f"（{reason}）" if reason else ""))
    for item in governance.get("unmapped_resources") or []:
        todos.append(f"资源类别未映射，需归集：{item.get('app_name')} / {item.get('resource')}")
    ungrouped_changes = [c for c in struct.get("changes") or [] if not c.get("app_name")]
    if ungrouped_changes:
        todos.append(f"存在 {len(ungrouped_changes)} 条未归集变更，请补 business_app 关联")
    for team in governance.get("truncated_teams") or []:
        todos.append(f"团队 {team} 应用数达到列表上限，结果可能截断，请拆分团队或收窄范围")
    if governance.get("changes_domain_error"):
        todos.append(f"变更数据域拉取失败，请检查平台 REST：{governance['changes_domain_error']}")
    return todos


def fmt(value) -> str:
    """数字展示格式化（仅格式化，不改值）；非数字原样输出。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:g}"
    return str(value)
