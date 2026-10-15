# 项目定位与架构边界（project charter）

> 本文是本仓库的最高层定位声明。与 `bingops` 仓库 `docs/ai-agent-mcp-design.md` 配套阅读。

---

## 1. 项目定位

本仓库是 **BingOps 多 agent 体系中的巡检专项 agent**（2026-10 架构升级：
由“编排层 + N 个 workflow”收窄为按场景拆分的多 agent，参考 LangGraph 多 agent 架构选型）：

- 本仓库只做巡检域：巡检日报（cron）+ 巡检 bot（飞书对话）；项目名保留 `inspection-agent` 不改；
- 六场景按“多场景 = 多专项 agent”演进：变更风险预检 / 根因预分析 / 故障复盘 / 日志摘要 /
  云成本分析各自独立成专项 agent 服务，不在本仓库实现（归属映射与同构骨架见 §1.2）；
- agent 编排统一走 LangChain/LangGraph（`graph/` 层：日报八步图 + bot ReAct 图，
  节点=现有确定性函数薄包装，LLM 只在节点内推理）；
- 数据面双轨制不变（§2）：pipeline 取数直连 REST；bot 经 MCP 白名单对接
  `bingops-mcp`（平台内）与外部 MCP（prometheus / gitlab 等）。

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
  （2026-10 注：agent loop 已迁 LangGraph 引擎 `graph/bot_graph.py`；对外接口、会话与白名单机制不变）
- **分流语义（实现时定）**：`im.message.receive_v1` 已有既有消费方（发消息 → 建单表单卡片流程），
  agent 接入后存在同一事件两个消费方——需按**命令前缀或会话状态**分流（如 `/ai` 前缀或
  处于 agent 会话中的消息转编排层，其余走建单流程），在平台转发分支处判定，避免两端抢答。

### 1.2 多 agent 协同模式（2026-10 拆分决策）

六场景 → 专项 agent 的归属映射：

| 场景 | 归属 agent | 入口 |
|---|---|---|
| 巡检日报 | 本仓库 | 平台/外部 cron 每日触发 |
| 巡检 bot 对话 | 本仓库 | 平台 `POST /feishu/events` 事件转发 |
| 变更风险预检 | 预检专项 agent（待建） | 平台 dispatch 前置钩子（同步 HTTP） |
| 根因预分析 | RCA 专项 agent（待建） | 平台事件转发（告警/bot） |
| 故障复盘初稿 | RCA 专项 agent（待建） | incident 工单触发 |
| 日志摘要 / 云成本分析 | 观测专项 agent（待建） | cron / 事件 |

协同规则：

- agent 间协同 = **入口确定性分流**（平台事件路由 / cron / HTTP dispatch 按场景路由到对应
  agent 服务），不引入 LLM supervisor——调度可复现可回归、成本可控（与参考文章
  “标准化流水线任务选主从架构”结论一致）；
- 各专项 agent 采用**同构骨架**：LLM 适配层（langchain-openai，OpenAI 兼容端点）+
  LangGraph 图编排 + 只读工具白名单 + 平台统一出站（`send_feishu_message`）；
- LLM 调度重评条件：出现真实协商需求（如预检 block 后多方讨论改方案）、凭据隔离要求
  拆独立 Deployment、或某场景必须异构推理端点时再评估。

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
3. bot 工具白名单以**部署版本 `tools/list` 交集**为准，白名单多列未实现工具无害但应注释标记；
4. bot 触发的**实时快检**（`pipeline/quickcheck.py`，quick_inspect 本地工具）属于
   pipeline 数据路径：取数走 sources/ REST、判定复用规则引擎（红线 3），产物 JSON 由
   LLM 逐字引用；bot 进程因此也需要 platform/external 端点与凭据可达
   （部署约束，见 config/inspection.example.yaml bot 段注释）；
   日报产物读取（latest_report 本地工具）走 OSS 只读（oss.read_report）。

## 3. 文档权威性

`bingops` 仓库 `docs/` 为设计文档**唯一权威**；本仓库 `docs/` 下的
`ai-agent-mcp-design.md`、`inspection-report-design.md` 为**快照拷贝**（不回写）。
设计变更一律改权威版，本仓库只落编排层实现细节（workflow 定义、配置契约）。

## 4. 场景分期与归属

六场景 → 专项 agent 的归属映射见 §1.2。本仓库（巡检专项 agent）路线：

| 期 | 内容 | 状态 |
|---|---|---|
| P0 | 巡检日报（规则版 → LLM 叙事版） | 已实现；2026-10 完成 LangGraph 图化（`graph/report_graph.py`） |
| P1.5 | 双通道输出（飞书短摘要 + OSS 完整 HTML） | 已实现 |
| P2 | 巡检 bot 对话（MCP 只读白名单工具 + 运行时技能） | 已实现；2026-10 完成 LangGraph 图化（`graph/bot_graph.py`） |

P1 变更风险预检 / P3 故障复盘 / P4 日志与成本分析 → 各自专项 agent 仓库排期，不在本仓库。
执行模式注意：预检是**同步秒级**场景（嵌 dispatch 流程），与 bot 的异步会话是两种执行模式——
专项 agent 拆分后两者天然隔离（不同服务、不同入口）。

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
