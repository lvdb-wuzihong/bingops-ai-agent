# AI Agent 与 MCP 体系设计文档

> 本文是 AI Agent（巡检/根因/复盘/成本等六场景）的数据面总体设计，确定 MCP 拆分、
> bingops-mcp 工具清单与平台侧前置改造项。
> 场景 3（巡检日报）的执行细节见配套 `inspection-report-design.md`。
> 现有体系参考：`cmdb-design.md`、`task-system-design.md`、`ticket-system.md`。

---

## 1. 定位与边界

**Agent 编排层独立于 bingops 平台**（Dify / n8n / 自研 loop 均可），平台只提供数据面：
以 MCP 协议暴露工具，供编排层调用。平台本身不引入 LLM 依赖。

边界纪律：

1. **bingops-mcp 是目录与 join 层，外部 MCP 是事实层**——告警 IP、Prometheus target、
   git 仓库等外部标识，必须经 CMDB（provider_id / IP / cluster+namespace / app 标签 / repo_url）
   翻译成业务含义后才有价值；
2. agent 对平台**默认只读**，写操作白名单化（§4.2）；
3. 编排层、模型选型、prompt 策略不在本文范围。

### 1.1 六场景清单

| # | 场景 | 一句话定义 |
|---|---|---|
| 1 | 告警根因预分析 | 夜莺告警 → CMDB 富化 → 近期变更/拓扑/指标/日志 → 候选根因排序+证据链 |
| 2 | 故障日志自动摘要 | 故障时间窗内，按应用聚合错误日志 → LLM 聚类摘要 |
| 3 | 自动生成巡检日报 | 定时聚合指标水位+告警统计+变更摘要 → 日报推送飞书 |
| 4 | 故障复盘初稿生成 | incident 工单 → 全量时间线证据 → 按模板成稿 |
| 5 | 上线变更风险预检 | dispatch 前置钩子：变更上下文+git diff+当前告警水位 → 风险评分 |
| 6 | 云成本分析 | 账单入 CH → CMDB 分摊 + 利用率闲置识别 → 月报与优化建议 |

---

## 2. 现状数据盘点

### 2.1 平台已有

| 数据域 | 内容 | 关键实现 |
|---|---|---|
| CMDB | 三云 + K8s 全量资源（动态 JSONB fields）、belongs_to/relates_to 拓扑、标签、业务应用（负责人/团队/repo_url/pipelines）、应用-资源关联 | `cmdb_resources` GIN 索引已建 |
| 变更 | 工单全生命周期（risk_level/审批/服务目录/处理组/值班）、CMDB 变更审计、封禁窗口 | `change_context` 已聚合活跃工单+近期变更+冻结 |
| 执行 | jobs 作业记录（**code_ref = git tag 快照，dispatch 强制要求**）、runbook、Kafka 事件链路 | 每次执行自带 tag，是变更影响分析的天然 join key |
| 采集出口 | Prometheus/vmagent HTTP SD（CMDB 主机快照） | 采集 target 标识由平台控制 |
| 工单类型 | `general / request / change / incident` | incident 类型可直接承载故障复盘 |

### 2.2 平台缺失（必须外部补）

| 缺失 | 数据源 | 影响场景 |
|---|---|---|
| 指标时序 | Prometheus / VictoriaMetrics | 1、3、4、5、6 |
| 告警事件 | 夜莺（n9e OpenAPI） | 1、3、4、5 |
| 日志 | ClickHouse（前提：日志已入 CH） | 2、4 |
| 账单费用 | 云厂商 Billing（阿里云 BSS / AWS CE / GCP Export） | 6 |
| git 提交事实 | GitLab / GitHub API | 1、4、5 |

### 2.3 六场景支撑判定

| 场景 | 支撑度 | 硬缺口 |
|---|---|---|
| 5 上线变更风险预检 | **高**（change_context 底座已存在） | 仅缺 git diff 与当前告警/水位 |
| 3 巡检日报 | 中高 | 仅缺外部指标/告警 |
| 1 告警根因预分析 | 中 | 标识对齐 + 指标/告警全缺 |
| 2 故障日志摘要 | 中 | 日志是否已入 CH 待确认 |
| 4 故障复盘初稿 | 中 | 依赖场景 1/2/3 的工具链先落地 |
| 6 云成本分析 | **低** | 账单管道完全不存在，需先建 |

