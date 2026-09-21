"""Prometheus 原生 HTTP API 数据源（双轨制：pipeline 直连 REST，MCP 只留给 bot）。

- 端点：GET {base_url}/api/v1/query_range?query&start&end&step（RFC3339 时间）；
- 响应：{status, data: {resultType, result}}——normalize_matrix 兼容
  裸列表 / {resultType, result} / {data: {...}} 三种形态；
- 窗口统计 avg/max/p95 与"最长连续高于阈值时长"全部在此确定性计算——数字只来自规则链路。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from .errors import MCPlessSourceError

logger = logging.getLogger(__name__)


@dataclass
class MetricSeries:
    labels: dict[str, str] = field(default_factory=dict)
    times: list[float] = field(default_factory=list)  # epoch 秒（P1.5 趋势日聚合用）
    values: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class WindowStats:
    """单资源单检查项的窗口统计（跨 series 聚合口径见 window_stats 注释）。"""

    avg: float
    max: float
    p95: float
    series_count: int


class PrometheusSource:
    def __init__(self, base_url: str, timeout_sec: float = 30.0) -> None:
        self._base = (base_url or "").rstrip("/")
        self._timeout = timeout_sec

    @property
    def base_url(self) -> str:
        return self._base

    async def query_range(
        self, promql: str, start: datetime, end: datetime, step_seconds: int
    ) -> list[MetricSeries]:
        url = f"{self._base}/api/v1/query_range"
        params = {
            "query": promql,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "step": f"{step_seconds}s",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise MCPlessSourceError(f"prometheus query_range 请求失败: {exc}") from exc
        except ValueError as exc:
            raise MCPlessSourceError(f"prometheus 返回非 JSON: {exc}") from exc
        if isinstance(data, dict) and data.get("status") not in (None, "success"):
            logger.warning("prometheus 查询异常（按空数据处理）: %s", str(data)[:200])
            return []
        return normalize_matrix(data)

    def graph_url(self, promql: str, start: datetime, end: datetime) -> str | None:
        """Prom 图证据链接；未配置 base_url 时返回 None（调用方以表达式为证据）。"""
        if not self._base:
            return None
        hours = max(1, int((end - start).total_seconds() // 3600))
        params = urlencode({"g0.expr": promql, "g0.range_input": f"{hours}h", "g0.tab": 0})
        return f"{self._base}/graph?{params}"


def normalize_matrix(data: Any) -> list[MetricSeries]:
    """Prometheus range/matrix 返回归一化为 MetricSeries（丢弃 NaN/非数值点）。"""
    rows: Any = data
    if isinstance(data, dict):
        payload = data.get("data", data)
        rows = payload.get("result", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        logger.warning("prometheus 返回结构无法识别，按空数据处理")
        return []
    series: list[MetricSeries] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        times: list[float] = []
        values: list[float] = []
        for point in row.get("values") or []:
            if not (isinstance(point, (list, tuple)) and len(point) >= 2):
                continue
            try:
                ts = float(point[0])
                num = float(point[1])
            except (TypeError, ValueError):
                continue
            if math.isfinite(num):
                times.append(ts)
                values.append(num)
        series.append(MetricSeries(labels=dict(row.get("metric") or {}), times=times, values=values))
    return series


def window_stats(series: list[MetricSeries]) -> WindowStats | None:
    """跨 series 聚合：avg=各 series 均值的均值；max=最差 series 最大值；p95=各 series p95 的最大值。

    口径说明：巡检关注"最坏情况"，disk 多挂载点取 max 最直观；avg 用于报告参考。
    """
    per_series = [s.values for s in series if s.values]
    if not per_series:
        return None
    avgs = [sum(v) / len(v) for v in per_series]
    maxes = [max(v) for v in per_series]
    p95s = [percentile_nearest_rank(sorted(v), 0.95) for v in per_series]
    return WindowStats(
        avg=sum(avgs) / len(avgs),
        max=max(maxes),
        p95=max(p95s),
        series_count=len(per_series),
    )


def percentile_nearest_rank(sorted_values: list[float], q: float) -> float:
    """最近秩分位数（确定性，无插值）。"""
    if not sorted_values:
        raise ValueError("空序列无分位数")
    rank = max(1, math.ceil(q * len(sorted_values)))
    return sorted_values[rank - 1]


def duration_above_minutes_with_step(
    series: list[MetricSeries], threshold: float, step_seconds: int
) -> float:
    """所有 series 中"最长连续 ≥ 阈值"的时长（分钟）。

    空洞（NaN 已在 normalize 阶段剔除）不中断连续性——与 PromQL 持续判定近似，联调可校准。
    """
    longest_points = 0
    for s in series:
        current = 0
        for value in s.values:
            if value >= threshold:
                current += 1
                longest_points = max(longest_points, current)
            else:
                current = 0
    return longest_points * step_seconds / 60.0
