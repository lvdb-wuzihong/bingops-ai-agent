"""bot 单测：agent loop 编排、只读白名单拒绝、迭代上限、结果截断、会话裁剪。"""

from __future__ import annotations

from types import SimpleNamespace

from inspection_agent.bot.agent import ChatAgent
from inspection_agent.bot.session import SessionStore
from inspection_agent.bot.tools import DEFAULT_ALLOWLIST, effective_allowlist, load_tool_schemas
from inspection_agent.config import BotConfig

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


# ---------------------------------------------------------------- agent loop


async def test_loop_executes_tool_then_answers():
    llm = FakeLLM(
        [
            {"tool_calls": [tool_call("bingops__list_business_apps", '{"team": "t1"}')]},
            {"content": "共 3 个应用"},
        ]
    )
    agent = ChatAgent(FakePool({"list_business_apps": {"items": [1, 2, 3]}}), llm, [SCHEMA])
    answer = await agent.answer("chat-1", "有哪些应用？")
    assert answer == "共 3 个应用"
    tool_messages = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_messages and "items" in tool_messages[0]["content"]


async def test_allowlist_rejects_write_tool():
    llm = FakeLLM(
        [
            {"tool_calls": [tool_call("bingops__add_ticket_comment", "{}")]},
            {"content": "已拒绝执行写操作"},
        ]
    )
    agent = ChatAgent(FakePool({}), llm, [SCHEMA])
    answer = await agent.answer("c", "帮我写个工单评论")
    assert answer == "已拒绝执行写操作"
    tool_messages = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert "不在只读白名单" in tool_messages[0]["content"]


async def test_max_iterations_forces_summary_without_tools():
    llm = FakeLLM(
        [
            *([{"tool_calls": [tool_call("bingops__list_business_apps")]}] * 2),
            {"content": "基于已获取信息回答"},
        ]
    )
    agent = ChatAgent(
        FakePool({"list_business_apps": {}}), llm, [SCHEMA], max_iterations=2
    )
    answer = await agent.answer("c", "查查")
    assert answer == "基于已获取信息回答"
    assert llm.calls[-1]["tools"] is None  # 收尾轮不再提供工具
    assert len(llm.calls) == 3


async def test_tool_result_truncated():
    llm = FakeLLM(
        [
            {"tool_calls": [tool_call("bingops__list_business_apps")]},
            {"content": "ok"},
        ]
    )
    agent = ChatAgent(
        FakePool({"list_business_apps": {"blob": "x" * 500}}),
        llm,
        [SCHEMA],
        tool_result_max_chars=50,
    )
    await agent.answer("c", "查")
    tool_messages = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_messages[0]["content"].endswith("字符）")


# ---------------------------------------------------------------- 会话与白名单


def test_session_trim_and_isolation():
    store = SessionStore(max_history=4)
    for i in range(5):
        store.append_user("c1", f"u{i}")
        store.append_assistant("c1", f"a{i}")
    assert len(store.history("c1")) == 4
    assert store.history("c2") == []


def test_effective_allowlist_narrows_without_expanding():
    config = BotConfig(
        tool_allowlist={
            "bingops": ["list_business_apps", "add_ticket_comment"],  # 写工具必须被交集掉
            "prometheus": ["query_instant"],
        }
    )
    allow = effective_allowlist(config)
    assert allow["bingops"] == {"list_business_apps"}
    assert allow["prometheus"] == {"query_instant"}  # 显式配置只可收窄
    assert allow["gitlab"] == DEFAULT_ALLOWLIST["gitlab"]  # 未提及的 server 保持默认


async def test_load_tool_schemas_filters_writes():
    class FakeSession:
        def __init__(self, tools: list[str]) -> None:
            self._tools = tools

        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(name=name, description="d", inputSchema={"type": "object"})
                    for name in self._tools
                ]
            )

    class FakeDiscoveryPool:
        def __init__(self, per_server: dict[str, list[str]]) -> None:
            self._per = per_server

        def connections(self):
            return {
                server: SimpleNamespace(session=FakeSession(tools))
                for server, tools in self._per.items()
            }

    pool = FakeDiscoveryPool(
        {
            "bingops": ["list_business_apps", "add_ticket_comment", "create_ticket"],
            "prometheus": ["query_instant", "execute_write"],
        }
    )
    schemas = await load_tool_schemas(pool, BotConfig())
    names = {s["function"]["name"] for s in schemas}
    assert "bingops__list_business_apps" in names
    assert "bingops__add_ticket_comment" not in names
    assert "bingops__create_ticket" not in names
    assert "prometheus__query_instant" in names
    assert "prometheus__execute_write" not in names


async def test_load_tool_schemas_with_skill_filter():
    """技能 tool_filter 非空时：未声明的白名单内工具不暴露（并集语义见 skillregistry）。"""

    class FakeSession:
        def __init__(self, tools: list[str]) -> None:
            self._tools = tools

        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(name=name, description="d", inputSchema={"type": "object"})
                    for name in self._tools
                ]
            )

    class FakeDiscoveryPool:
        def __init__(self, per_server: dict[str, list[str]]) -> None:
            self._per = per_server

        def connections(self):
            return {
                server: SimpleNamespace(session=FakeSession(tools))
                for server, tools in self._per.items()
            }

    pool = FakeDiscoveryPool({"bingops": ["list_business_apps", "list_tickets"]})
    schemas = await load_tool_schemas(
        pool, BotConfig(), tool_filter=frozenset({"list_business_apps"})
    )
    assert [s["function"]["name"] for s in schemas] == ["bingops__list_business_apps"]

    # tool_filter=None → 不收窄，全白名单照常暴露
    schemas = await load_tool_schemas(pool, BotConfig())
    assert {s["function"]["name"] for s in schemas} == {
        "bingops__list_business_apps",
        "bingops__list_tickets",
    }

