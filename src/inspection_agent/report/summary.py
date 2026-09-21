"""飞书短摘要（趋势焦点）：数字全部来自结构化 JSON（SKILL 红线 3/7）。

结构：标题 + 总览行 + 趋势关注 Top5（按 |change_pct| 降序，▲/▼ 标注）+ 完整报告链接。
OSS 失败/未配置时消息照发并标注（runner 传 oss_url=None）。
"""

from __future__ import annotations

from .render import fmt, overview_line, report_title

_TREND_TOP_N = 5
_ARROW = {"up": "▲", "down": "▼"}
_NO_TREND_TEXT = "无显著趋势变动"
_OSS_FAILED_TEXT = "生成失败，请检查 OSS 配置"


def render_summary(struct: dict, oss_url: str | None) -> str:
    lines: list[str] = [report_title(struct), overview_line(struct)]
    trends = sorted(
        (r for r in struct.get("risks") or [] if r.get("type") == "trend"),
        key=lambda r: abs(float(r.get("change_pct") or 0)),
        reverse=True,
    )
    lines.append("趋势关注（昨日 vs 近 7 日均值）：")
    if not trends:
        lines.append(f"  {_NO_TREND_TEXT}")
    for risk in trends[:_TREND_TOP_N]:
        direction = str(risk.get("direction") or "up")
        arrow = _ARROW.get(direction, "•")
        change_pct = float(risk.get("change_pct") or 0)
        lines.append(
            f"  {arrow} {risk.get('app_name')}/{risk.get('resource_name')}"
            f" {risk.get('metric')} {fmt(risk.get('observed'))}"
            f"（近 7 日均值 {fmt(risk.get('base_value'))}，{change_pct:+.1f}%）"
        )
    lines.append("完整报告：" + (oss_url if oss_url else _OSS_FAILED_TEXT))
    clue_count = sum(len(a.get("clues") or []) for a in struct.get("anomalies") or [])
    if clue_count:
        lines.append(f"含 {clue_count} 条根因线索，详见完整报告")
    return "\n".join(lines)
