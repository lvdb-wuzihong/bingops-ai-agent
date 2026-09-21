"""步骤⑦ P1 叙事层：结构化 JSON → LLM 成稿 → 一致性校验 → 失败降级 P0。

红线（SKILL 3/4/5/6，契约见 .qoder/skills/inspection-agent-dev/reference.md §7）：
- LLM 只写叙事，数字/证据必须逐字来自结构化 JSON；
- verify 失败整篇降级（不裁句修补）；
- token 控制：健康应用一行、仅异常应用送明细、超预算逐步收缩；
- LLM API 异常/超时由 runner 捕获后降级 P0 模板版。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..config import LLMConfig
from ..llm.client import LLMClient
from .render import overview_line

logger = logging.getLogger(__name__)

_SECTIONS = ("一、总览", "二、异常项", "三、风险提示", "四、昨日变更回顾", "五、待办建议")
_URL_RE = re.compile(r"https?://\S+")
_DATETIME_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?)?")
# 含数字的标识符（tag 版本号/工单号/资源名等：v1.2.3、T-77、ecs-order-03），剥离后不做数字校验
_IDENTIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9._\-]*\d[A-Za-z0-9._\-]*")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_URL_TRAILING_PUNCT = "。，）；）、】》\"'》>,，"
_CHANGES_HARD_CAP = 20
_COMMON_NUMBERS = {float(n) for n in range(10)} | {100.0}

SYSTEM_PROMPT = (
    "你是运维巡检日报撰写员。根据用户提供的结构化 JSON 撰写巡检日报，硬性规则：\n"
    "1. 输出必须且只能包含五段，标题逐字使用：一、总览：/ 二、异常项：/ 三、风险提示：/ "
    "四、昨日变更回顾：/ 五、待办建议：\n"
    "2. \"一、总览\"一行必须原样引用 JSON 的 overview_line 字段，一字不改；\n"
    "3. 所有数字（观测值、阈值、时长、计数）必须逐字来自 JSON，"
    "禁止计算、改写、新增、省略任何数字；\n"
    "4. 每条异常必须原样保留其 evidence_url（在该异常描述后单独一行，格式：证据: <url>），"
    "不得新增任何 URL；\n"
    "5. 数据缺失/无数据的应用写\"无数据\"，禁止编造；\n"
    "6. 点评可引用 anomalies 中的 clues 线索事实（变更/告警/拓扑/git），\n"
    "   但线索内容必须逐字引用，不得推算新的事实或数字；\n"
    "7. 简洁中文，面向运维值班阅读。"
)


class NarrateError(RuntimeError):
    """叙事成稿失败（空内容等）。"""


class NarrateVerifyError(NarrateError):
    """叙事一致性校验失败（数字/证据/大纲违规）。"""


async def render_narrative(
    struct: dict[str, Any], llm: LLMConfig, window_hours: int | None = None
) -> str:
    """成稿并校验；任何失败以 NarrateError/LLMError 抛出，由 runner 降级 P0 模板。"""
    client = LLMClient.from_config(llm)
    payload = build_payload(struct, llm.token_budget, window_hours)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    message = await client.chat(messages)
    text = str(message.get("content") or "").strip()
    if not text:
        raise NarrateError("LLM 返回空内容")
    violations = verify_narrative(text, struct, window_hours)
    if violations:
        detail = "; ".join(violations[:5])
        raise NarrateVerifyError(
            f"叙事一致性校验失败（共 {len(violations)} 项）：{detail}"
        )
    return text


# ---------------------------------------------------------------- payload 构造（token 控制）


def build_payload(
    struct: dict[str, Any], token_budget: int, window_hours: int | None = None
) -> dict[str, Any]:
    """构造 LLM 输入：健康应用一行、异常应用明细（SKILL 红线 6），超预算逐步收缩。"""
    payload: dict[str, Any] = {
        "report_date": struct.get("report_date"),
        "overview_line": overview_line(struct),
        "window_hours": window_hours,
        "apps_summary": struct.get("apps_summary") or [],
        "anomalies": struct.get("anomalies") or [],
        "risks": struct.get("risks") or [],
        "changes_summary": [
            {
                "app_name": c.get("app_name") or "未归集",
                "tag": c.get("tag") or "",
                "status": c.get("status") or "",
                "ticket_no": c.get("ticket_no") or "",
            }
            for c in (struct.get("changes") or [])[:_CHANGES_HARD_CAP]
        ],
        "governance": struct.get("governance") or {},
    }
    return _shrink(payload, token_budget)


def _shrink(payload: dict[str, Any], budget_chars: int) -> dict[str, Any]:
    """超出预算逐步收缩（changes 明细 → risks → apps_summary），anomalies 永不裁剪。"""

    def length() -> int:
        return len(json.dumps(payload, ensure_ascii=False))

    payload["truncated"] = False
    if length() <= budget_chars:
        return payload
    payload["changes_summary"] = payload["changes_summary"][:10]
    payload["truncated"] = "changes 已截断"
    if length() <= budget_chars:
        return payload
    payload["risks_count"] = len(payload.pop("risks"))
    payload["truncated"] = "risks+changes 已截断"
    if length() <= budget_chars:
        return payload
    payload["apps_summary"] = payload["apps_summary"][:50]
    payload["truncated"] = "深度截断（仅保留异常明细）"
    return payload


# ---------------------------------------------------------------- 一致性校验


def verify_narrative(
    text: str, struct: dict[str, Any], window_hours: int | None = None
) -> list[str]:
    """数字/证据/大纲三重校验（reference.md §7）；返回违规列表，空列表 = 通过。"""
    violations: list[str] = []

    for section in _SECTIONS:
        if section not in text:
            violations.append(f"缺少段落标题：{section}")

    contract_urls = _collect_urls(struct)
    for anomaly in struct.get("anomalies") or []:
        url = str(anomaly.get("evidence_url") or "")
        if url and url not in text:
            violations.append(f"异常证据链接未原样出现在正文：{url}")
    for url in _URL_RE.findall(text):
        if url.rstrip(_URL_TRAILING_PUNCT) not in contract_urls:
            violations.append(f"正文出现契约外 URL：{url[:80]}")

    allowed = _allowed_numbers(struct, window_hours)
    cleaned = _DATETIME_RE.sub(" ", _URL_RE.sub(" ", text))
    cleaned = _IDENTIFIER_RE.sub(" ", cleaned)
    for token in _NUMBER_RE.findall(cleaned):
        if float(token) not in allowed:
            violations.append(f"正文数字不在契约内：{token}")
    return violations


def _collect_urls(struct: dict[str, Any]) -> set[str]:
    """收集契约内证据链接（evidence_url 字段，含 Prom 图 URL / 工单号）。"""
    urls: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "evidence_url" and isinstance(value, str) and value.strip():
                    urls.add(value.strip())
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(struct)
    return urls


def _allowed_numbers(struct: dict[str, Any], window_hours: int | None) -> set[float]:
    """数字白名单：结构化 JSON 数值字段 ∪ 字符串字段中的数字 ∪ {0-9, 100} ∪ 窗口小时数。

    字符串字段（如 governance.missing_reasons 的容错声明含错误码 HTTP 404）同样允许
    LLM 逐字引用——payload 内文本与正文校验必须对称，否则契约自相矛盾；
    引用时同样先剥 URL/日期/标识符（与正文校验同一套规则）。
    """
    allowed = set(_COMMON_NUMBERS)

    def walk(node: Any) -> None:
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            allowed.add(float(node))
        elif isinstance(node, str):
            text = _DATETIME_RE.sub(" ", _URL_RE.sub(" ", node))
            text = _IDENTIFIER_RE.sub(" ", text)
            for token in _NUMBER_RE.findall(text):
                allowed.add(float(token))
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(struct)
    if window_hours is not None:
        allowed.add(float(window_hours))
    return allowed
