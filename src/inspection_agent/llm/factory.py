"""LangChain ChatModel 装配（OpenAI 兼容端点：DeepSeek 等；唯一装配点）。

- 引擎：langchain-openai 的 ChatOpenAI，base_url 指向 OpenAI 兼容服务
  （DeepSeek 官方 https://api.deepseek.com 或自建网关，POST {base_url}/chat/completions）；
- max_retries=0：重试语义由调用方控制（bot 会话回提示 / 叙事整篇降级 P0 模板），
  框架层不做静默重试（保持旧 httpx 直连的单次尝试语义）；
- 密钥不入码：api_key 由 LLMClient.from_config 从环境变量读取后传入（config.llm.api_key_env）；
- 超时/温度/输出上限透传 LLMConfig（低温度保数字稳定，SKILL 红线 3）。
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from ..config import LLMConfig


def build_chat_model(config: LLMConfig, api_key: str) -> ChatOpenAI:
    """按 LLMConfig 构建 ChatOpenAI（base_url/model/api_key 校验在 LLMClient.from_config）。"""
    return ChatOpenAI(
        model=config.model,
        base_url=config.base_url,
        api_key=api_key,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        timeout=config.timeout_sec,
        max_retries=0,
    )
