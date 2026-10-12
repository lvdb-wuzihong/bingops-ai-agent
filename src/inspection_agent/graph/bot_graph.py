"""bot ReAct 对话图（LangGraph StateGraph；引擎替换自研 loop，闸门与预算语义不变）。

图结构：
  START → agent（LLM 推理）─ 无 tool_calls → END（answer 就绪）
        └ 有 tool_calls → tools（白名单闸门 + MCPServerPool 执行 + 截断）
  tools → 未达上限回 agent ／ 达上限 → finalize（不再提供工具，强制基于已获取信息作答）→ END

红线不因框架迁移松动（SKILL 2/6）：
- 工具 schemas 只来自白名单交集 + 技能收窄的装配产物（bot/tools.py + skillregistry）；
- 白名单外调用在工具节点内硬拒绝；工具执行走 mcpclient（bot 对接 bingops/prometheus
  等多实例 MCP，多实例分组别名由装配层解析），框架层不直连任何工具实现；
- 迭代上限（max_iterations）与结果截断（tool_result_max_chars）语义与旧 loop 一致。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

from ..mcpclient import MCPError, MCPServerPool
from .state import BotState

logger = logging.getLogger(__name__)

FALLBACK_NO_CONTENT = "（无内容）"
FALLBACK_LIMIT = "本次查询复杂度过高，请缩小问题范围后重试。"
FALLBACK_LLM_DOWN = "当前 LLM 服务不可用，请稍后重试。（本次未查询任何数据，不作猜测）"
LIMIT_NOTICE = (
    "（系统提示）已达工具调用次数上限，请基于已获取的信息直接回答，"
    "未查询到的部分明确说明。"
)


def build_bot_graph(
    *,
    llm: Any,
    pool: MCPServerPool,
    tool_schemas: list[dict[str, Any]],
    max_iterations: int,
    tool_result_max_chars: int,
) -> Any:
    """组装 bot ReAct 图；llm 为 chat(messages, tools) 接口（LLMClient / 测试 Fake）。"""
    max_iterations = max(1, max_iterations)
    allowed_names = {s["function"]["name"] for s in tool_schemas}

    async def agent_node(state: BotState) -> dict[str, Any]:
        message = await llm.chat(state["messages"], tools=tool_schemas)
        update: dict[str, Any] = {"messages": [message]}
        if not (message.get("tool_calls") or []):
            update["answer"] = str(message.get("content") or "").strip() or FALLBACK_NO_CONTENT
        return update

    async def tools_node(state: BotState) -> dict[str, Any]:
        last = state["messages"][-1]
        results = [
            {
                "role": "tool",
                "tool_call_id": str(call.get("id") or ""),
                "content": await _execute(call, pool, allowed_names, tool_result_max_chars),
            }
            for call in (last.get("tool_calls") or [])
        ]
        return {"messages": results, "tool_rounds": state["tool_rounds"] + 1}

    async def finalize_node(state: BotState) -> dict[str, Any]:
        """达迭代上限：注入系统提示并撤走工具，要求基于已获取信息作答（优雅收尾）。"""
        message = await llm.chat(
            [*state["messages"], {"role": "user", "content": LIMIT_NOTICE}]
        )
        return {"answer": str(message.get("content") or "").strip() or FALLBACK_LIMIT}

    def route_after_agent(state: BotState) -> str:
        return "tools" if state["messages"][-1].get("tool_calls") else END

    def route_after_tools(state: BotState) -> str:
        return "agent" if state["tool_rounds"] < max_iterations else "finalize"

    graph = StateGraph(BotState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("finalize", finalize_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent, {"tools": "tools", END: END})
    graph.add_conditional_edges(
        "tools", route_after_tools, {"agent": "agent", "finalize": "finalize"}
    )
    graph.add_edge("finalize", END)
    return graph.compile()


async def _execute(
    call: dict[str, Any],
    pool: MCPServerPool,
    allowed_names: set[str],
    result_max_chars: int,
) -> str:
    """执行单个 tool_call：白名单拒绝 / 参数容错 / MCP 失败转提示 / 超长截断。

    原 ChatAgent._execute 语义原样迁移（闸门在工具节点内，红线 2）。
    """
    function = call.get("function") or {}
    name = str(function.get("name") or "")
    raw_args = str(function.get("arguments") or "")
    if name not in allowed_names:
        logger.warning("bot 拒绝非白名单工具调用: %s", name)
        return f"错误：工具 {name} 不在只读白名单内，已拒绝执行。"
    try:
        arguments = json.loads(raw_args) if raw_args.strip() else {}
    except ValueError:
        return "错误：工具参数不是合法 JSON。"
    if not isinstance(arguments, dict):
        return "错误：工具参数必须是 JSON 对象。"
    server, sep, bare = name.partition("__")
    if not sep or not bare:
        return f"错误：工具名 {name} 缺少 server 前缀。"
    try:
        conn = pool[server]
        data = await conn.call_tool(bare, arguments)
    except MCPError as exc:
        return f"工具执行失败（数据不可用，请勿猜测）：{exc}"
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) > result_max_chars:
        text = text[:result_max_chars] + f"\n…（结果已截断，原始长度 {len(text)} 字符）"
    return text
