"""契约 JSON 组装（结构 = .qoder/skills/inspection-agent-dev/reference.md §1）。

P1 起，本 JSON 是 LLM 成稿的唯一输入：健康应用只送一行摘要，仅异常应用送明细；
LLM 不得产出/修改其中任何数字与证据链接。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..config import AppConfig
from ..pipeline.collect import ReportData


def build_structured(
    data: ReportData,
    config: AppConfig,
    anomalies: list[dict],
    risks: list[dict],
    generated_at: datetime,
) -> dict[str, Any]:
    changes = [dict(c) for app in data.apps for c in app.changes]
    changes.extend(dict(c) for c in data.ungrouped_changes)

    # 应用一行摘要（P1 叙事 payload 来源：健康应用只传一行，SKILL 红线 6）
    anomaly_count: dict[str, int] = {}
    for a in anomalies:
        name = str(a.get("app_name"))
        anomaly_count[name] = anomaly_count.get(name, 0) + 1
    apps_summary = [
        {
            "app_name": a.app_name,
            "status": (
                "数据缺失" if a.missing_reason
                else ("异常" if a.app_name in anomaly_count else "健康")
            ),
            "anomalies": anomaly_count.get(a.app_name, 0) if not a.missing_reason else 0,
        }
        for a in data.apps
    ]

    return {
        "report_date": data.report_date,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "apps_inspected": len(data.apps),
        "apps_missing_data": [a.app_name for a in data.apps if a.missing_reason],
        "apps_summary": apps_summary,
        "anomalies": anomalies,
        "risks": risks,
        "changes": changes,
        "governance": {
            "missing_reasons": {
                a.app_name: a.missing_reason for a in data.apps if a.missing_reason
            },
            "unmapped_resources": [
                {"app_name": a.app_name, "resource": name}
                for a in data.apps
                for name in a.unmapped_resources
            ],
            "changes_domain_error": data.changes_domain_error,
            "truncated_teams": list(data.truncated_teams),
        },
        "teams": list(data.teams),
    }
