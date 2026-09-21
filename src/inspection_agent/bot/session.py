"""会话历史（内存态，按 chat_id 隔离；重启即失，P2 再考虑持久化）。"""

from __future__ import annotations

import time


class SessionStore:
    def __init__(self, max_history: int = 20, ttl_sec: float = 3600.0) -> None:
        self._max = max(2, max_history)
        self._ttl = ttl_sec
        self._chats: dict[str, list[dict[str, str]]] = {}
        self._last_active: dict[str, float] = {}

    def history(self, chat_id: str) -> list[dict[str, str]]:
        self._gc()
        return list(self._chats.get(chat_id, []))

    def append_user(self, chat_id: str, text: str) -> list[dict[str, str]]:
        """追加用户消息并返回裁剪后的历史（含 system 由 agent 层拼接）。"""
        self._touch(chat_id).append({"role": "user", "content": text})
        return self._trim(self._chats[chat_id])

    def append_assistant(self, chat_id: str, text: str) -> None:
        self._touch(chat_id).append({"role": "assistant", "content": text})
        self._trim(self._chats[chat_id])

    def _touch(self, chat_id: str) -> list[dict[str, str]]:
        self._last_active[chat_id] = time.monotonic()
        return self._chats.setdefault(chat_id, [])

    def _trim(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        if len(messages) > self._max:
            del messages[: len(messages) - self._max]
        return messages

    def _gc(self) -> None:
        now = time.monotonic()
        expired = [c for c, ts in self._last_active.items() if now - ts > self._ttl]
        for chat_id in expired:
            self._chats.pop(chat_id, None)
            self._last_active.pop(chat_id, None)