---

## 3. MCP 体系规划（共 6 个）

| MCP | 职责 | 选型 | 覆盖场景 | 优先级 |
|---|---|---|---|---|
| **bingops-mcp** | CMDB/工单/变更上下文/jobs 查询——目录与 join 层 | 自建（FastMCP 挂载现有 FastAPI） | 6/6 | **P0** |
| **夜莺 MCP** | 告警事件、规则、静默 | 社区现成（n9e OpenAPI） | 1、3、4、5 | **P0** |
| **Prometheus/VM MCP** | PromQL 即时/区间查询、targets 状态 | 社区现成 | 1、3、4、5、6 | **P0** |
| **ClickHouse MCP** | 日志/事件查询 + 账单数据查询（只读+强制 LIMIT） | 社区现成 | 2、4、6 | P1 |
| **代码托管 MCP** | tag/branch、两 tag compare、MR、CI pipeline 状态 | GitLab 社区 MCP / GitHub 官方 MCP，**不自研** | 1、4、5 | P1 |
| Grafana MCP | 看板检索 + **annotations**（变更时间点标记） | 社区现成，可选 | 4、5 增强 | P2 |

关键决策：

1. **账单不按云厂商拆 MCP**——每日定时把三家账单拉进 ClickHouse，用 CH MCP 一并查询，
   省两个 server，且账单可与资源清单在 SQL 层 join；
2. 若 CI 用 GitLab，代码托管 MCP 同时覆盖仓库与 pipeline，无需再接 Jenkins MCP；
3. agent 调用链（以根因分析为例）：
   夜莺事件 → `find_app_by_resource` / IP 反查 → `get_change_context` → `list_job_executions`
   取 code_ref → git MCP compare 上一 tag..本次 tag → `get_topology` 展开上下游 →
   Prometheus/CH 取证 → 候选根因排序。

---

## 4. bingops-mcp 设计

### 4.1 接入方式

- 官方 `mcp` python-sdk（FastMCP）挂载进**现有 FastAPI 进程**，streamable-http 暴露 `/mcp`；
- 复用现有 `get_db_session` 会话依赖、RBAC 与统一响应/异常体系，不新增服务、不改分层。

### 4.2 设计原则

1. **暴露复合工具而非裸 CRUD**——一次调用返回聚合结果（如 app 详情+资源清单）；
2. **token 控制**——输出字段白名单裁剪、列表默认 limit=20（上限 100）、时间统一 ISO8601；
3. **只读为主**——写操作白名单且可整体关闭（`BINGOPS_MCP_WRITE_ENABLED`）；
4. **LLM 友好错误**——结构化 error + hint（提示 agent 下一步可尝试的工具）；
5. ID 与名称**双字段返回**（LLM 需要可读名，程序需要稳定 ID）。

> 工具编码落地规范（命名、description 三段式、输出契约、安全红线）见
> `.qoder/skills/bingops-mcp-tools/SKILL.md`，本节只定架构，编码时以 skill 为准。

### 4.3 工具清单

#### A. 应用与资源目录（6 场景共用）

| 工具 | 底层 API | 说明 | 场景 |
|---|---|---|---|
| `list_business_apps` | GET /api/v1/cmdb/apps | 按 team/关键词过滤 | 3、6 |
| `get_app_overview` | /apps/{id} + /apps/{id}/resources 复合 | 详情+repo_url+pipelines+资源清单 | 1、2、3、6 |
| `find_app_by_resource` | GET /apps/by-resource/{id} | 实例→应用反查（根因分析入口） | 1、2 |
| `search_resources` | GET /cmdb/resources | provider/model/status/region 过滤 + keyword 名称模糊 + **field_value 动态字段值精确检索（IP/连接地址/实例 ID，命中返回 matched_fields）** | 全部 |
| `search_assets` | GET /cmdb/search | **跨域聚合**：应用+资源一次命中（工作台搜索同源，exact 开关） | 全部 |
| `get_models_overview` | GET /cmdb/models/overview | **平台结构总览**：分类→模型→存活资源计数 | 全部 |
| `get_resource_detail` | GET /cmdb/resources/{id} | 含动态 fields | 1、5 |

