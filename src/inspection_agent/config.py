"""配置加载：YAML → 强类型 AppConfig（样例见 config/inspection.example.yaml；
运行时配置由部署侧提供，K8s 以 ConfigMap 挂载到容器内路径后经 --config 指定）。

结构定义 = .qoder/skills/inspection-agent-dev/reference.md §2；
完整格式校验由 .qoder/skills/inspection-agent-dev/scripts/validate_config.py 在开发期把关，
本模块只做加载期最小校验（缺必填即报错，未知阈值键降级为告警）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_STEP_UNITS = {"s": 1, "m": 60, "h": 3600}


class ConfigError(ValueError):
    """配置缺失或非法。"""


def parse_duration_seconds(text: str) -> int:
    """解析 '5m'/'30s'/'1h' 为秒数。"""
    text = str(text).strip()
    if len(text) < 2 or text[-1].lower() not in _STEP_UNITS:
        raise ConfigError(f"时长格式非法: {text!r}（应为 <数字><s|m|h>，如 5m）")
    unit = _STEP_UNITS[text[-1].lower()]
    try:
        value = float(text[:-1])
    except ValueError as exc:
        raise ConfigError(f"时长格式非法: {text!r}") from exc
    if value <= 0:
        raise ConfigError(f"时长必须 > 0: {text!r}")
    return int(value * unit)


@dataclass(frozen=True)
class McpServerConfig:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    options: dict[str, object] = field(default_factory=dict)  # source 自定义键（如 query_range_tool）


@dataclass(frozen=True)
class Thresholds:
    """异常判定阈值（SKILL 异常判定规则表；全部来自 YAML，代码禁止硬编码）。"""

    cpu_high_pct: float = 80.0
    cpu_high_duration_min: float = 30.0
    disk_warn_pct: float = 85.0
    disk_crit_pct: float = 95.0
    mem_high_pct: float = 90.0
    mem_high_duration_min: float = 30.0
    error_rate_5xx: float = 0.01
    pod_restart_min_count: int = 3


@dataclass(frozen=True)
class QueryConfig:
    per_app_timeout_sec: float = 30.0
    report_deadline_sec: float = 300.0
    list_limit_default: int = 20
    list_limit_max: int = 100
    app_concurrency: int = 8


@dataclass(frozen=True)
class OutputConfig:
    out_dir: str = "out"
    archive_ticket_enabled: bool = False   # 可选 general 工单归档（写操作，默认关闭，SKILL 红线 2）


@dataclass(frozen=True)
class LLMConfig:
    """OpenAI 兼容 API 配置（P1 叙事层；密钥从环境变量读取，不入码）。"""

    enabled: bool = False
    base_url: str = ""          # 形如 https://api.example.com/v1（POST {base_url}/chat/completions）
    api_key_env: str = "LLM_API_KEY"
    model: str = ""
    temperature: float = 0.2    # 低温度保数字稳定
    timeout_sec: float = 60.0
    max_tokens: int = 2000
    token_budget: int = 4000    # 送入 LLM 的 payload 上限（字符近似）


@dataclass(frozen=True)
class BotConfig:
    """飞书聊天助手配置（P2-charter：接收平台转发 + 出站走平台 send_feishu_message）。"""

    enabled: bool = False
    port: int = 8080                     # POST /feishu/events + /healthz 监听端口（需 Service）
    max_history_per_chat: int = 20
    max_tool_iterations: int = 8
    tool_result_max_chars: int = 2000
    tool_allowlist: dict[str, list[str]] | None = None  # None = 默认白名单；只能收窄


@dataclass(frozen=True)
class TrendConfig:
    """趋势对比配置（P1.5；口径 = 昨日 vs 前 days 日均值，见 reference.md §9）。"""

    enabled: bool = True
    days: int = 7
    step: str = "1h"                      # 趋势查询步长（8 天 × 1h = 192 点/系列）
    step_seconds: int = 3600
    change_pct_threshold: float = 20.0    # |变化率| 阈值
    min_delta: float = 5.0                # 绝对差护栏（百分点），防小基数抖动


@dataclass(frozen=True)
class OssConfig:
    """阿里云 OSS 配置（P1.5；凭据从环境变量读取，不入码）。"""

    enabled: bool = False
    endpoint_env: str = "OSS_ENDPOINT"
    bucket_env: str = "OSS_BUCKET"
    access_key_id_env: str = "OSS_ACCESS_KEY_ID"
    access_key_secret_env: str = "OSS_ACCESS_KEY_SECRET"
    object_prefix: str = "inspection-report/"
    url_expires_sec: int = 604800         # 签名 URL 有效期（默认 7 天）


@dataclass(frozen=True)
class CluesConfig:
    """根因线索配置（P2；四类线索：变更/告警/拓扑/git compare）。"""

    enabled: bool = True
    topology_depth: int = 2               # get_topology 展开深度（≤3 防爆炸，设计文档 §4.3-B）
    git_compare: bool = True              # GitLab tag compare 线索（需 mcp_servers.gitlab + repo_url）
    per_anomaly_timeout_sec: float = 10.0


@dataclass(frozen=True)
class PlatformConfig:
    """bingops 平台 REST（pipeline 取数路径，双轨制；凭据从环境变量读取）。"""

    base_url: str = ""
    agent_token_env: str = "BINGOPS_AGENT_TOKEN"          # 直接 token 模式（联调期）
    username_env: str = "BINGOPS_AGENT_USERNAME"          # 自动登录模式（生产）
    password_env: str = "BINGOPS_AGENT_PASSWORD"
    timeout_sec: float = 30.0


@dataclass(frozen=True)
class ExternalEndpointConfig:
    """外部系统直连端点（pipeline 取数路径）。"""

    base_url: str = ""
    token_env: str = ""                 # 空 = 无鉴权
    token_header: str = "Authorization" # Authorization → Bearer 前缀；其他 header 名用裸 token
    timeout_sec: float = 30.0
    evidence_ui: str = "graph"          # 监控图表 UI：graph=Prometheus /graph；vmui=VictoriaMetrics /vmui/


@dataclass(frozen=True)
class FeishuOutboundConfig:
    """飞书出站（P2-charter：统一走平台 send_feishu_message，编排层零飞书凭据）。"""

    target_type: str = "chat"        # bot 回复目标类型（恒为来源会话，建议不改）
    report_target_type: str = ""    # 日报目标类型：chat=按会话 ID；user=按人 open_id；空=沿用 target_type
    chat_id_env: str = "FEISHU_REPORT_CHAT_ID"   # 日报推送目标（report_target_type 决定其语义）


@dataclass(frozen=True)
class AppConfig:
    teams: list[str]
    checks: dict[str, list[str]]
    thresholds: Thresholds
    prom_queries: dict[str, dict[str, str]]
    category_by_model: dict[str, str]
    label_map: dict[str, str]
    window_hours: int
    step: str
    step_seconds: int
    timezone: str
    mcp_servers: dict[str, McpServerConfig]
    query: QueryConfig
    output: OutputConfig
    platform: PlatformConfig
    external: dict[str, ExternalEndpointConfig]
    feishu: FeishuOutboundConfig
    # 多监控源（单 agent 巡检多业务）：实例表 + team 路由；单实例旧写法自动包装为 {"default": ...}
    prometheus_instances: dict[str, ExternalEndpointConfig] = field(default_factory=dict)
    default_prometheus: str = "default"
    prometheus_routes: dict[str, str] = field(default_factory=dict)
    llm: LLMConfig = field(default_factory=LLMConfig)
    bot: BotConfig = field(default_factory=BotConfig)
    trend: TrendConfig = field(default_factory=TrendConfig)
    oss: OssConfig = field(default_factory=OssConfig)
    clues: CluesConfig = field(default_factory=CluesConfig)


def load_config(path: Path) -> AppConfig:
    """加载并最小校验配置。缺必填抛 ConfigError；未知阈值键记警告并忽略。"""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"配置文件读取失败 {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 解析失败 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("配置顶层必须为映射")

    teams = data.get("teams")
    if not isinstance(teams, list) or not teams or not all(isinstance(t, str) and t.strip() for t in teams):
        raise ConfigError("teams 必须为非空字符串列表（巡检对象来源）")

    checks = data.get("checks")
    if not isinstance(checks, dict) or not checks:
        raise ConfigError("checks 必须为非空映射（资源类别 → 检查项矩阵）")

    thresholds = _thresholds(data.get("thresholds"))
    if not isinstance(data.get("thresholds"), dict):
        raise ConfigError("缺少 thresholds 段（阈值必须放 YAML，禁止硬编码在代码里）")

    mcp_servers = _mcp_servers(data.get("mcp_servers"))

    step = str((data.get("window") or {}).get("step", "5m"))
    window_hours = int((data.get("window") or {}).get("hours", 24))
    if window_hours < 1:
        raise ConfigError("window.hours 应 ≥1")

    prometheus = data.get("prometheus") or {}
    query_raw = data.get("query") or {}
    output_raw = data.get("output") or {}
    schedule = data.get("schedule") or {}
    platform_raw = data.get("platform") or {}
    external_raw = data.get("external") or {}
    feishu_raw = data.get("feishu") or {}
    llm_raw = data.get("llm") or {}
    bot_raw = data.get("bot") or {}
    trend_raw = data.get("trend") or {}
    oss_raw = data.get("oss") or {}
    clues_raw = data.get("clues") or {}

    trend_step = str(trend_raw.get("step", "1h"))

    # ---- 多监控源解析：external.prometheus 支持单对象（旧写法）与命名实例表两种形态 ----
    external_items: dict[str, ExternalEndpointConfig] = {}
    prometheus_instances: dict[str, ExternalEndpointConfig] = {}
    for name, spec in external_raw.items():
        if not isinstance(spec, dict):
            continue
        if str(name) == "prometheus":
            if "base_url" in spec or "token_env" in spec or "timeout_sec" in spec:
                # 旧单实例写法：自动包装为名为 default 的实例
                prometheus_instances["default"] = _section(ExternalEndpointConfig, spec)
                continue
            for inst_name, inst_spec in spec.items():
                if not isinstance(inst_spec, dict):
                    raise ConfigError(
                        f"external.prometheus.{inst_name} 必须为映射（监控实例配置：base_url/timeout_sec）"
                    )
                inst = _section(ExternalEndpointConfig, inst_spec)
                if not inst.base_url.strip():
                    raise ConfigError(f"external.prometheus.{inst_name}.base_url 必填（监控实例）")
                prometheus_instances[str(inst_name)] = inst
            continue
        external_items[str(name)] = _section(ExternalEndpointConfig, spec)

    if "prometheus" in external_raw and not prometheus_instances:
        raise ConfigError("external.prometheus 段为空：至少需要定义一个监控实例")
    if prometheus_instances and "default" not in prometheus_instances:
        # 无名为 default 的实例时，未路由团队兑底 = 声明顺序第一个实例
        default_prometheus = next(iter(prometheus_instances))
    else:
        default_prometheus = "default"

    routes_raw = data.get("prometheus_routes") or {}
    if not isinstance(routes_raw, dict):
        raise ConfigError("prometheus_routes 必须为映射（team → 监控实例名）")
    prometheus_routes: dict[str, str] = {}
    for team, inst in routes_raw.items():
        inst_name = str(inst)
        if inst_name not in prometheus_instances:
            raise ConfigError(
                f"prometheus_routes['{team}'] 指向的实例 '{inst_name}' 不存在于 external.prometheus"
            )
        prometheus_routes[str(team)] = inst_name

    return AppConfig(
        teams=[t.strip() for t in teams],
        checks={str(k): [str(c) for c in v] for k, v in checks.items() if isinstance(v, list)},
        thresholds=thresholds,
        prom_queries={k: dict(v) for k, v in (data.get("prom_queries") or {}).items() if isinstance(v, dict)},
        category_by_model={str(k): str(v) for k, v in (data.get("category_by_model") or {}).items()},
        label_map={str(k): str(v) for k, v in (prometheus.get("label_map") or {}).items()},
        window_hours=window_hours,
        step=step,
        step_seconds=parse_duration_seconds(step),
        timezone=str(schedule.get("timezone", "Asia/Shanghai")),
        mcp_servers=mcp_servers,
        query=QueryConfig(
            per_app_timeout_sec=float(query_raw.get("per_app_timeout_sec", 30)),
            report_deadline_sec=float(query_raw.get("report_deadline_sec", 300)),
            list_limit_default=int(query_raw.get("list_limit_default", 20)),
            list_limit_max=int(query_raw.get("list_limit_max", 100)),
            app_concurrency=int(query_raw.get("app_concurrency", 8)),
        ),
        output=OutputConfig(
            out_dir=str(output_raw.get("out_dir", "out")),
            archive_ticket_enabled=bool(output_raw.get("archive_ticket_enabled", False)),
        ),
        platform=PlatformConfig(
            base_url=str(platform_raw.get("base_url", "") or ""),
            agent_token_env=str(platform_raw.get("agent_token_env", "BINGOPS_AGENT_TOKEN")),
            username_env=str(platform_raw.get("username_env", "BINGOPS_AGENT_USERNAME")),
            password_env=str(platform_raw.get("password_env", "BINGOPS_AGENT_PASSWORD")),
            timeout_sec=float(platform_raw.get("timeout_sec", 30)),
        ),
        external=external_items,
        prometheus_instances=prometheus_instances,
        default_prometheus=default_prometheus,
        prometheus_routes=prometheus_routes,
        feishu=FeishuOutboundConfig(
            target_type=str(feishu_raw.get("target_type", "chat")),
            report_target_type=str(feishu_raw.get("report_target_type", "") or ""),
            chat_id_env=str(feishu_raw.get("chat_id_env", "FEISHU_REPORT_CHAT_ID")),
        ),
        llm=_section(LLMConfig, llm_raw),
        trend=TrendConfig(
            enabled=bool(trend_raw.get("enabled", True)),
            days=int(trend_raw.get("days", 7)),
            step=trend_step,
            step_seconds=parse_duration_seconds(trend_step),
            change_pct_threshold=float(trend_raw.get("change_pct_threshold", 20)),
            min_delta=float(trend_raw.get("min_delta", 5)),
        ),
        oss=_section(OssConfig, oss_raw),
        clues=CluesConfig(
            enabled=bool(clues_raw.get("enabled", True)),
            # 深度收敛到 1-3（设计文档 §4.3-B：防拓扑爆炸）
            topology_depth=max(1, min(3, int(clues_raw.get("topology_depth", 2)))),
            git_compare=bool(clues_raw.get("git_compare", True)),
            per_anomaly_timeout_sec=float(clues_raw.get("per_anomaly_timeout_sec", 10)),
        ),
        bot=BotConfig(
            enabled=bool(bot_raw.get("enabled", False)),
            port=int(bot_raw.get("port", 8080)),
            max_history_per_chat=int(bot_raw.get("max_history_per_chat", 20)),
            max_tool_iterations=int(bot_raw.get("max_tool_iterations", 8)),
            tool_result_max_chars=int(bot_raw.get("tool_result_max_chars", 2000)),
            tool_allowlist=(
                {str(k): [str(t) for t in v] for k, v in bot_raw["tool_allowlist"].items()}
                if isinstance(bot_raw.get("tool_allowlist"), dict)
                else None
            ),
        ),
    )


def _section(defaults: type, raw: dict) -> Any:
    """按 dataclass 字段收敛未知键（加载期容错，格式把关仍由 skill 校验脚本）。"""
    known = {f: raw[f] for f in defaults.__dataclass_fields__ if f in raw}
    return defaults(**known)


_THRESHOLD_FIELDS = {f for f in Thresholds.__dataclass_fields__}


def _thresholds(raw: object) -> Thresholds:
    if not isinstance(raw, dict):
        return Thresholds()
    unknown = set(raw) - _THRESHOLD_FIELDS
    if unknown:
        logger.warning("thresholds 含未知键（已忽略，若为新增规则请同步文档与 SKILL）: %s", sorted(unknown))
    values = {k: raw[k] for k in _THRESHOLD_FIELDS if k in raw}
    try:
        return Thresholds(**values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ConfigError(f"thresholds 字段类型非法: {exc}") from exc


def _mcp_servers(raw: object) -> dict[str, McpServerConfig]:
    """MCP 连接配置：双轨制后仅 bot（LLM 工具）与出站通道使用；不再强制特定 server。"""
    if not isinstance(raw, dict):
        return {}
    servers: dict[str, McpServerConfig] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict) or not str(spec.get("url", "")).strip():
            raise ConfigError(f"mcp_servers.{name} 缺少 url")
        known = {"url", "headers"}
        servers[str(name)] = McpServerConfig(
            url=str(spec["url"]),
            headers={str(k): str(v) for k, v in (spec.get("headers") or {}).items()},
            options={k: v for k, v in spec.items() if k not in known},
        )
    return servers
