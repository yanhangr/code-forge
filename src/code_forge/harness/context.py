"""Conversation context assembly and bounded compression."""

from __future__ import annotations

from typing import Any


class ConversationContextManager:
    """Keeps recent turns verbatim and compresses older turns into a summary."""

    def __init__(self, max_chars: int = 24_000):
        self.max_chars = max(2_000, max_chars)

    def build(
        self,
        system_prompt: str,
        history: list[dict[str, str]],
        current_input: str,
    ) -> list[dict[str, str]]:
        current = {"role": "user", "content": current_input}
        system = {"role": "system", "content": system_prompt}
        if self._size(history) + len(current_input) <= self.max_chars:
            return [system, *history, current]

        recent_budget = self.max_chars // 2
        recent: list[dict[str, str]] = []
        used = 0
        for message in reversed(history):
            content_size = len(message.get("content", ""))
            if recent and used + content_size > recent_budget:
                break
            recent.append(message)
            used += content_size
        recent.reverse()
        older = history[: len(history) - len(recent)]
        summary_budget = max(500, self.max_chars // 3)
        summary = self._summarize(older, summary_budget)
        messages = [system]
        if summary:
            messages.append(
                {
                    "role": "system",
                    "content": "Earlier conversation summary:\n" + summary,
                }
            )
        messages.extend(recent)
        messages.append(current)
        return messages

    @staticmethod
    def _size(history: list[dict[str, str]]) -> int:
        return sum(len(message.get("content", "")) for message in history)

    @staticmethod
    def _summarize(messages: list[dict[str, str]], limit: int) -> str:
        parts: list[str] = []
        for message in messages:
            role = message.get("role", "unknown")
            content = " ".join(message.get("content", "").split())
            if not content:
                continue
            parts.append(f"{role}: {content[:500]}")
        return "\n".join(parts)[:limit]
