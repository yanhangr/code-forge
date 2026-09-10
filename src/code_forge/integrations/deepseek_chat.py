"""DeepSeek OpenAI-compatible chat adapter.

The API key is intentionally injected by the composition root and never
serialized into repository state, events, logs, or public responses.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import threading
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from typing import Any

from code_forge.contracts import DomainError, ErrorCode


class DeepSeekChatAdapter:
    def __init__(
        self,
        api_key: str,
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 60,
        verify_ssl: bool | None = None,
    ):
        self.api_key = api_key
        self.model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
        self.base_url = (base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        self.timeout_seconds = timeout_seconds
        if verify_ssl is None:
            verify_ssl = os.environ.get("DEEPSEEK_VERIFY_SSL", "1").lower() not in {"0", "false", "no"}
        self.verify_ssl = verify_ssl

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._complete_sync, messages, tools)

    def _complete_sync(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        url = self.base_url + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 1024,
            "temperature": 0.7,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            context = None if self.verify_ssl else ssl._create_unverified_context()
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
                context=context,
            ) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise DomainError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                f"DeepSeek request failed with HTTP {exc.code}: {detail[:300]}",
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise DomainError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                f"DeepSeek request failed: {exc}",
            ) from exc
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise DomainError(ErrorCode.DEPENDENCY_UNAVAILABLE, "DeepSeek returned invalid JSON") from exc
        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DomainError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "DeepSeek response did not contain a message",
            ) from exc

    async def complete_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield content deltas and a final assistant message from an SSE stream."""

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        def emit(kind: str, value: Any) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, (kind, value))

        def worker() -> None:
            try:
                payload: dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                    "max_tokens": 1024,
                    "temperature": 0.7,
                    "stream": True,
                }
                if tools:
                    payload["tools"] = tools
                    payload["tool_choice"] = "auto"
                request = urllib.request.Request(
                    self.base_url + "/chat/completions",
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                context = None if self.verify_ssl else ssl._create_unverified_context()
                content_parts: list[str] = []
                reasoning_parts: list[str] = []
                tool_calls: dict[int, dict[str, Any]] = {}
                with urllib.request.urlopen(
                    request,
                    timeout=self.timeout_seconds,
                    context=context,
                ) as response:
                    for raw_line in response:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line.startswith("data:"):
                            continue
                        raw_data = line[5:].strip()
                        if raw_data == "[DONE]":
                            break
                        chunk = json.loads(raw_data)
                        delta = chunk["choices"][0].get("delta") or {}
                        content = delta.get("content")
                        if content:
                            content_parts.append(content)
                            emit("event", {"type": "content", "text": content})
                        reasoning = delta.get("reasoning_content")
                        if reasoning:
                            reasoning_parts.append(reasoning)
                        for tool_call in delta.get("tool_calls") or []:
                            index = int(tool_call.get("index", 0))
                            current = tool_calls.setdefault(
                                index,
                                {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                },
                            )
                            if tool_call.get("id"):
                                current["id"] = tool_call["id"]
                            function = tool_call.get("function") or {}
                            if function.get("name"):
                                current["function"]["name"] += function["name"]
                            if function.get("arguments"):
                                current["function"]["arguments"] += function["arguments"]
                message: dict[str, Any] = {
                    "role": "assistant",
                    "content": "".join(content_parts),
                }
                if reasoning_parts:
                    message["reasoning_content"] = "".join(reasoning_parts)
                if tool_calls:
                    message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
                emit("event", {"type": "message", "message": message})
                emit("done", None)
            except Exception as exc:
                emit("error", exc)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        while True:
            kind, value = await queue.get()
            if kind == "done":
                return
            if kind == "error":
                raise DomainError(
                    ErrorCode.DEPENDENCY_UNAVAILABLE,
                    f"DeepSeek streaming failed: {value}",
                ) from value
            yield value
