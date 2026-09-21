"""自研 function-calling loop（bot 大脑）。

红线（SKILL 2/3/6）：
- 工具只读白名单，白名单外调用直接拒绝并回提示；
- 回答数字只能来自工具返回，工具失败回"数据不可用"而非猜测；
- 迭代上限（默认 8 轮）与单次工具结果截断；LLM 断供回复可用性提示。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..llm.client import LLMClient, LLMError
from ..mcpclient import MCPError, MCPServerPool
from .session import SessionStore

logger = logging.getLogger(__name__)

BOT_SYSTEM_PROMPT = (
    "你是 bingops 运维巡检助手，可以调用只读工具查询：CMDB 应用与资源、Prometheus 指标、"
    "夜莺告警事件、工单与变更记录。硬性规则：\n"
    "1. 回答中的所有数字必须来自工具返回结果，禁止编造或推算；\n"
    "2. 尽量附证据（Prometheus 图表 URL / 夜莺事件 / 工单号）；\n"
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
    ) -> None:
        self._pool = pool
        self._llm = llm
        self._schemas = tool_schemas
        self._max_iterations = max(1, max_iterations)
        self._result_max = tool_result_max_chars
        self._session = session or SessionStore()
        self._allowed_names = {s["function"]["name"] for s in tool_schemas}

    async def answer(self, chat_id: str, user_text: str) -> str:
        """处理一条用户消息：loop 直到无工具调用或达上限；LLM 断供返回可用性提示。"""
        history = self._session.append_user(chat_id, user_text)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": BOT_SYSTEM_PROMPT},
            *history,
        ]
        try:
            for _ in range(self._max_iterations):
                message = await self._llm.chat(messages, tools=self._schemas)
                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    content = str(message.get("content") or "").strip() or "（无内容）"
                    self._session.append_assistant(chat_id, content)
                    return content
                messages.append(message)
                for call in tool_calls:
                    result = await self._execute(call)
                    messages.append(
                        {"role": "tool", "tool_call_id": str(call.get("id") or ""), "content": result}
                    )
            # 达迭代上限：不再提供工具，要求基于已获取信息作答（优雅收尾）
            messages.append(
                {
                    "role": "user",
                    "content": "（系统提示）已达工具调用次数上限，请基于已获取的信息直接回答，"
                               "未查询到的部分明确说明。",
                }
            )
            message = await self._llm.chat(messages, tools=None)
            content = (
                str(message.get("content") or "").strip()
                or "本次查询复杂度过高，请缩小问题范围后重试。"
            )
            self._session.append_assistant(chat_id, content)
            return content
        except LLMError as exc:
            logger.warning("bot LLM 调用失败: %s", exc)
            return "当前 LLM 服务不可用，请稍后重试。（本次未查询任何数据，不作猜测）"

    async def _execute(self, call: dict[str, Any]) -> str:
        """执行单个 tool_call：白名单拒绝 / 参数容错 / MCP 失败转提示 / 超长截断。"""
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        raw_args = str(function.get("arguments") or "")
        if name not in self._allowed_names:
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
            conn = self._pool[server]
            data = await conn.call_tool(bare, arguments)
        except MCPError as exc:
            return f"工具执行失败（数据不可用，请勿猜测）：{exc}"
        text = json.dumps(data, ensure_ascii=False, default=str)
        if len(text) > self._result_max:
            text = text[: self._result_max] + f"\n…（结果已截断，原始长度 {len(text)} 字符）"
        return text
