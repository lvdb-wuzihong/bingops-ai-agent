"""GitLab REST API v4 数据源（双轨制：pipeline 直连 REST，MCP 只留给 bot）。

- 端点：GET {base}/api/v4/projects/{urlencoded_path}/repository/compare?from&to
  （PRIVATE-TOKEN 认证；project 为规范化 repo_url 解析出的 group/project）；
- normalize_compare 兼容 diffs/files 与 commits 等常见返回字段名（联调校准）。
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

from .prometheus import MCPlessSourceError

logger = logging.getLogger(__name__)

_MAX_COMMITS = 3


class GitLabSource:
    def __init__(
        self, base_url: str, token: str | None = None, timeout_sec: float = 10.0
    ) -> None:
        self._base = (base_url or "").rstrip("/")
        self._token = token or ""
        self._timeout = timeout_sec

    async def compare(self, project: str, ref_from: str, ref_to: str) -> dict[str, Any]:
        """tag/branch 对比：变更文件数 + 提交主题（截断）。"""
        quoted = quote(project, safe="")
        url = f"{self._base}/api/v4/projects/{quoted}/repository/compare"
        headers = {"PRIVATE-TOKEN": self._token} if self._token else {}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, params={"from": ref_from, "to": ref_to}, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise MCPlessSourceError(f"gitlab compare 请求失败: {exc}") from exc
        except ValueError as exc:
            raise MCPlessSourceError(f"gitlab 返回非 JSON: {exc}") from exc
        return normalize_compare(data)


def normalize_compare(data: Any) -> dict[str, Any]:
    """compare 返回归一化：兼容 diffs/files 与 commits 等常见返回字段名（联调校准）。"""
    if not isinstance(data, dict):
        return {"files_count": 0, "commits": []}
    files = data.get("diffs") or data.get("files") or []
    commits = data.get("commits") or []
    files_count = len(files) if isinstance(files, list) else int(data.get("diffs_count") or 0)
    titles: list[str] = []
    for commit in commits[:_MAX_COMMITS] if isinstance(commits, list) else []:
        if not isinstance(commit, dict):
            continue
        title = str(commit.get("title") or "").strip()
        if not title:
            message = str(commit.get("message") or "").strip()
            title = message.splitlines()[0] if message else ""
        if title:
            titles.append(title)
    return {"files_count": files_count, "commits": titles}


def parse_project(repo_url: str) -> str | None:
    """解析规范化 repo_url 为 GitLab project path。

    https://git.example.com/group/project.git → group/project（平台改造 2 约定格式）。
    """
    url = repo_url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[: -len(".git")]
    if "://" not in url:
        return None
    path = url.split("://", 1)[1]
    _, _, project = path.partition("/")
    project = project.strip("/")
    return project or None


def compare_web_url(repo_url: str, ref_from: str, ref_to: str) -> str | None:
    """GitLab compare 网页链接（证据）；repo_url 非法返回 None。"""
    project = parse_project(repo_url)
    if not project:
        return None
    scheme, _, rest = repo_url.partition("://")
    host = rest.split("/", 1)[0]
    return f"{scheme}://{host}/{project}/-/compare/{ref_from}...{ref_to}"
