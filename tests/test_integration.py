"""集成测试（P2-charter 双轨制）：平台/外部 REST 假服务 + bot HTTP 端点 全链路验证。

覆盖：
- 巡检日报全链路（REST 取数 → 规则 → 线索 → HTML → OSS → 摘要，dry-run 落盘）；
- 单应用失败隔离；趋势命中；LLM 叙事三态（成稿/篡改降级/断供降级）；
- OSS 成功/失败；bot 接收平台转发事件（POST /feishu/events）→ agent → 出站。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
import uvicorn
import yaml
from mcp.server.fastmcp import FastMCP

from inspection_agent.bot.server import create_app
from inspection_agent.runner import run_report

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOM_APP_ID = 999
TS_IN_WINDOW = int(datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc).timestamp())
ISO_IN_WINDOW = "2026-09-02T08:00:00+00:00"
ISO_EARLIER_IN_WINDOW = "2026-09-02T06:00:00+00:00"

APPS = {
    1: {"id": 1, "name": "订单中心", "app_code": "order", "team": "t1", "owner": "alice",
        "department": "", "repo_url": "https://git.example.com/group/order.git"},
    2: {"id": 2, "name": "支付网关", "app_code": "pay", "team": "t1", "owner": "bob",
        "department": "", "repo_url": ""},
    BOOM_APP_ID: {"id": BOOM_APP_ID, "name": "故障应用", "app_code": "boom", "team": "t1",
                  "owner": "carol", "department": "", "repo_url": ""},
}

RESOURCES = {
    1: [
        {"id": 101, "name": "ecs-order-03", "model_code": "ecs", "fields": {"ip": "10.0.0.3"}},
        {"id": 102, "name": "pod-order-api-0", "model_code": "pod", "fields": {}},
    ],
    2: [{"id": 103, "name": "ecs-pay-01", "model_code": "ecs", "fields": {"ip": "10.0.0.4"}}],
}

JOBS = [
    {"id": 11, "runbook_id": 1, "runbook_version": "v1", "code_ref": "v1.2.3",
     "status": "failed", "ticket_id": 77, "triggered_by": 3, "target_resources": [101],
     "started_at": ISO_IN_WINDOW, "finished_at": ISO_IN_WINDOW, "created_at": ISO_IN_WINDOW},
    {"id": 10, "runbook_id": 1, "runbook_version": "v1", "code_ref": "v1.2.2",
     "status": "success", "ticket_id": 77, "triggered_by": 3, "target_resources": [101],
     "started_at": ISO_EARLIER_IN_WINDOW, "finished_at": ISO_EARLIER_IN_WINDOW,
     "created_at": ISO_EARLIER_IN_WINDOW},
    {"id": 12, "runbook_id": 2, "runbook_version": "v1", "code_ref": "v2.0.0",
     "status": "success", "ticket_id": 78, "triggered_by": 3, "target_resources": [],
     "started_at": ISO_IN_WINDOW, "finished_at": ISO_IN_WINDOW, "created_at": ISO_IN_WINDOW},
]

TICKETS = [
    {"id": 77, "ticket_no": "T-77", "title": "订单中心发布", "status": "done",
     "ticket_type": "change", "priority": "P2", "risk_level": "low",
     "approval_status": "approved", "creator_id": 1, "assignee_id": 2,
     "catalog_item_id": None, "group_id": 1, "business_app_id": 1,
     "target_resource_ids": [101], "code_ref": None, "runbook_id": 1,
     "created_at": ISO_IN_WINDOW, "updated_at": ISO_IN_WINDOW, "resolved_at": None},
    {"id": 78, "ticket_no": "T-78", "title": "支付网关发布", "status": "done",
     "ticket_type": "change", "priority": "P2", "risk_level": "low",
     "approval_status": "approved", "creator_id": 1, "assignee_id": 2,
     "catalog_item_id": None, "group_id": 1, "business_app_id": 2,
     "target_resource_ids": [], "code_ref": None, "runbook_id": 2,
     "created_at": ISO_IN_WINDOW, "updated_at": ISO_IN_WINDOW, "resolved_at": None},
]

TOPOLOGY = {
    "nodes": [{"id": 201, "name": "mysql-01"}, {"id": 202, "name": "redis-02"}],
    "edges": [{"source": 201, "target": 101}, {"source": 202, "target": 101}],
}


class MockASGIServer:
    """在临时端口上运行裸 ASGI 应用。"""

    def __init__(self, app) -> None:
        self._app = app
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    async def start(self, suffix: str = "") -> str:
        config = uvicorn.Config(self._app, host="127.0.0.1", port=0, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        for _ in range(200):
            if self._server.started and self._server.servers:
                port = self._server.servers[0].sockets[0].getsockname()[1]
                return f"http://127.0.0.1:{port}{suffix}"
            await asyncio.sleep(0.05)
        raise RuntimeError("mock ASGI server 启动超时")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)


def _respond(send, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode()

    async def send_all() -> None:
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

    return send_all()


def _lifespan_app(handler) -> Any:
    """裸 ASGI 骨架：处理 lifespan + 读 body + 调用 http handler(scope, path, params, body, send)。"""

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        params = dict(
            parse_qsl(scope.get("query_string", b"").decode(), keep_blank_values=True)
        )
        await handler(scope, path=scope["path"], params=params, body=body, send=send)

    return app


# ---------------------------------------------------------------- 平台/外部 REST 假服务


def _ok(data: Any) -> dict:
    return {"code": 0, "message": "success", "data": data, "request_id": ""}


def _respond(send, status: int, payload: dict):
    """返回单参 async 发送函数（调用方 await send()）。"""
    body = json.dumps(payload).encode()

    async def _send() -> None:
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

    return _send


def build_platform_rest_app() -> Any:
    """平台 REST 假服务（统一响应体 {code, message, data}；端点已按平台源码核对）。"""

    async def handler(scope, path, params, body, send):
        if path == "/api/v1/auth/login":
            send = _respond(send, 200, _ok({"access_token": "fake-jwt", "refresh_token": "r"}))
        elif path == "/api/v1/cmdb/apps":
            send = _respond(send, 200, _ok({"items": list(APPS.values()), "total": len(APPS)}))
        elif path == f"/api/v1/cmdb/apps/{BOOM_APP_ID}":
            send = _respond(send, 500, {"code": 500, "message": "boom（模拟平台内部错误）"})
        elif path.startswith("/api/v1/cmdb/apps/") and path.endswith("/resources"):
            app_id = int(path.split("/")[5])
            send = _respond(send, 200, _ok({"items": RESOURCES.get(app_id, [])}))
        elif path.startswith("/api/v1/cmdb/apps/"):
            app_id = int(path.rsplit("/", 1)[1])
            send = _respond(send, 200, _ok({**APPS[app_id], "pipelines": []}))
        elif path == "/api/v1/jobs/executions":
            send = _respond(send, 200, _ok({"items": JOBS, "total": len(JOBS)}))
        elif path == "/api/v1/tickets":
            send = _respond(send, 200, _ok({"items": TICKETS, "total": len(TICKETS)}))
        elif path.endswith("/topology"):
            send = _respond(send, 200, _ok(TOPOLOGY))
        else:
            send = _respond(send, 404, {"code": 404, "message": "not found"})
        await send()

    return _lifespan_app(handler)


def build_prometheus_rest_app(disk_mode: str = "linear") -> Any:
    """Prometheus /api/v1/query_range 假服务。

    disk_mode="linear"：磁盘线性 60→96（24h critical + 8 天趋势命中，默认）；
    disk_mode="flat88"：磁盘恒 88（warning；多实例定向用例中区分第二实例的指纹值）。
    """

    async def handler(scope, path, params, body, send):
        start_dt = datetime.fromisoformat(params["start"])
        end_dt = datetime.fromisoformat(params["end"])
        step_seconds = int(params.get("step", "300s").rstrip("s") or 300)
        count = max(1, int((end_dt - start_dt).total_seconds() // step_seconds))
        query = params["query"]
        if "10.0.0.4" in query:
            values = [30.0] * count
        elif "node_cpu_seconds_total" in query:
            values = [85.0] * count
        elif "node_filesystem_avail_bytes" in query:
            if disk_mode == "flat88":
                values = [88.0] * count
            else:
                values = [60.0 + 36.0 * i / max(1, count - 1) for i in range(count)]
        elif "node_memory_MemAvailable" in query:
            values = [50.0] * count
        elif "kube_pod_container_status_restarts_total" in query:
            values = [min(4.0, i / 72.0) for i in range(count)]
        else:
            values = [10.0] * count
        base_ts = start_dt.timestamp()
        points = [[base_ts + i * step_seconds, str(v)] for i, v in enumerate(values)]
        payload = {"status": "success",
                   "data": {"resultType": "matrix",
                            "result": [{"metric": {"instance": "x"}, "values": points}]}}
        await _respond(send, 200, payload)()

    return _lifespan_app(handler)


def build_gitlab_rest_app() -> Any:
    """GitLab compare 假服务。"""

    async def handler(scope, path, params, body, send):
        payload = {"diffs": [{"old_path": f"f{i}.py"} for i in range(12)],
                   "commits": [{"title": "fix: order bug"}, {"title": "feat: payment retry"}]}
        await _respond(send, 200, payload)()

    return _lifespan_app(handler)


# ---------------------------------------------------------------- bot 侧 MCP mock（LLM 工具 + 出站）


def build_bingops_mcp_mock(sent: list) -> FastMCP:
    """bot 用 bingops-mcp mock：只读工具子集 + send_feishu_message（出站记录到 sent）。"""
    mcp = FastMCP("bingops-mcp-mock", stateless_http=True)

    @mcp.tool()
    async def list_business_apps(team=None, owner=None, keyword=None, limit=None) -> dict:
        items = [a for a in APPS.values() if team is None or a["team"] == team]
        return {"items": items, "total": len(items)}

    @mcp.tool()
    async def get_app_overview(app_id: int) -> dict:
        if app_id == BOOM_APP_ID:
            raise RuntimeError("boom")
        app = APPS[app_id]
        return {**app, "pipelines": [], "resources": RESOURCES.get(app_id, [])}

    @mcp.tool()
    async def send_feishu_message(target: str, content: str, target_type: str = "chat") -> dict:
        sent.append({"target": target, "content": content})
        return {"sent": True, "target_type": target_type, "target": target}

    return mcp


def build_gitlab_mcp_mock() -> FastMCP:
    mcp = FastMCP("gitlab-mcp-mock", stateless_http=True)

    @mcp.tool()
    async def compare(project=None, ref_from=None, ref_to=None) -> dict:
        return {"diffs": [{"old_path": f"f{i}.py"} for i in range(12)],
                "commits": [{"title": "fix: order bug"}]}

    return mcp


class MockMCPServer:
    """在临时端口上以线程方式运行 FastMCP streamable-http 应用（自带 lifespan）。"""

    def __init__(self, mcp: FastMCP) -> None:
        self._app = mcp.streamable_http_app()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    async def start(self) -> str:
        config = uvicorn.Config(self._app, host="127.0.0.1", port=0, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        for _ in range(200):
            if self._server.started and self._server.servers:
                port = self._server.servers[0].sockets[0].getsockname()[1]
                return f"http://127.0.0.1:{port}/mcp"
            await asyncio.sleep(0.05)
        raise RuntimeError("mock MCP server 启动超时")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)


@pytest.fixture()
def rest_start():
    servers = [
        MockASGIServer(build_platform_rest_app()),
        MockASGIServer(build_prometheus_rest_app()),
        MockASGIServer(build_gitlab_rest_app()),
    ]

    async def _start() -> dict[str, str]:
        urls = await asyncio.gather(*(s.start() for s in servers))
        return {"platform": urls[0], "prometheus": urls[1], "gitlab": urls[2]}

    try:
        yield _start
    finally:
        for server in servers:
            server.stop()


@pytest.fixture()
def bot_mcp_start():
    sent: list[dict] = []
    servers = [
        MockMCPServer(build_bingops_mcp_mock(sent)),
        MockMCPServer(build_gitlab_mcp_mock()),
    ]

    async def _start() -> dict[str, str]:
        urls = await asyncio.gather(*(s.start() for s in servers))
        return {"bingops": urls[0], "gitlab": urls[1], "sent": sent}

    try:
        yield _start
    finally:
        for server in servers:
            server.stop()


def _write_test_config(
    path: Path,
    rest: dict[str, str],
    out_dir: Path,
    *,
    llm_base_url: str | None = None,
    oss_enabled: bool = False,
    gitlab_rest_enabled: bool = True,
    bot_mcp: dict[str, str] | None = None,
    prometheus_external: dict[str, Any] | None = None,
    prometheus_routes: dict[str, str] | None = None,
) -> None:
    config: dict[str, Any] = {
        "window": {"hours": 24, "step": "5m"},
        "schedule": {"cron": "0 10 * * *", "timezone": "UTC"},
        "teams": ["t1"],
        "category_by_model": {"ecs": "host", "pod": "pod"},
        "checks": {
            "host": ["cpu", "memory", "disk"],
            "pod": ["cpu", "memory", "restarts"],
        },
        "thresholds": {
            "cpu_high_pct": 80,
            "cpu_high_duration_min": 30,
            "disk_warn_pct": 85,
            "disk_crit_pct": 95,
            "mem_high_pct": 90,
            "mem_high_duration_min": 30,
            "error_rate_5xx": 0.01,
            "pod_restart_min_count": 3,
        },
        "prom_queries": {
            "host": {
                "cpu": '100 * (1 - avg by (instance) '
                       '(rate(node_cpu_seconds_total{instance=~"{selector}", mode="idle"}[5m])))',
                "memory": '100 * (1 - node_memory_MemAvailable_bytes{instance=~"{selector}"}'
                          ' / node_memory_MemTotal_bytes{instance=~"{selector}"})',
                "disk": '100 * (1 - node_filesystem_avail_bytes{instance=~"{selector}"}'
                        ' / node_filesystem_size_bytes{instance=~"{selector}"})',
            },
            "pod": {
                "restarts": 'increase(kube_pod_container_status_restarts_total'
                            '{pod=~"{selector}"}[{window}])',
            },
        },
        "platform": {"base_url": rest["platform"], "timeout_sec": 30},
        "external": {
            "prometheus": prometheus_external
            if prometheus_external is not None
            else {"base_url": rest["prometheus"]},
            **(
                {"gitlab": {"base_url": rest["gitlab"], "token_env": "GITLAB_TOKEN"}}
                if gitlab_rest_enabled
                else {}
            ),
        },
        **({"prometheus_routes": prometheus_routes} if prometheus_routes else {}),
        "feishu": {"target_type": "chat", "chat_id_env": "FEISHU_REPORT_CHAT_ID"},
        "mcp_servers": {
            **(
                {
                    "bingops": {"url": bot_mcp["bingops"]},
                    "gitlab": {"url": bot_mcp["gitlab"]},
                }
                if bot_mcp
                else {}
            ),
        },
        "clues": {"enabled": True, "topology_depth": 2, "git_compare": True,
                  "per_anomaly_timeout_sec": 10},
        "query": {
            "per_app_timeout_sec": 20,
            "report_deadline_sec": 120,
            "list_limit_default": 20,
            "list_limit_max": 100,
            "app_concurrency": 4,
        },
        "output": {"out_dir": str(out_dir), "archive_ticket_enabled": False},
    }
    if oss_enabled:
        config["oss"] = {
            "enabled": True,
            "endpoint_env": "OSS_ENDPOINT",
            "bucket_env": "OSS_BUCKET",
            "access_key_id_env": "OSS_ACCESS_KEY_ID",
            "access_key_secret_env": "OSS_ACCESS_KEY_SECRET",
            "object_prefix": "inspection-report/",
            "url_expires_sec": 604800,
        }
    if llm_base_url:
        config["llm"] = {
            "enabled": True,
            "base_url": llm_base_url,
            "api_key_env": "LLM_API_KEY",
            "model": "fake",
            "temperature": 0.2,
            "timeout_sec": 30,
            "max_tokens": 2000,
            "token_budget": 10**6,
        }
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")


async def _run_report(
    tmp_path, rest_start, monkeypatch, *, llm_mode: str | None = None,
    oss_enabled: bool = False, gitlab_rest_enabled: bool = True,
    bot_mcp: dict[str, str] | None = None, dry_run: bool = True,
    prometheus_external: dict[str, Any] | None = None,
    prometheus_routes: dict[str, str] | None = None,
):
    if llm_mode is not None:
        monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("BINGOPS_AGENT_TOKEN", "test-jwt")   # 平台 REST 直连凭据（联调模式）
    monkeypatch.setenv("GITLAB_TOKEN", "test-git-token")     # git 线索（REST PRIVATE-TOKEN）
    rest = await rest_start()
    llm_url = (
        await MockASGIServer(build_llm_app(llm_mode)).start() if llm_mode else None
    )
    config_path = tmp_path / "inspection.yaml"
    out_dir = tmp_path / "out"
    _write_test_config(
        config_path, rest, out_dir,
        llm_base_url=llm_url, oss_enabled=oss_enabled,
        gitlab_rest_enabled=gitlab_rest_enabled, bot_mcp=bot_mcp,
        prometheus_external=prometheus_external,
        prometheus_routes=prometheus_routes,
    )
    rc = await run_report(
        config_path,
        report_date=date(2026, 9, 2),
        dry_run=dry_run,
        now=datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc),
    )
    return rc, out_dir / "2026-09-02"


# ---------------------------------------------------------------- 巡检日报全链路


async def test_full_pipeline_produces_contract_report(tmp_path, rest_start, monkeypatch):
    rc, report_dir = await _run_report(tmp_path, rest_start, monkeypatch)
    assert rc == 0, "run_report 应成功（单应用失败隔离，不中断整报）"

    struct = json.loads((report_dir / "anomalies.json").read_text(encoding="utf-8"))
    assert struct["apps_inspected"] == 3
    assert struct["apps_missing_data"] == ["故障应用"]
    assert struct["governance"]["missing_reasons"]["故障应用"]
    assert {c["ticket_no"] for c in struct["changes"]} == {"T-77", "T-78"}

    rules = {a["rule"] for a in struct["anomalies"]}
    assert {"cpu_high", "disk_usage", "pod_restart", "change_failed"} <= rules
    for anomaly in struct["anomalies"]:
        assert isinstance(anomaly["observed"], (int, float))
        assert not isinstance(anomaly["observed"], bool)
        assert anomaly["evidence_url"].strip()
        assert isinstance(anomaly.get("clues"), list)
    disk = next(a for a in struct["anomalies"] if a["rule"] == "disk_usage")
    assert disk["severity"] == "critical" and disk["observed"] == 96.0

    report = (report_dir / "report.txt").read_text(encoding="utf-8")
    for section in ("一、总览", "二、异常项", "三、风险提示", "四、昨日变更回顾", "五、待办建议"):
        assert section in report, f"缺少报告段落: {section}"
    assert "数据缺失补采：故障应用" in report

    for script, target in (
        ("validate_anomalies.py", report_dir / "anomalies.json"),
        ("validate_config.py", tmp_path / "inspection.yaml"),
    ):
        proc = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / ".qoder" / "skills" / "inspection-agent-dev" / "scripts" / script),
             str(target)],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        assert proc.returncode == 0, f"{script} 校验失败:\n{proc.stdout}\n{proc.stderr}"


# ---------------------------------------------------------------- P1：LLM 叙事三态


def build_llm_app(mode: str):
    """OpenAI 兼容假 LLM（/v1/chat/completions）：narrate_ok/tamper/bot/down。"""

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        if mode == "down":
            response = json.dumps({"error": "llm down"}).encode()
            await send({"type": "http.response.start", "status": 500,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": response})
            return
        request = json.loads(body)
        messages = request.get("messages") or []
        if request.get("tools"):
            if any(m.get("role") == "tool" for m in messages):
                tool_content = next(
                    m["content"] for m in reversed(messages) if m.get("role") == "tool"
                )
                try:
                    count = len(json.loads(tool_content).get("items") or [])
                except ValueError:
                    count = 0
                message = {"role": "assistant", "content": f"共 {count} 个应用（数据来自工具）"}
            else:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "bingops__list_business_apps",
                                                 "arguments": '{"team": "t1"}'}}],
                }
        else:
            payload = json.loads(messages[-1]["content"])
            message = {"role": "assistant", "content": _narrative_from_payload(payload, mode)}
        response = json.dumps({"choices": [{"message": message}]}).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": response})

    return app


def _narrative_from_payload(payload: dict, mode: str) -> str:
    lines = [
        f"【全量 巡检日报 {payload.get('report_date')}】",
        str(payload.get("overview_line")),
        "二、异常项：",
    ]
    for a in payload.get("anomalies") or []:
        threshold = a.get("threshold")
        thr = "" if threshold is None else f"（阈值 {threshold}）"
        lines.append(
            f"  [{a['severity']}] {a['app_name']} / {a['resource_name']}"
            f" {a['metric']} 观测 {a['observed']}{thr}"
        )
        lines.append(f"  证据: {a['evidence_url']}")
    lines += [
        "三、风险提示：", "  无", "四、昨日变更回顾：",
        "  " + str(payload.get("changes_summary") or "无"), "五、待办建议：", "  - 数据缺失补采",
    ]
    if mode == "tamper":
        lines.append("  磁盘观测 99.9，需关注")
    return "\n".join(lines)


async def test_narrative_llm_success(tmp_path, rest_start, monkeypatch):
    rc, report_dir = await _run_report(tmp_path, rest_start, monkeypatch, llm_mode="narrate_ok")
    assert rc == 0
    struct = json.loads((report_dir / "anomalies.json").read_text(encoding="utf-8"))
    assert struct["report_mode"] == "llm"
    report = (report_dir / "report.txt").read_text(encoding="utf-8")
    assert "二、异常项" in report and "证据:" in report


async def test_narrative_tamper_degrades_to_template(tmp_path, rest_start, monkeypatch):
    rc, report_dir = await _run_report(tmp_path, rest_start, monkeypatch, llm_mode="tamper")
    assert rc == 0
    struct = json.loads((report_dir / "anomalies.json").read_text(encoding="utf-8"))
    assert struct["report_mode"] == "template"
    expected = render_report(struct)
    assert (report_dir / "report.txt").read_text(encoding="utf-8") == expected


async def test_narrative_llm_down_degrades_to_template(tmp_path, rest_start, monkeypatch):
    rc, report_dir = await _run_report(tmp_path, rest_start, monkeypatch, llm_mode="down")
    assert rc == 0
    struct = json.loads((report_dir / "anomalies.json").read_text(encoding="utf-8"))
    assert struct["report_mode"] == "template"


from inspection_agent.report.render import render_report  # noqa: E402

# ---------------------------------------------------------------- P1.5：趋势 + OSS


async def test_trend_risks_and_html_report(tmp_path, rest_start, monkeypatch):
    rc, report_dir = await _run_report(tmp_path, rest_start, monkeypatch)
    assert rc == 0
    struct = json.loads((report_dir / "anomalies.json").read_text(encoding="utf-8"))
    trend_risks = [r for r in struct["risks"] if r.get("type") == "trend"]
    assert trend_risks, "disk 线性增长应产生趋势风险"
    disk_trend = next(r for r in trend_risks if r["metric"] == "disk")
    assert disk_trend["direction"] == "up"
    assert disk_trend["change_pct"] >= 20 and disk_trend["window_days"] == 7
    html = (report_dir / "report.html").read_text(encoding="utf-8")
    assert "趋势关注" in html and "<table>" in html


async def test_oss_success_pushes_summary_with_link(
    tmp_path, rest_start, bot_mcp_start, monkeypatch
):
    monkeypatch.setenv("FEISHU_REPORT_CHAT_ID", "oc_test")
    for name in ("OSS_ENDPOINT", "OSS_BUCKET", "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET"):
        monkeypatch.setenv(name, "fake")
    bot_mcp = await bot_mcp_start()

    class FakeBucket:
        def __init__(self, *args) -> None:
            pass

        def put_object_from_file(self, key, path) -> None:
            pass

        def sign_url(self, method, key, expires) -> str:
            return f"http://fake-oss/{key}?sig=1"

    monkeypatch.setattr("oss2.Bucket", FakeBucket)
    rc, _ = await _run_report(
        tmp_path, rest_start, monkeypatch, oss_enabled=True, dry_run=False, bot_mcp=bot_mcp
    )
    assert rc == 0
    assert bot_mcp["sent"], "日报摘要应经平台 send_feishu_message 出站"
    first = bot_mcp["sent"][0]["content"]
    assert "完整报告：http://fake-oss/inspection-report/2026-09-02/report.html?sig=1" in first
    assert "趋势关注" in first
    assert "根因线索" in first


async def test_oss_failure_marks_summary_and_continues(
    tmp_path, rest_start, bot_mcp_start, monkeypatch
):
    """OSS 失败：消息照发并标注生成失败，本地 HTML 保留，rc=0（SKILL 红线 7）。"""
    monkeypatch.setenv("FEISHU_REPORT_CHAT_ID", "oc_test")
    for name in ("OSS_ENDPOINT", "OSS_BUCKET", "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET"):
        monkeypatch.setenv(name, "fake")
    bot_mcp = await bot_mcp_start()

    class BrokenBucket:
        def __init__(self, *args) -> None:
            pass

        def put_object_from_file(self, key, path) -> None:
            raise ConnectionError("refused")

    monkeypatch.setattr("oss2.Bucket", BrokenBucket)
    rc, report_dir = await _run_report(
        tmp_path, rest_start, monkeypatch, oss_enabled=True, dry_run=False, bot_mcp=bot_mcp
    )
    assert rc == 0
    assert bot_mcp["sent"], "OSS 失败时消息仍应照发"
    assert "完整报告：生成失败，请检查 OSS 配置" in bot_mcp["sent"][0]["content"]
    assert (report_dir / "report.html").exists()


# ---------------------------------------------------------------- P2：根因线索


async def test_clues_pipeline(tmp_path, rest_start, monkeypatch):
    rc, report_dir = await _run_report(tmp_path, rest_start, monkeypatch)
    assert rc == 0
    struct = json.loads((report_dir / "anomalies.json").read_text(encoding="utf-8"))
    order = next(a for a in struct["anomalies"] if a["app_name"] == "订单中心")
    types = {c["type"] for c in order["clues"]}
    assert {"change", "topology", "git"} <= types
    git = next(c for c in order["clues"] if c["type"] == "git")
    assert "v1.2.2→v1.2.3" in git["text"] and "12 个文件" in git["text"]
    change = next(c for c in order["clues"] if c["type"] == "change")
    assert "v1.2.3" in change["text"] and change["evidence_url"] == "T-77"
    topo = next(c for c in order["clues"] if c["type"] == "topology")
    assert "mysql-01" in topo["text"]
    html = (report_dir / "report.html").read_text(encoding="utf-8")
    assert "clue-type" in html and "topology" in html


async def test_clues_git_rest_down(tmp_path, rest_start, monkeypatch):
    """GitLab REST 断供：git 线索跳过，其余三类不受影响，rc=0。"""
    rc, report_dir = await _run_report(tmp_path, rest_start, monkeypatch, gitlab_rest_enabled=False)
    assert rc == 0
    struct = json.loads((report_dir / "anomalies.json").read_text(encoding="utf-8"))
    order = next(a for a in struct["anomalies"] if a["app_name"] == "订单中心")
    types = {c["type"] for c in order["clues"]}
    assert {"change", "topology"} <= types
    assert "git" not in types


# ---------------------------------------------------------------- 多监控源定向巡检


async def test_multi_prometheus_routes_by_team(tmp_path, rest_start, monkeypatch):
    """单 agent 双监控实例：团队 t1 路由到第二实例，指标与证据链接均来自该实例。"""
    rest = await rest_start()
    second = MockASGIServer(build_prometheus_rest_app(disk_mode="flat88"))
    second_url = await second.start()
    try:
        rc, report_dir = await _run_report(
            tmp_path, rest_start, monkeypatch,
            prometheus_external={
                "neibu": {"base_url": rest["prometheus"]},
                "waibu": {"base_url": second_url},
            },
            prometheus_routes={"t1": "waibu"},
        )
        assert rc == 0
        struct = json.loads((report_dir / "anomalies.json").read_text(encoding="utf-8"))
        disk = next(a for a in struct["anomalies"] if a["rule"] == "disk_usage")
        # 磁盘值来自第二实例（恒 88 warning），而非第一实例的线性 60→96（critical 96）
        assert disk["observed"] == 88.0
        assert disk["severity"] == "warning"
        assert disk["evidence_url"].startswith(second_url)
        cpu = next(a for a in struct["anomalies"] if a["rule"] == "cpu_high")
        assert cpu["evidence_url"].startswith(second_url)  # 证据跟随所属实例
    finally:
        second.stop()


# ---------------------------------------------------------------- P2-charter：bot HTTP 端点


async def test_bot_receives_platform_event(tmp_path, rest_start, bot_mcp_start, monkeypatch):
    """平台转发事件 → POST /feishu/events → agent → send_feishu_message 出站。"""
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    rest = await rest_start()
    bot_mcp = await bot_mcp_start()
    llm_url = await MockASGIServer(build_llm_app("bot")).start()
    config_path = tmp_path / "inspection.yaml"
    _write_test_config(
        config_path, rest, tmp_path / "out",
        llm_base_url=llm_url, bot_mcp=bot_mcp,
    )

    from inspection_agent.bot.server import build_agent
    from inspection_agent.config import load_config
    from inspection_agent.mcpclient import MCPServerPool

    config = load_config(config_path)
    pool = MCPServerPool(config.mcp_servers, timeout_sec=20)
    await pool.__aenter__()
    try:
        agent = await build_agent(config, pool)
        app = create_app(config, pool=pool, agent=agent)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://bot") as client:
            health = await client.get("/healthz")
            assert health.status_code == 200
            resp = await client.post(
                "/feishu/events",
                json={"event_id": "e1", "chat_id": "oc_test", "text": "有哪些应用？"},
            )
            assert resp.status_code == 202
            for _ in range(60):
                if bot_mcp["sent"]:
                    break
                await asyncio.sleep(0.1)
            assert bot_mcp["sent"], "bot 应经平台 send_feishu_message 出站"
            assert "个应用" in bot_mcp["sent"][0]["content"]
    finally:
        await pool.__aexit__(None, None, None)
