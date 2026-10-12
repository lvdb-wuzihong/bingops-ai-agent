"""OpenAI 兼容 chat client（LangChain ChatOpenAI 引擎，对外保持 dict 接口）。

- 引擎：llm/factory.build_chat_model 构建 ChatOpenAI（DeepSeek 等 OpenAI 兼容端点）；
- 对外语义不变：messages 为 OpenAI dict 列表，tools 为 OpenAI function schemas，
  返回 assistant message dict（content / tool_calls），任何失败抛 LLMError 由调用方降级
  （叙事整篇降级 P0 模板 / bot 回可用性提示，SKILL 红线 4）；
- 数字约束（红线 3）与本层无关：只透传消息，数字/证据契约校验在 narrate.verify_narrative。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from ..config import LLMConfig
from .factory import build_chat_model

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """LLM 配置缺失或调用失败。"""


class LLMClient:
    def __init__(self, config: LLMConfig, api_key: str) -> None:
        self._cfg = config
        self._model = build_chat_model(config, api_key)

    @classmethod
    def from_config(cls, config: LLMConfig) -> "LLMClient":
        if not config.base_url or not config.model:
            raise LLMError("llm 配置不完整（base_url/model）")
        api_key = os.environ.get(config.api_key_env) or ""
        if not api_key:
            raise LLMError(f"环境变量 {config.api_key_env} 未设置（密钥不入码）")
        return cls(config, api_key)

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """一轮 chat completion；返回 assistant message dict（content / tool_calls）。"""
        try:
            model = self._model.bind_tools(tools) if tools else self._model
            result = await model.ainvoke(_to_lc_messages(messages))
        except Exception as exc:  # SDK/网络/协议异常统一收敛，调用方降级语义不变
            raise LLMError(f"LLM 请求失败: {exc}") from exc
        if not isinstance(result, AIMessage):
            raise LLMError(f"LLM 返回非 assistant 消息: {type(result).__name__}")
        return _to_openai_message(result)


# ---------------------------------------------------------------- 消息格式转换


def _to_lc_messages(messages: list[dict[str, Any]]) -> list[Any]:
    """OpenAI dict 消息 → langchain 消息（assistant.tool_calls[].arguments 为 JSON 字符串）。"""
    out: list[Any] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            out.append(SystemMessage(content=str(content or "")))
        elif role == "tool":
            out.append(
                ToolMessage(
                    content=str(content or ""), tool_call_id=str(m.get("tool_call_id") or "")
                )
            )
        elif role == "assistant":
            out.append(
                AIMessage(content=str(content or ""), tool_calls=_to_lc_tool_calls(m.get("tool_calls")))
            )
        else:
            out.append(HumanMessage(content=str(content or "")))
    return out


def _to_lc_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """OpenAI tool_calls → langchain ToolCall dicts（arguments 容错解析，非法 JSON 记空参）。"""
    calls: list[dict[str, Any]] = []
    for call in raw or []:
        function = call.get("function") or {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                args = json.loads(arguments) if arguments.strip() else {}
            except ValueError:
                args = {}
        else:
            args = arguments
        calls.append(
            {
                "name": str(function.get("name") or ""),
                "args": args if isinstance(args, dict) else {},
                "id": str(call.get("id") or "") or None,
                "type": "tool_call",
            }
        )
    return calls


def _to_openai_message(message: AIMessage) -> dict[str, Any]:
    """langchain AIMessage → OpenAI assistant dict（content / tool_calls）。"""
    out: dict[str, Any] = {
        "role": "assistant",
        "content": (
            message.content if isinstance(message.content, str) else str(message.content or "")
        ),
    }
    tool_calls = [
        {
            "id": str(call.get("id") or ""),
            "type": "function",
            "function": {
                "name": str(call.get("name") or ""),
                "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False),
            },
        }
        for call in (message.tool_calls or [])
    ]
    if tool_calls:
        out["tool_calls"] = tool_calls
    return out
