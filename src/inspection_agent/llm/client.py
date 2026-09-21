"""OpenAI 兼容 chat client（httpx 直连 /chat/completions，不引 openai SDK）。

- base_url 形如 https://api.example.com/v1，实际 POST {base_url}/chat/completions；
- 支持 tools/function calling（bot agent loop 使用；叙事层不传 tools）；
- 任何失败抛 LLMError，由调用方降级处理。
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from ..config import LLMConfig

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """LLM 配置缺失或调用失败。"""


class LLMClient:
    def __init__(self, config: LLMConfig, api_key: str) -> None:
        self._cfg = config
        self._api_key = api_key
        self._url = config.base_url.rstrip("/") + "/chat/completions"

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
        payload: dict[str, Any] = {
            "model": self._cfg.model,
            "messages": messages,
            "temperature": self._cfg.temperature,
            "max_tokens": self._cfg.max_tokens,
        }
        if tools:
            payload["tools"] = tools
        try:
            async with httpx.AsyncClient(timeout=self._cfg.timeout_sec) as client:
                resp = await client.post(
                    self._url,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 请求失败: {exc}") from exc
        except ValueError as exc:
            raise LLMError(f"LLM 返回非 JSON: {exc}") from exc
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"LLM 响应缺少 choices: {str(data)[:200]}")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise LLMError("LLM 响应缺少 message")
        return message
