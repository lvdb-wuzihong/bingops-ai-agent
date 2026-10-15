"""bot 大脑（LangGraph ReAct 图引擎；巡检工作流定位 + 数据域下钻）。

红线（SKILL 2/3/6）：
- 工具只读白名单，白名单外调用直接拒绝并回提示（闸门在 graph/bot_graph.py 工具节点内；
  本地工具 quick_inspect/latest_report 与 MCP 工具同闸门）;
- 回答数字与异常判定只能来自工具返回，工具失败回"数据不可用"而非猜测；
  quick_inspect/latest_report 返回的 rule/severity 是规则引擎结论，LLM 只逐字引用；
- 迭代上限（默认 8 轮）与单次工具结果截断；LLM 断供回复可用性提示。
会话历史仍由 SessionStore 持有（图只处理单轮 invoke 的消息流，不接 checkpointer）。
"""

from __future__ import annotations

import logging
from typing import Any

from ..graph.bot_graph import FALLBACK_LLM_DOWN, FALLBACK_NO_CONTENT, build_bot_graph
from ..graph.state import BotState
from ..llm.client import LLMClient, LLMError
from ..mcpclient import MCPServerPool
from .session import SessionStore

logger = logging.getLogger(__name__)

BOT_SYSTEM_PROMPT = (
    "你是 bingops 巡检助手，围绕巡检工作流服务：\n"
    "- 实时快检：quick_inspect(app, hours?)——按检查矩阵做规则判定，返回异常/风险/证据，"
    "适合“XX 现在正常吗/有什么问题”；\n"
    "- 日报解读：latest_report(date?)——查巡检日报的异常与趋势结论；\n"
    "- 下钻排查：只读数据域工具（CMDB 应用与资源、指标、变更与工单、Git 仓库）。\n"
    "硬性规则：\n"
    "1. 回答中的所有数字与异常判定必须来自工具返回结果，禁止编造或推算，"
    "禁止自行按阈值下结论（判定以工具返回的 rule/severity 为准）；\n"
    "2. 尽量附证据（Prometheus 图表 URL / 工单号）；\n"
    "3. 查不到或工具失败时明确说明数据不可用，禁止猜测；\n"
    "4. 回答使用简洁中文。"
)


class ChatAgent:
    def __init__(
        self,
        pool: MCPServerPool,
        llm: LLMClient,
        tool_schemas: list[dict[str, Any]],
        max_iterations: int = 8,
        tool_result_max_chars: int = 2000,
        session: SessionStore | None = None,
        extra_system_prompt: str = "",
        local_tools: dict[str, Any] | None = None,
    ) -> None:
        self._pool = pool
        self._llm = llm
        self._schemas = tool_schemas
        self._max_iterations = max(1, max_iterations)
        self._result_max = tool_result_max_chars
        self._session = session or SessionStore()
        # 基础行为红线（代码持有）+ 运行时技能方法论段（skills/ 目录，skillregistry 装配）
        self._system_prompt = BOT_SYSTEM_PROMPT + extra_system_prompt
        self._graph = build_bot_graph(
            llm=llm,
            pool=pool,
            tool_schemas=tool_schemas,
            max_iterations=self._max_iterations,
            tool_result_max_chars=self._result_max,
            local_tools=local_tools,
        )

    @property
    def system_prompt(self) -> str:
        """组装后的 system prompt（诊断与测试用）。"""
        return self._system_prompt

    async def answer(self, chat_id: str, user_text: str) -> str:
        """处理一条用户消息：图内 ReAct 循环直到无工具调用或达上限；LLM 断供回可用性提示。"""
        history = self._session.append_user(chat_id, user_text)
        initial: BotState = {
            "messages": [{"role": "system", "content": self._system_prompt}, *history],
            "tool_rounds": 0,
        }
        try:
            # 步数上限随迭代上限放大（agent+tools+finalize ≈ 2N+1），防 GraphRecursionError
            # 抢在 finalize 之前触发
            result = await self._graph.ainvoke(
                initial, config={"recursion_limit": 2 * self._max_iterations + 6}
            )
        except LLMError as exc:
            logger.warning("bot LLM 调用失败: %s", exc)
            return FALLBACK_LLM_DOWN
        content = result.get("answer") or FALLBACK_NO_CONTENT
        self._session.append_assistant(chat_id, content)
        return content
