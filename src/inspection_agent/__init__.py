"""bingops 巡检日报 Agent（场景 3）P0 编排层。

架构：编排层独立于 bingops 平台，平台以 MCP 提供数据面（docs/ai-agent-mcp-design.md）；
执行细节见 docs/inspection-report-design.md；开发约束见 .qoder/skills/inspection-agent-dev。
P0 零 LLM：规则引擎先算数，报告按固定模板拼装。
"""

__version__ = "0.1.0"
