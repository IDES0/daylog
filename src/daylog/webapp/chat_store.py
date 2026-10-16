"""The web app's chat: what's shown (the last 40 messages) and what the model
is given (chat.trim_history of the same turns), in server memory.

Separate from Telegram's history (bot_data): the two clients are equal, and
each keeps its own conversation. Thread-safe; single user.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
from typing import Any

from anthropic.types import MessageParam

from daylog import chat

MAX_SHOWN = 40


class ChatStore:
    def __init__(self) -> None:
        self._shown: deque[dict[str, str]] = deque(maxlen=MAX_SHOWN)
        self._history: list[MessageParam] = []
        self._lock = threading.Lock()

    def messages(self) -> list[dict[str, str]]:
        with self._lock:
            return list(self._shown)

    def history(self) -> list[MessageParam]:
        with self._lock:
            return list(self._history)

    def record(
        self, user_text: str, reply: str, new_history: list[MessageParam], now: datetime
    ) -> None:
        """One exchange: shown as two messages, and the model's history replaced."""
        at = now.isoformat(timespec="seconds")
        with self._lock:
            self._shown.append({"role": "user", "text": user_text, "at": at})
            self._shown.append({"role": "assistant", "text": reply, "at": at})
            self._history = chat.trim_history(new_history)

    def remember(self, label: str, text: str) -> None:
        """Something the app produced on its own (a brief, a plan): context for the
        next chat turn, so "make option B shorter" knows the plan. Not shown."""
        entry: list[Any] = [
            {"role": "user", "content": f"[{label}]"},
            {"role": "assistant", "content": text},
        ]
        with self._lock:
            self._history = chat.trim_history([*self._history, *entry])

    def reset(self) -> None:
        with self._lock:
            self._shown.clear()
            self._history = []
