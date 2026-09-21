"""P1 叙事层单测：payload 构造/裁剪、verify 三重校验、成稿与降级路径。"""

from __future__ import annotations

import pytest

from inspection_agent.config import LLMConfig
from inspection_agent.llm.client import LLMError
from inspection_agent.report.narrate import (
    NarrateVerifyError,
    build_payload,
    render_narrative,
    verify_narrative,
)
from inspection_agent.report.render import overview_line

URL = "https://prom.example.com/graph?g0.expr=disk"

STRUCT = {
    "report_date": "2026-09-02",
    "generated_at": "2026-09-03T10:00:00+08:00",
    "apps_inspected": 3,
    "apps_missing_data": ["故障应用"],
    "apps_summary": [
        {"app_name": "订单中心", "status": "异常", "anomalies": 1},
        {"app_name": "支付网关", "status": "健康", "anomalies": 0},
        {"app_name": "故障应用", "status": "数据缺失", "anomalies": 0},
    ],
    "anomalies": [
        {
            "rule": "disk_usage",
            "severity": "critical",
            "app_id": 17,
            "app_name": "订单中心",
            "resource_id": "res-1",
            "resource_name": "ecs-order-03",
            "metric": "disk",
            "observed": 96.4,
            "threshold": 95,
            "duration_minutes": None,
            "evidence_url": URL,
        }
    ],
    "risks": [],
    "changes": [
        {"app_name": "订单中心", "tag": "v1.2.3", "status": "failed", "ticket_no": "T-77"}
    ],
    "alerts": {
        "window_total": 1,
        "unrecovered_total": 0,
        "by_severity": {"2": 1},
        "ungrouped_window_total": 0,
        "ungrouped_active_total": 0,
    },
    "governance": {},
    "teams": ["t1"],
}

LLM_CFG = LLMConfig(enabled=True, base_url="http://127.0.0.1:9/v1", model="fake", token_budget=10**6)


def make_narrative(struct: dict, *, tamper: bool = False, drop: str | None = None) -> str:
    lines = [
        f"【全量 巡检日报 {struct['report_date']}】",
        overview_line(struct),
        "二、异常项：",
    ]
    for a in struct.get("anomalies") or []:
        threshold = a.get("threshold")
        thr = "" if threshold is None else f"（阈值 {threshold}）"
        lines.append(
            f"  [{a['severity']}] {a['app_name']} / {a['resource_name']}"
            f" {a['metric']} 观测 {a['observed']}{thr}"
        )
        if drop != "evidence":
            lines.append(f"  证据: {a['evidence_url']}")
    lines += [
        "三、风险提示：",
        "  无",
        "四、昨日变更回顾：",
        "  订单中心: tag v1.2.3 结果 failed（工单 T-77）",
        "五、待办建议：",
        "  - 数据缺失补采：故障应用",
    ]
    if tamper:
        lines.append("  磁盘观测 99.9，需关注")
    if drop != "section":
        pass
    else:
        lines = [line for line in lines if not line.startswith("五、待办建议")]
    return "\n".join(lines)


# ---------------------------------------------------------------- payload


def test_build_payload_shape():
    payload = build_payload(STRUCT, token_budget=10**6)
    assert payload["overview_line"] == overview_line(STRUCT)
    assert payload["apps_summary"][0]["status"] == "异常"
    assert payload["changes_summary"][0]["ticket_no"] == "T-77"
    assert payload["truncated"] is False


def test_build_payload_shrinks_under_budget_never_drops_anomalies():
    payload = build_payload(STRUCT, token_budget=50)
    assert payload["truncated"]
    assert payload["anomalies"] == STRUCT["anomalies"]
    assert "risks" not in payload and payload["risks_count"] == 0


# ---------------------------------------------------------------- verify


def test_verify_passes_on_contract_compliant_text():
    assert verify_narrative(make_narrative(STRUCT), STRUCT) == []


def test_verify_flags_tampered_number():
    violations = verify_narrative(make_narrative(STRUCT, tamper=True), STRUCT)
    assert any("不在契约内" in v for v in violations)


def test_verify_flags_missing_evidence():
    violations = verify_narrative(make_narrative(STRUCT, drop="evidence"), STRUCT)
    assert any("未原样出现" in v for v in violations)


def test_verify_flags_extra_url():
    text = make_narrative(STRUCT) + "\n  证据: https://evil.example.com/x"
    violations = verify_narrative(text, STRUCT)
    assert any("契约外 URL" in v for v in violations)


def test_verify_flags_missing_section():
    violations = verify_narrative(make_narrative(STRUCT, drop="section"), STRUCT)
    assert any("缺少段落标题" in v for v in violations)


# ---------------------------------------------------------------- render_narrative（降级路径在集成测试覆盖）


def _patch_llm(monkeypatch, response: str | None = None, error: Exception | None = None) -> None:
    """替换 narrate 命名空间中的 LLMClient（保留 from_config 调用路径）。"""

    class _FakeLLMClient:
        def __init__(self, config: LLMConfig) -> None:
            pass

        @classmethod
        def from_config(cls, config: LLMConfig) -> "_FakeLLMClient":
            return cls(config)

        async def chat(self, messages, tools=None):
            if error is not None:
                raise error
            return {"content": response}

    monkeypatch.setattr("inspection_agent.report.narrate.LLMClient", _FakeLLMClient)


async def test_render_narrative_success(monkeypatch):
    _patch_llm(monkeypatch, response=make_narrative(STRUCT))
    text = await render_narrative(STRUCT, LLM_CFG, window_hours=24)
    assert "二、异常项" in text and URL in text


async def test_render_narrative_verify_failure_raises(monkeypatch):
    _patch_llm(monkeypatch, response=make_narrative(STRUCT, tamper=True))
    with pytest.raises(NarrateVerifyError):
        await render_narrative(STRUCT, LLM_CFG)


async def test_render_narrative_llm_down_raises(monkeypatch):
    _patch_llm(monkeypatch, error=LLMError("down"))
    with pytest.raises(LLMError):
        await render_narrative(STRUCT, LLM_CFG)
