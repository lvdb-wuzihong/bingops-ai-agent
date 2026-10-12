"""LangGraph 图编排层（多 agent 化改造）。

两张图：
- graph/bot_graph.py：bot ReAct 对话图（引擎替换自研 loop，白名单闸门与预算语义不变）；
- graph/report_graph.py：日报八步链路图（节点=现有确定性函数薄包装，行为等价）。

红线不因框架迁移松动（SKILL 2/3/8）：bot 工具执行仍走 mcpclient（闸门在图工具节点内，
schemas 只来自白名单交集 + 技能收窄）；report 图节点零数字产出，取数仍走 sources/ REST
（MCP 禁入 pipeline）。状态定义见 graph/state.py。
"""
