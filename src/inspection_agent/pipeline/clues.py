"""根因线索（P2）：对命中异常注入结构化事实线索。

红线（SKILL 3/7）：
- 线索 = 规则/工具拼装的结构化事实，LLM 点评只可引用、不可推算；
- 单异常线索失败/超时 → clues=[]，绝不中断整报；全部只读；
- 变更线索复用 collect 已拉数据（零额外查询）；拓扑/git 仅对命中异常各一次调用。

三类：change（变更关联）/ topology（上下游依赖）/ git（tag compare）。
拓扑两级：应用级（/apps/{id}/topology，依赖应用/外部依赖）+ 资源级（/resources/{id}/topology，上游资源）。
注：alert 线索已随夜莺下线移除（2026-09），接入新告警源时恢复（git 历史可查）。
契约见 .qoder/skills/inspection-agent-dev/reference.md §10。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..config import AppConfig
from ..sources import Sources
from ..sources.errors import SourceError
from ..sources.gitlab import GitLabSource, compare_web_url, parse_project
from .collect import ReportData

logger = logging.getLogger(__name__)

_UPSTREAM_LIMIT = 5
_MAX_COMMITS = 3


def _all_changes(data: ReportData) -> list[dict[str, Any]]:
    """全量变更明细（按应用 + 未归集），jobs 已按时间倒序。"""
    changes: list[dict[str, Any]] = []
    for app in data.apps:
        changes.extend(app.changes)
    changes.extend(data.ungrouped_changes)
    return changes


def _change_clue(anomaly: dict[str, Any], changes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """变更关联：异常资源的 target_resources 命中优先，回退同应用变更。"""
    resource_id = anomaly.get("resource_id")
    app_name = str(anomaly.get("app_name") or "")
    candidates = [
        c for c in changes if resource_id is not None and resource_id in (c.get("target_ids") or [])
    ]
    if not candidates and app_name:
        candidates = [c for c in changes if c.get("app_name") == app_name]
    if not candidates:
        return None
    latest = candidates[0]  # jobs 时间倒序，取最近
    status = str(latest.get("status") or "")
    status_note = f"，结果 {status}" if status else ""
    text = (
        f"昨日 {latest.get('tag') or '无 tag'} 发布"
        f"（工单 {latest.get('ticket_no') or '-'}{status_note}）后出现"
    )
    return {
        "type": "change",
        "text": text,
        "evidence_url": str(latest.get("ticket_no") or latest.get("job_id") or "change"),
    }


def _find_previous_tag(
    anomaly: dict[str, Any], changes: list[dict[str, Any]]
) -> tuple[str, str] | None:
    """推断 (current_tag, previous_tag)：最近一次带 tag 变更 + 同应用更早的带 tag 变更。"""
    app_name = str(anomaly.get("app_name") or "")
    resource_id = anomaly.get("resource_id")
    current: dict[str, Any] | None = None
    for change in changes:
        if not change.get("tag"):
            continue
        if resource_id is not None and resource_id in (change.get("target_ids") or []):
            current = change
            break
        if app_name and change.get("app_name") == app_name:
            current = change
            break
    if current is None:
        return None
    for change in changes:
        if change is current or not change.get("tag"):
            continue
        if change.get("app_name") == current.get("app_name"):
            return str(current["tag"]), str(change["tag"])
    return None


def _normalize_upstream(data: Any, resource_id: Any = None) -> list[str]:
    """get_topology 返回宽松解析：优先 nodes+edges 图（平台 REST 形态），兼容 upstream 列表。"""
    if not isinstance(data, dict):
        return []
    nodes = data.get("nodes")
    edges = data.get("edges")
    if isinstance(nodes, list) and isinstance(edges, list):
        node_names = {
            str(n.get("id")): str(n.get("name") or n.get("id"))
            for n in nodes
            if isinstance(n, dict)
        }
        names: list[str] = []
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            src = str(edge.get("source"))
            dst = str(edge.get("target"))
            peer = src if dst == str(resource_id) else (dst if src == str(resource_id) else None)
            if peer and node_names.get(peer) and peer != str(resource_id):
                name = node_names[peer]
                if name not in names:
                    names.append(name)
        return names
    for key in ("upstream", "upstreams", "parents", "related"):
        items = data.get(key)
        if not isinstance(items, list):
            continue
        names = []
        for item in items:
            if isinstance(item, dict):
                name = item.get("name") or item.get("resource_name") or item.get("id")
            else:
                name = item
            if name is not None and str(name).strip():
                names.append(str(name))
        return names
    return []


def _app_topology_deps(data: Any) -> tuple[list[str], list[str]]:
    """应用级拓扑解析：(依赖应用名, 外部依赖名)。

    只取中心应用（is_center）的出向 depends_on 边；宽松解析，结构异常返回空。
    """
    if not isinstance(data, dict):
        return [], []
    nodes = data.get("nodes")
    edges = data.get("edges")
    if not (isinstance(nodes, list) and isinstance(edges, list)):
        return [], []
    names: dict[str, str] = {}
    center: str | None = None
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        names[node_id] = str(node.get("name") or node_id)
        if node.get("is_center"):
            center = node_id
    apps: list[str] = []
    externals: list[str] = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        relation = str(edge.get("relation") or "")
        if center is not None and str(edge.get("source")) != center:
            continue  # 只取中心应用的出向依赖
        target = str(edge.get("target") or "")
        name = names.get(target)
        if not name:
            continue
        if relation == "external_dependency" or (
            relation == "depends_on" and target.startswith("external:")
        ):
            if name not in externals:
                externals.append(name)
        elif relation == "depends_on" and target.startswith("app:"):
            if name not in apps:
                apps.append(name)
    return apps, externals


async def _topology_clue(
    sources: Sources, anomaly: dict[str, Any], config: AppConfig
) -> dict[str, Any] | None:
    """拓扑线索：依赖应用/外部依赖（应用级拓扑）+ 上游资源（资源级拓扑）。

    应用级优先（REST /apps/{id}/topology，能拿到资源级拓扑没有的依赖应用维度）；
    任一级失败/为空则跳过该级，全部为空返回 None。
    """
    app_id = anomaly.get("app_id")
    parts: list[str] = []

    # 应用级：依赖应用 + 外部依赖（任一级失败只跳过本级，不影响另一级）
    try:
        app_data = await sources.bingops.get_app_topology(int(app_id))  # type: ignore[arg-type]
        deps, externals = _app_topology_deps(app_data)
        if deps:
            parts.append(f"依赖应用：{'、'.join(deps[:_UPSTREAM_LIMIT])}")
        if externals:
            parts.append(f"外部依赖：{'、'.join(externals[:3])}")
    except Exception as exc:  # noqa: BLE001 单级容错（SourceError/替身缺方法等）
        logger.info("应用级拓扑跳过（app %s）: %s", app_id, exc)

    # 资源级：上游资源（原逻辑）
    resource_id = anomaly.get("resource_id")
    try:
        numeric_id = int(resource_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        numeric_id = None
    if numeric_id is not None:
        try:
            data = await sources.bingops.get_topology(numeric_id, config.clues.topology_depth)
            upstream = _normalize_upstream(data, resource_id)[:_UPSTREAM_LIMIT]
        except Exception as exc:  # noqa: BLE001 单级容错
            logger.info("资源级拓扑跳过（资源 %s）: %s", resource_id, exc)
            upstream = []
        if upstream:
            parts.append(f"上游资源：{'、'.join(upstream)}")

    if not parts:
        return None
    evidence = f"topology-app-{app_id}"
    if config.platform.base_url:
        evidence = f"{config.platform.base_url.rstrip('/')}/apps/{app_id}"
    return {
        "type": "topology",
        "text": "；".join(parts),
        "evidence_url": evidence,
    }


async def _git_clue(
    git_source: GitLabSource | None, data: ReportData, anomaly: dict[str, Any],
    changes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """git 线索：上一 tag → 昨日 tag 的 compare 摘要（GitLab REST）。"""
    if git_source is None:
        return None
    tags = _find_previous_tag(anomaly, changes)
    if tags is None:
        return None
    current_tag, previous_tag = tags
    app = next(
        (a for a in data.apps if a.app_name == anomaly.get("app_name")), None
    )
    if app is None or not app.repo_url:
        return None
    project = parse_project(app.repo_url)
    if not project:
        logger.debug("repo_url 无法解析 project，跳过 git 线索: %s", app.repo_url)
        return None
    try:
        result = await git_source.compare(project, previous_tag, current_tag)
    except SourceError as exc:
        logger.info("git 线索跳过（compare 失败）: %s", exc)
        return None
    commits = "；".join(
        c if isinstance(c, str) else str((c or {}).get("title") or "")
        for c in (result.get("commits") or [])
    ).strip("； ") or "无提交信息"
    text = (
        f"{previous_tag}→{current_tag} 变更 {result.get('files_count', 0)} 个文件：{commits}"
    )
    evidence = compare_web_url(app.repo_url, previous_tag, current_tag) or f"{previous_tag}...{current_tag}"
    return {"type": "git", "text": text, "evidence_url": evidence}


async def _clues_for_anomaly(
    anomaly: dict[str, Any],
    data: ReportData,
    sources: Sources,
    config: AppConfig,
    changes: list[dict[str, Any]],
    git_source: GitLabSource | None,
) -> list[dict[str, str]]:
    clues: list[dict[str, str]] = []
    add = clues.append
    change = _change_clue(anomaly, changes)
    if change:
        add(change)
    topology = await _topology_clue(sources, anomaly, config)
    if topology:
        add(topology)
    git = await _git_clue(git_source, data, anomaly, changes)
    if git:
        add(git)
    return clues


async def attach_clues(
    anomalies: list[dict[str, Any]],
    data: ReportData,
    sources: Sources,
    config: AppConfig,
) -> None:
    """对命中异常逐条注入 clues（runner 在 rules 后调用；REST 直连，无需 MCP 连接）。

    容错：单异常失败/超时 → clues=[]；gitlab 未配置/断供 → git 线索静默跳过。
    """
    if not config.clues.enabled or not anomalies:
        for anomaly in anomalies:
            anomaly.setdefault("clues", [])  # disabled 时也保证契约键存在
        return
    changes = _all_changes(data)
    git_source = sources.gitlab if config.clues.git_compare else None
    for anomaly in anomalies:
        anomaly.setdefault("clues", [])

    async def _one(anomaly: dict[str, Any]) -> None:
        try:
            anomaly["clues"] = await asyncio.wait_for(
                _clues_for_anomaly(anomaly, data, sources, config, changes, git_source),
                timeout=config.clues.per_anomaly_timeout_sec,
            )
        except TimeoutError:
            logger.warning("线索生成超时（保持空线索）: %s", anomaly.get("app_name"))
        except Exception:  # noqa: BLE001 单异常兜底，绝不上抛中断整报
            logger.exception("线索生成失败（保持空线索）: %s", anomaly.get("app_name"))

    await asyncio.gather(*(_one(anomaly) for anomaly in anomalies), return_exceptions=True)
    total = sum(len(a.get("clues") or []) for a in anomalies)
    logger.info("根因线索完成：%d 个异常，共 %d 条线索", len(anomalies), total)