#### B. 拓扑（场景 1、4）

| 工具 | 底层 API | 说明 |
|---|---|---|
| `get_topology` | GET /resources/{id}/topology | 已存在；包装时 depth 上限 ≤3 跳防爆炸 |
| `list_relations` | parents/children/relations-from/to | 薄包装，topology 不够时用 |

#### C. 变更与执行（场景 1、3、4、5）

| 工具 | 底层 API | 说明 |
|---|---|---|
| `get_change_context` | ticket_service.change_context | 已存在：活跃工单+近期变更+冻结一次拿全，场景 5 核心 |
| `list_freezes` | 冻结窗口接口 | 判断当前是否封禁期 |
| `list_tickets` | GET /tickets | type/status/时间窗过滤；复盘取 incident |
| `get_ticket_timeline` | 工单详情+流转+评论 | 复盘时间线素材 |
| `list_job_executions` | GET /jobs | 含 code_ref/target_resource_ids，时间窗过滤；日报"昨日变更"段来源 |

#### D. 统计与写操作

| 工具 | 说明 |
|---|---|
| `get_ticket_stats` / `get_resource_stats` | 现有 /stats 端点包装 |
| `add_ticket_comment` | 唯一默认开放的写：预检结论/日报落档到工单 |
| `create_ticket` | 仅 incident/request；默认关闭，开启时需人工确认机制 |

### 4.4 安全模型

- 新增 `ai_agent` 角色：权限码仅含各域 `list`/`read`（+可选 `ticket:comment`），MCP token 绑定该角色；
- 外部 MCP（CH/git/夜莺/Prometheus）一律使用**独立只读账号**；
- ClickHouse MCP 强制 LIMIT 与超时；敏感字段（RDS 连接串等）在工具层脱敏后再返回。

### 4.5 平台侧前置改造（3 项）

| # | 改造 | 原因 | 改动点 |
|---|---|---|---|
| 1 | ~~`search_resources` 暴露 `fields.*` JSONB 查询参数~~ **✅ 已完成（2026-09-22）** | 落地为通用 `field_value` 参数（而非逐字段命名参数）：`fields::text` 带双引号边界等值匹配，全动态字段生效（IP/连接地址/实例 ID/嵌套数组），无需枚举字段名；命中附带 matched_fields 告知 agent 命中字段。`fields::text` 无法用 GIN 索引（全扫），万级可接受 | `api/v1/cmdb/resources.py` + `resource_repo._json_value_like` + `mcp/tools/resources.py` |
| 2 | repo_url 规范化（约定 `https://git.example.com/group/project.git` 格式，或入库解析出 git_host/project_path 结构化字段） | 自由字符串 agent 无法稳定解析 git host 与项目路径 | `cmdb_business_apps` 字段约定 + schemas 校验 |
| 3 | `ai_agent` 只读角色 + 权限码种子 | MCP 鉴权载体 | `scripts/init_data.py` |

---

## 5. 落地路线图

| 阶段 | 内容 | 依赖 |
|---|---|---|
| P0 | bingops-mcp（A/C 组工具 + add_ticket_comment）+ 夜莺 MCP + Prometheus MCP | §4.5 改造 1、3 |
| P1 | 场景 3 巡检日报上线（见 `inspection-report-design.md`）→ 场景 5 变更风险预检（dispatch 前置钩子，结果写回工单评论） | P0 |
| P2 | 场景 1 根因预分析 → 场景 2 日志摘要（先确认日志入 CH）→ 场景 4 复盘初稿 | P0+P1 |
| P3 | 账单入 CH 管道 → 场景 6 成本分析；Grafana MCP（annotations 增强复盘/预检） | 独立 |

落地顺序依据：前两个场景不依赖账单/日志数据现状；场景 4 是 1+2+3 的组合输出，放最后。
