"""bot 大脑（LangGraph ReAct 图引擎；对外接口与约束语义不变）。

红线（SKILL 2/3/6）：
- 工具只读白名单，白名单外调用直接拒绝并回提示（闸门在 graph/bot_graph.py 工具节点内）；
- 回答数字只能来自工具返回，工具失败回"数据不可用"而非猜测；
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
    "你是 bingops 运维巡检助手，可以调用只读工具查询：CMDB 应用与资源、Prometheus 指标、"
    "工单与变更记录。硬性规则：\n"
    "1. 回答中的所有数字必须来自工具返回结果，禁止编造或推算；\n"
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
