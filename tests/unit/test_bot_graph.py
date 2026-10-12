"""bot_graph 单测：LangGraph 图编排路径（工具轮转 / 达限收尾 / 白名单闸门 / LLM 断供）。

ChatAgent 层行为回归在 test_agent.py（mock 点不变）；本文件直接验证图的路由与状态流。
"""

from __future__ import annotations

from typing import Any

from inspection_agent.bot.agent import ChatAgent
from inspection_agent.bot.session import SessionStore
from inspection_agent.graph.bot_graph import build_bot_graph
from inspection_agent.llm.client import LLMError

SCHEMA = {
    "type": "function",
    "function": {"name": "bingops__list_business_apps", "parameters": {"type": "object"}},
}


class FakeLLM:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def chat(self, messages, tools=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        return self._responses.pop(0)


class BoomLLM:
    async def chat(self, messages, tools=None):
        raise LLMError("llm down")


class FakeConn:
    def __init__(self, results: dict) -> None:
        self._results = results

    async def call_tool(self, name, arguments, read_timeout_sec=None):
        return self._results[name]


class FakePool:
    def __init__(self, results: dict) -> None:
        self._results = results

    def __getitem__(self, name):
        return FakeConn(self._results)


def tool_call(name: str, arguments: str = "{}") -> dict:
    return {"id": "c1", "function": {"name": name, "arguments": arguments}}


def make_graph(responses: list[dict], results: dict, max_iterations: int = 8):
    llm = FakeLLM(responses)
    graph = build_bot_graph(
        llm=llm,
        pool=FakePool(results),
        tool_schemas=[SCHEMA],
        max_iterations=max_iterations,
        tool_result_max_chars=2000,
    )
    return graph, llm


def initial_state() -> dict[str, Any]:
    return {
        "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}],
        "tool_rounds": 0,
    }


async def test_graph_tool_round_then_answer():
    graph, _ = make_graph(
        [
            {"tool_calls": [tool_call("bingops__list_business_apps", '{"team": "t1"}')]},
            {"content": "共 3 个应用"},
        ],
        {"list_business_apps": {"items": [1, 2, 3]}},
    )
    result = await graph.ainvoke(initial_state())
    assert result["answer"] == "共 3 个应用"
    tool_messages = [m for m in result["messages"] if m.get("role") == "tool"]
    assert len(tool_messages) == 1 and "items" in tool_messages[0]["content"]


async def test_graph_finalize_without_tools_after_limit():
    graph, llm = make_graph(
        [
            {"tool_calls": [tool_call("bingops__list_business_apps")]},
            {"tool_calls": [tool_call("bingops__list_business_apps")]},
            {"content": "收尾回答"},
        ],
        {"list_business_apps": {}},
        max_iterations=2,
    )
    result = await graph.ainvoke(initial_state())
    assert result["answer"] == "收尾回答"
    assert llm.calls[-1]["tools"] is None  # 收尾轮不再提供工具
    assert len(llm.calls) == 3


async def test_graph_rejects_non_allowlist_tool():
    graph, _ = make_graph(
        [
            {"tool_calls": [tool_call("bingops__add_ticket_comment")]},
            {"content": "已拒绝"},
        ],
        {},
    )
    result = await graph.ainvoke(initial_state())
    tool_messages = [m for m in result["messages"] if m.get("role") == "tool"]
    assert "不在只读白名单" in tool_messages[0]["content"]
    assert result["answer"] == "已拒绝"


async def test_chatagent_llm_down_returns_fallback_without_history_write():
    agent = ChatAgent(FakePool({}), BoomLLM(), [SCHEMA], session=SessionStore())
    answer = await agent.answer("c", "hi")
    assert answer.startswith("当前 LLM 服务不可用")
    # 断供路径不写 assistant 回复进会话历史（与旧 loop 语义一致）
    history = agent._session.history("c")
    assert len(history) == 1 and history[0]["role"] == "user"
