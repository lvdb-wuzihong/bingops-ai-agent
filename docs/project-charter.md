# 项目定位与架构边界（project charter）

> 本文是本仓库的最高层定位声明。与 `bingops` 仓库 `docs/ai-agent-mcp-design.md` 配套阅读。

---

## 1. 项目定位

本仓库是 **BingOps Agent 编排层**（agent runtime），不是巡检专用工具：

- 巡检日报是**第一个 workflow**（历史原因项目名保留 `inspection-agent`，不改名不重构）；
- 六场景（巡检日报 / 变更风险预检 / 根因预分析 / 日志摘要 / 复盘初稿 / 云成本分析）
  按"一个 runtime + N 个 workflow 定义"演进：新增场景 = 新增 workflow 模块，不动运行时；
- 数据面通过 MCP 连接 `bingops-mcp`（平台内）与外部 MCP（n9e / prometheus / gitlab / clickhouse）。

### 1.1 事件拓扑决策（复用同一飞书应用）

飞书事件**唯一入口在平台**，编排层不直连飞书：

- 排他性原因：事件订阅方式（webhook / WS 长连接）是**应用级二选一**，平台已占用 webhook
  （`/api/v1/integrations/feishu/events`，验签解密与 CHALLENGE 握手就位）；且飞书 WS 多连接
  为**竞争消费**（事件随机推给一个连接），两端同连会互相抢事件；
- 因此 `bot/feishu.py` 的 WS 客户端**退场**；`bot/app.py` 新增 `POST /feishu/events`
  接收平台转发（平台已验签解密，编排层不处理飞书协议，也不持有 app_secret）；
- 出站回复统一走平台 `feishu_bot`（`send_feishu_message` 写工具 / 平台 API），
  编排层与飞书完全解耦——以后飞书换事件模式只动平台；
- `bot/agent.py`、`session.py`、`tools.py`（agent loop / 会话 / 白名单）零改动，仅桥接方式切换。
- **分流语义（实现时定）**：`im.message.receive_v1` 已有既有消费方（发消息 → 建单表单卡片流程），
  agent 接入后存在同一事件两个消费方——需按**命令前缀或会话状态**分流（如 `/ai` 前缀或
  处于 agent 会话中的消息转编排层，其余走建单流程），在平台转发分支处判定，避免两端抢答。

## 2. 双轨制数据路径（核心防漂移规则）

同一份业务数据存在两条取数路径，**分工固定，禁止交叉复制**：

| 路径 | 使用者 | 理由 |
|---|---|---|
| `sources/` 直连平台 REST | `pipeline/`（确定性规则流水线，如巡检判定） | 低延迟、无 LLM 选工具开销、行为可预测 |
| `mcpclient` + MCP 工具白名单 | `bot/`（LLM 对话 agent） | LLM 自主选工具；白名单收窄（`bot/tools.py`，只读、永不扩写） |

约束条款：

1. **新数据能力优先加在平台 service 层**——平台 REST 与 MCP 工具同源于 service，
   两侧自动受益；禁止只在编排层单边实现后反推平台；
2. 禁止 pipeline 复制 MCP 工具的聚合逻辑（如变更上下文聚合以平台 `change_context` 为唯一事实源）；
3. bot 工具白名单以**部署版本 `tools/list` 交集**为准，白名单多列未实现工具无害但应注释标记。

## 3. 文档权威性

`bingops` 仓库 `docs/` 为设计文档**唯一权威**；本仓库 `docs/` 下的
`ai-agent-mcp-design.md`、`inspection-report-design.md` 为**快照拷贝**（不回写）。
设计变更一律改权威版，本仓库只落编排层实现细节（workflow 定义、配置契约）。

## 4. 六场景分期路线图

| 期 | 场景 | 形态 | 现有基础 | 增量 |
|---|---|---|---|---|
| P0 | 巡检日报 | cron，规则版→叙事版 | `pipeline/` + `report/` + `sources/` + OSS | narrate 接 llm 收尾 |
| P1 | 上线变更风险预检 | **同步 HTTP 端点**（平台 dispatch 前置钩子调用），纯规则评分起步 | `get_change_context`/`list_freezes`/n9e/prom 数据源齐备 | 端点 + 评分规则 + 结果写回工单评论 |
| P2 | 告警根因预分析 | bot 对话 / 事件触发 | MCP 白名单工具全齐（bingops+n9e+prometheus+gitlab） | rca workflow 编排 + prompt；前置：事件转发路由 |
| P3 | 故障复盘初稿 | incident 工单触发 | 复用 P2 + `get_ticket_timeline` | retro workflow + 模板装配 |
| P4 | 日志摘要 + 云成本分析 | 事件/月度 cron | —— | 前置：日志入 CH 确认 + 账单入 CH 管道，新增 `sources/clickhouse.py` |

执行模式注意：P1 是**同步秒级**场景（嵌 dispatch 流程），与 bot 的异步会话是两种执行模式，共享 runtime 但入口不同。

## 5. 与平台侧的待办联动

**已排期（下一步，P2 对话场景前置）**：

- 平台：`feishu_event_service` 的 `im.message.receive_v1` 分支增加**转发编排层路由**
  （新配置 `BINGOPS_AGENT_CALLBACK_URL`；fire-and-forget，失败只记日志不阻断）；
- 平台：`send_feishu_message` MCP 写工具 + `feishu_bot` 群发送（chat_id）——
  日报推送与 agent 回复的统一出站通道；
- 编排层：`bot/app.py` 接收端点 + `bot/feishu.py` WS 退场（与平台转发同步切换）。

按场景需要穿插：

- B 组拓扑工具（`get_topology` / `list_relations`）—— P2 根因分析前置；
- `search_resources` 暴露 `fields.*` JSONB 查询 —— 按告警 IP 反查资源；
- `ai_agent` 只读角色种子 —— bot 上生产前必须（MCP 鉴权依赖此角色）；
- `get_ticket_stats` / `get_resource_stats` —— bot 白名单已注释，平台补齐后启用。
