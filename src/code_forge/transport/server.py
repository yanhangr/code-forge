"""Standard-library Platform-facing HTTP/SSE adapter for Agent Runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from code_forge.contracts import (
    BusinessCode,
    DomainError,
    ErrorCode,
    EventType,
    ExecutionContext,
    MessageStatus,
    PlatformEventType,
    RunStatus,
    UserBinding,
)
from code_forge.runtime.app import AgentRuntime
from code_forge.runtime.plugins import PluginRegistry, build_plugins

TERMINAL = {
    RunStatus.SUCCEEDED.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
    RunStatus.TIMED_OUT.value,
}

EVENT_TYPE_MAP = {
    EventType.RUN_ACCEPTED.value: PlatformEventType.MESSAGE_ACCEPTED.value,
    EventType.RUN_STARTED.value: PlatformEventType.MESSAGE_STARTED.value,
    EventType.RUN_WAITING.value: PlatformEventType.MESSAGE_WAITING.value,
    EventType.RUN_RESUMED.value: PlatformEventType.MESSAGE_RESUMED.value,
    EventType.RUN_RECOVERING.value: PlatformEventType.MESSAGE_RECOVERING.value,
    EventType.RUN_CANCEL_REQUESTED.value: PlatformEventType.MESSAGE_CANCEL_REQUESTED.value,
    EventType.RUN_FINISHED.value: PlatformEventType.MESSAGE_FINISHED.value,
    EventType.MESSAGE_DELTA.value: PlatformEventType.MESSAGE_DELTA.value,
    EventType.MESSAGE_COMPLETED.value: PlatformEventType.MESSAGE_COMPLETED.value,
    EventType.TOOL_PREPARED.value: PlatformEventType.TOOL_PREPARED.value,
    EventType.TOOL_STARTED.value: PlatformEventType.TOOL_STARTED.value,
    EventType.TOOL_OUTPUT.value: PlatformEventType.TOOL_OUTPUT.value,
    EventType.TOOL_FINISHED.value: PlatformEventType.TOOL_FINISHED.value,
    EventType.TOOL_UNKNOWN.value: PlatformEventType.TOOL_UNKNOWN.value,
    EventType.SKILL_ACTIVATED.value: PlatformEventType.SKILL_ACTIVATED.value,
    EventType.SKILL_STARTED.value: PlatformEventType.SKILL_STARTED.value,
    EventType.SKILL_FINISHED.value: PlatformEventType.SKILL_FINISHED.value,
    EventType.WORKSPACE_COMMITTED.value: PlatformEventType.WORKSPACE_COMMITTED.value,
}


def _absolute_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainError(ErrorCode.INVALID_REQUEST, f"{field} is required")
    path = Path(value)
    if not path.is_absolute():
        raise DomainError(ErrorCode.INVALID_REQUEST, f"{field} must be an absolute path")
    return path.resolve(strict=False).as_posix()


def _relative_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainError(ErrorCode.INVALID_REQUEST, f"{field} is required")
    path = Path(value)
    if path.is_absolute():
        raise DomainError(ErrorCode.INVALID_REQUEST, f"{field} must be relative")
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if not parts or any(part == ".." for part in parts):
        raise DomainError(ErrorCode.INVALID_REQUEST, f"{field} escapes its logical root")
    return Path(*parts).as_posix()


def _reference(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if value in (None, "") and allow_empty:
        return ""
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 200
        or not re.fullmatch(r"[A-Za-z0-9._-]+", value)
    ):
        raise DomainError(ErrorCode.INVALID_REQUEST, f"{field} is invalid")
    return value


def _user_ref(user_rel_path: str) -> str:
    digest = hashlib.sha256(user_rel_path.encode("utf-8")).hexdigest()[:32]
    return f"u-{digest}"


def _derived_project_ref(project_rel_path: str | None) -> str:
    if project_rel_path is None:
        return "__default__"
    digest = hashlib.sha256(project_rel_path.encode("utf-8")).hexdigest()[:24]
    return f"__derived_{digest}"


def _binding_from_payload(
    storage_root: Any,
    user_rel_path: Any,
    project_ref: Any,
    project_rel_path: Any,
) -> UserBinding:
    root = _absolute_path(storage_root, "storage_root")
    user_relative = _relative_path(user_rel_path, "user_rel_path")
    project_relative = (
        _relative_path(project_rel_path, "project_rel_path")
        if project_rel_path not in (None, "")
        else None
    )
    public_project_ref = _reference(project_ref, "project_ref", allow_empty=True) or None
    internal_project_ref = public_project_ref or _derived_project_ref(project_relative)
    user_path = (Path(root) / user_relative).resolve(strict=False)
    if project_relative:
        project_path = (user_path / project_relative).resolve(strict=False)
    elif public_project_ref:
        project_path = user_path / "workspace" / "projects" / public_project_ref
    else:
        project_path = user_path / "workspace"
    user_ref = _user_ref(user_relative)
    return UserBinding(
        scope_id=user_ref,
        tenant_ref="platform",
        user_ref=user_ref,
        user_path=user_path.as_posix(),
        project_ref=internal_project_ref,
        project_path=project_path.resolve(strict=False).as_posix(),
        storage_root=root,
        user_rel_path=user_relative,
        project_rel_path=project_relative or "",
    )


def _binding_from_session(
    session: Mapping[str, Any],
    *,
    storage_root: Any = None,
) -> UserBinding | None:
    if not session.get("project_path") or not session.get("user_path"):
        return None
    project_path = Path(str(session["project_path"]))
    user_path = Path(str(session["user_path"]))
    try:
        project_rel_path = project_path.relative_to(user_path).as_posix()
    except ValueError:
        return None
    internal_project_ref = str(session.get("project_ref") or "")
    public_project_ref = None if internal_project_ref.startswith("__") else internal_project_ref
    root = (
        _absolute_path(storage_root, "storage_root")
        if storage_root not in (None, "")
        else str(session.get("storage_root") or "")
    )
    if not root:
        return None
    return _binding_from_payload(
        root,
        session.get("user_rel_path"),
        public_project_ref,
        project_rel_path,
    )


class RuntimeHTTPServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], runtime: AgentRuntime):
        self.runtime = runtime
        super().__init__(address, RuntimeRequestHandler)


class RuntimeRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: RuntimeHTTPServer

    def log_message(self, fmt: str, *args: Any) -> None:
        return None

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        try:
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            handlers = {
                "/health": lambda: self._health(),
                "/v1/get-session": lambda: self._get_session(query),
                "/v1/list-sessions": lambda: self._list_sessions(query),
                "/v1/get-session-state": lambda: self._get_session_state(query),
                "/v1/get-message": lambda: self._get_message(query),
                "/v1/get-tool-execution": lambda: self._get_tool_execution(query),
                "/v1/list-session-messages": lambda: self._list_session_messages(query),
                "/v1/stream-session-events": lambda: self._stream_session_events(query),
                "/v1/list-session-events": lambda: self._list_session_events(query),
                "/v1/list-session-files": lambda: self._list_session_files(query),
                "/v1/read-session-file": lambda: self._read_session_file(query),
            }
            handler = handlers.get(parsed.path)
            if handler is None:
                self._business_error(BusinessCode.INVALID_REQUEST, 404, "Not found")
                return
            handler()
        except DomainError as exc:
            self._domain_error(exc)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self._business_error(BusinessCode.INVALID_REQUEST, 400, str(exc))
        except Exception:
            self._business_error(BusinessCode.INTERNAL_ERROR, 500, "Runtime internal error")

    def do_POST(self) -> None:
        try:
            path = urlsplit(self.path).path
            body = self._read_body()
            handlers = {
                "/v1/create-session": lambda: self._create_session(body),
                "/v1/update-session": lambda: self._update_session(body),
                "/v1/send-message": lambda: self._send_message(body),
                "/v1/cancel-message": lambda: self._cancel_message(body),
                "/v1/reply": lambda: self._reply(body),
            }
            handler = handlers.get(path)
            if handler is None:
                self._business_error(BusinessCode.INVALID_REQUEST, 404, "Not found")
                return
            handler()
        except DomainError as exc:
            self._domain_error(exc)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self._business_error(BusinessCode.INVALID_REQUEST, 400, str(exc))
        except Exception:
            self._business_error(BusinessCode.INTERNAL_ERROR, 500, "Runtime internal error")

    def _health(self) -> None:
        self._ok(
            {
                "status": "ok",
                "api_version": "1",
                "model_configured": bool(
                    os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("MODEL_API_URL")
                ),
                "execution_backend": "local_process",
                "permission_mode": "default_allow",
                "isolation_mode": "trusted_logical",
            }
        )

    def _create_session(self, body: dict[str, Any]) -> None:
        binding = _binding_from_payload(
            body.get("storage_root"),
            body.get("user_rel_path"),
            body.get("project_ref"),
            body.get("project_rel_path"),
        )
        context = ExecutionContext(
            scope_id=binding.scope_id,
            actor_ref="platform:api",
            user_binding=binding,
        )
        session = self.server.runtime.create_session(
            scope_id=binding.scope_id,
            idempotency_key=f"platform-session:{uuid4()}",
            title="新会话",
            external_ref=None,
            context=context,
        )
        self._ok(self._session_view(session), 201)

    def _update_session(self, body: dict[str, Any]) -> None:
        session_id = self._required_string(body, "session_id")
        title = self._required_string(body, "title")
        if not 1 <= len(title) <= 200:
            raise DomainError(ErrorCode.INVALID_REQUEST, "title must be 1-200 characters")
        if not self.server.runtime.store.get_session_unscoped(session_id):
            raise DomainError(ErrorCode.SESSION_NOT_FOUND, "Session not found")
        session = self.server.runtime.store.update_session_title(
            session_id,
            title,
            "system:runtime/api",
        )
        self.server.runtime.sync_session_storage(session)
        self._ok(self._session_view(session))

    def _get_session(self, query: dict[str, list[str]]) -> None:
        session = self._require_session(query)
        self._ok(self._session_view(session))

    def _list_sessions(self, query: dict[str, list[str]]) -> None:
        limit = self._int_query(query, "limit", 50, 100)
        items, next_cursor = self.server.runtime.store.list_sessions_public(
            user_rel_path=self._query_value(query, "user_rel_path"),
            project_ref=self._query_value(query, "project_ref"),
            cursor=self._query_value(query, "cursor"),
            limit=limit,
        )
        self._ok(
            {
                "items": [self._session_view(item) for item in items],
                "next_cursor": next_cursor,
            }
        )

    def _get_session_state(self, query: dict[str, list[str]]) -> None:
        session = self._require_session(query)
        current = self.server.runtime.store.get_current_message(session["id"])
        pending = self.server.runtime.store.get_pending_reply(session["id"])
        cursor = self.server.runtime.store.session_event_cursor(session["id"])
        self._ok(
            {
                "session": self._session_view(session),
                "current_message": (
                    {
                        "message_id": current["id"],
                        "status": self._message_status(current["status"]),
                    }
                    if current
                    else None
                ),
                "pending_reply": pending,
                "event_cursor": f"{session['id']}:{cursor}",
            }
        )

    def _get_message(self, query: dict[str, list[str]]) -> None:
        message_id = self._required_query(query, "message_id")
        message = self.server.runtime.store.get_run_unscoped(message_id)
        if not message:
            raise DomainError(ErrorCode.RUN_NOT_FOUND, "Message not found")
        self._ok(self._message_view(message))

    def _get_tool_execution(self, query: dict[str, list[str]]) -> None:
        session = self._require_session(query)
        operation_id = self._required_query(query, "operation_id")
        detail = self.server.runtime.get_tool_execution(session["id"], operation_id)
        self._ok(self._tool_execution_view(detail))

    def _tool_execution_view(self, detail: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "operation_id": detail["operation_id"],
            "message_id": detail["message_id"],
            "tool_ref": detail["tool_ref"],
            "status": detail["status"],
            "input": detail["input"],
            "stdout": detail["stdout"],
            "stderr": detail["stderr"],
            "truncated": detail["truncated"],
            "error": self._public_error(detail.get("error"), detail["message_id"]),
        }

    def _list_session_messages(self, query: dict[str, list[str]]) -> None:
        session = self._require_session(query)
        limit = self._int_query(query, "limit", 50, 100)
        cursor = self._query_value(query, "cursor")
        items, next_cursor = self.server.runtime.store.list_messages_public(
            session["id"],
            cursor,
            limit,
        )
        self._ok(
            {
                "items": [self._message_view(item) for item in items],
                "next_cursor": next_cursor,
            }
        )

    def _send_message(self, body: dict[str, Any]) -> None:
        session = self._require_session_id(str(body.get("session_id") or ""))
        input_text = self._required_string(body, "input")
        if not 1 <= len(input_text) <= 100_000:
            raise DomainError(ErrorCode.INVALID_REQUEST, "input must be 1-100000 characters")
        binding = _binding_from_session(session, storage_root=body.get("storage_root"))
        if binding is None:
            raise DomainError(ErrorCode.STATE_CONFLICT, "Session has no managed workspace")
        if binding.storage_root != session.get("storage_root"):
            self.server.runtime.store.rebind_workspace_storage(
                session,
                binding,
                "system:runtime/api",
            )
        skill_paths = self._skill_paths(body.get("skill_paths"), binding)
        before = self.server.runtime.store.session_event_cursor(session["id"])
        context = ExecutionContext(
            scope_id=binding.scope_id,
            actor_ref="platform:api",
            user_binding=binding,
        )
        accepted = self.server.runtime.submit_run(
            session_id=session["id"],
            input_text=input_text,
            agent_ref=str(body.get("agent_ref") or "general@1"),
            skills=(),
            skill_paths=skill_paths,
            idempotency_key=f"platform-message:{uuid4()}",
            context=context,
            reject_if_active=True,
        )
        self._stream_message(
            binding.scope_id,
            session["id"],
            accepted["id"],
            before,
        )

    def _reply(self, body: dict[str, Any]) -> None:
        message_id = self._required_string(body, "message_id")
        pending_id = self._required_string(body, "pending_id")
        text = self._required_string(body, "text")
        if not 1 <= len(text) <= 100_000:
            raise DomainError(ErrorCode.INVALID_REQUEST, "text must be 1-100000 characters")
        message = self.server.runtime.store.get_run_unscoped(message_id)
        if not message:
            raise DomainError(ErrorCode.RUN_NOT_FOUND, "Message not found")
        before = self.server.runtime.store.session_event_cursor(message["session_id"])
        self.server.runtime.respond_to_run(
            scope_id=message["scope_id"],
            run_id=message_id,
            pending_id=pending_id,
            response_key=f"platform:{pending_id}",
            expected_state_version=int(message["state_version"]),
            text=text,
        )
        self._stream_message(
            message["scope_id"],
            message["session_id"],
            message_id,
            before,
        )

    def _cancel_message(self, body: dict[str, Any]) -> None:
        message_id = self._required_string(body, "message_id")
        message = self.server.runtime.store.get_run_unscoped(message_id)
        if not message:
            raise DomainError(ErrorCode.RUN_NOT_FOUND, "Message not found")
        cancelled = self.server.runtime.cancel_run(
            message["scope_id"],
            message_id,
            actor_ref="platform:api",
        )
        self._ok(self._message_view(cancelled))

    def _stream_session_events(self, query: dict[str, list[str]]) -> None:
        session = self._require_session(query)
        after_seq = self._int_query(query, "after_seq", 0, 10**9)
        self._stream(
            session["scope_id"],
            session["id"],
            terminal_message_id=None,
            after_seq=after_seq,
        )

    def _list_session_events(self, query: dict[str, list[str]]) -> None:
        session = self._require_session(query)
        after_seq = self._int_query(query, "after_seq", 0, 10**9)
        limit = self._int_query(query, "limit", 200, 1000)
        events, next_after = self.server.runtime.store.list_session_events(
            session["id"],
            after_seq,
            limit,
        )
        items = [
            projected for event in events if (projected := self._public_event(event)) is not None
        ]
        self._ok({"items": items, "next_after_seq": next_after})

    def _list_session_files(self, query: dict[str, list[str]]) -> None:
        session = self._require_session(query)
        binding = _binding_from_session(
            session,
            storage_root=self._query_value(query, "storage_root"),
        )
        if binding is None:
            raise DomainError(ErrorCode.STATE_CONFLICT, "Session has no managed workspace")
        files = self.server.runtime.workspace.list_files(session["workspace_id"], binding)
        items = [
            {
                "path": item["path"],
                "size_bytes": item["size_bytes"],
                "digest": f"sha256:{item['digest']}",
            }
            for item in files
        ]
        self._ok({"items": items, "next_cursor": None})

    def _read_session_file(self, query: dict[str, list[str]]) -> None:
        session = self._require_session(query)
        relative_path = _relative_path(self._required_query(query, "path"), "path")
        binding = _binding_from_session(
            session,
            storage_root=self._query_value(query, "storage_root"),
        )
        if binding is None:
            raise DomainError(ErrorCode.STATE_CONFLICT, "Session has no managed workspace")
        text, digest, truncated = self.server.runtime.workspace.read_text(
            session["workspace_id"],
            relative_path,
            user_binding=binding,
        )
        self._ok(
            {
                "path": relative_path,
                "content": text,
                "digest": f"sha256:{digest}",
                "truncated": truncated,
            }
        )

    def _stream_message(
        self,
        scope_id: str,
        session_id: str,
        message_id: str,
        after_seq: int,
    ) -> None:
        self._stream(
            scope_id,
            session_id,
            terminal_message_id=message_id,
            after_seq=after_seq,
        )

    def _stream(
        self,
        scope_id: str,
        session_id: str,
        *,
        terminal_message_id: str | None,
        after_seq: int,
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.close_connection = True
        self._cors_headers()
        self.end_headers()
        cursor = after_seq
        try:
            while True:
                events, cursor = self.server.runtime.store.list_session_events(
                    session_id,
                    cursor,
                    200,
                )
                for event in events:
                    if terminal_message_id and event["run_id"] != terminal_message_id:
                        continue
                    projected = self._public_event(event)
                    if projected is None:
                        continue
                    payload = json.dumps(
                        {
                            "code": BusinessCode.OK.value,
                            "message": "success",
                            "data": projected,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    self.wfile.write(
                        f"id: {session_id}:{projected['seq']}\n"
                        f"event: {projected['type']}\n"
                        f"data: {payload}\n\n".encode("utf-8")
                    )
                    self.wfile.flush()
                if terminal_message_id:
                    message = self.server.runtime.store.get_run(scope_id, terminal_message_id)
                    if (
                        message
                        and message["status"] in TERMINAL
                        and cursor >= self.server.runtime.store.session_event_cursor(session_id)
                    ):
                        break
                else:
                    current = self.server.runtime.store.get_current_message(session_id)
                    if not current or (
                        current["status"] in TERMINAL
                        and cursor >= self.server.runtime.store.session_event_cursor(session_id)
                    ):
                        break
                time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _public_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        public_type = EVENT_TYPE_MAP.get(event["type"])
        if public_type is None:
            return None
        message_id = event["run_id"]
        data = event.get("data") or {}
        if event["type"] == EventType.RUN_ACCEPTED.value:
            payload = {"message_id": message_id, "status": MessageStatus.QUEUED.value}
        elif event["type"] == EventType.RUN_STARTED.value:
            payload = {"message_id": message_id, "status": MessageStatus.RUNNING.value}
        elif event["type"] == EventType.RUN_RESUMED.value:
            payload = {"message_id": message_id, "status": MessageStatus.QUEUED.value}
        elif event["type"] == EventType.RUN_WAITING.value:
            pending = self.server.runtime.store.get_pending_reply(event["session_id"])
            payload = {
                "message_id": message_id,
                "status": self._message_status(str(data.get("status") or "")),
                "pending_reply": pending,
            }
        elif event["type"] == EventType.RUN_RECOVERING.value:
            payload = {"message_id": message_id, "reason": data.get("reason", "")}
        elif event["type"] == EventType.RUN_CANCEL_REQUESTED.value:
            payload = {
                "message_id": message_id,
                "status": MessageStatus.CANCELLING.value,
            }
        elif event["type"] == EventType.RUN_FINISHED.value:
            payload = {
                "message_id": message_id,
                "status": self._message_status(str(data.get("status") or "")),
                "task_outcome": data.get("task_outcome"),
                "output": data.get("output"),
                "error": self._public_error(data.get("error"), message_id),
            }
        elif event["type"] in {
            EventType.MESSAGE_DELTA.value,
            EventType.MESSAGE_COMPLETED.value,
        }:
            payload = {"text": data.get("text", "")}
        else:
            payload = dict(data)
        return {
            "event_id": event["event_id"],
            "session_id": event["session_id"],
            "message_id": message_id,
            "seq": int(event["session_seq"]),
            "type": public_type,
            "occurred_at": event["occurred_at"],
            "data": payload,
        }

    def _session_view(self, session: Mapping[str, Any]) -> dict[str, Any]:
        internal_project_ref = str(session.get("project_ref") or "")
        project_ref = None if internal_project_ref.startswith("__") else internal_project_ref
        return {
            "session_id": session["id"],
            "title": session["title"],
            "user_rel_path": session.get("user_rel_path") or "",
            "project_ref": project_ref,
            "project_rel_path": self._project_rel_path(session),
            "date_created": session["date_created"],
            "date_updated": session["date_updated"],
        }

    @staticmethod
    def _project_rel_path(session: Mapping[str, Any]) -> str:
        user_path = session.get("user_path")
        project_path = session.get("project_path")
        if not user_path or not project_path:
            return ""
        try:
            return Path(str(project_path)).relative_to(Path(str(user_path))).as_posix()
        except ValueError:
            return ""

    def _message_view(self, message: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "message_id": message["id"],
            "session_id": message["session_id"],
            "message_seq": int(message["run_seq"]),
            "input": message["input"],
            "status": self._message_status(message["status"]),
            "task_outcome": message.get("task_outcome"),
            "output": message.get("output"),
            "error": self._public_error(message.get("error"), message["id"]),
            "date_created": message["date_created"],
            "date_updated": message["date_updated"],
        }

    @staticmethod
    def _message_status(status: str) -> str:
        mapping = {
            RunStatus.QUEUED.value: MessageStatus.QUEUED.value,
            RunStatus.RUNNING.value: MessageStatus.RUNNING.value,
            RunStatus.WAITING_USER.value: MessageStatus.WAITING_USER.value,
            RunStatus.WAITING_EXTERNAL.value: MessageStatus.WAITING_EXTERNAL.value,
            RunStatus.RECOVERING.value: MessageStatus.RECOVERING.value,
            RunStatus.CANCELLING.value: MessageStatus.CANCELLING.value,
            RunStatus.SUCCEEDED.value: MessageStatus.SUCCEEDED.value,
            RunStatus.FAILED.value: MessageStatus.FAILED.value,
            RunStatus.CANCELLED.value: MessageStatus.CANCELLED.value,
            RunStatus.TIMED_OUT.value: MessageStatus.TIMED_OUT.value,
        }
        return mapping.get(status, MessageStatus.FAILED.value)

    @staticmethod
    def _public_error(error: Any, message_id: str) -> dict[str, Any] | None:
        if not error:
            return None
        if isinstance(error, str):
            error = {"message": error}
        internal_code = str(error.get("code") or "")
        code = {
            ErrorCode.INVALID_REQUEST.value: BusinessCode.INVALID_REQUEST.value,
            ErrorCode.SKILL_INCOMPATIBLE.value: BusinessCode.SKILL_INVALID.value,
            ErrorCode.SKILL_NOT_FOUND.value: BusinessCode.SKILL_INVALID.value,
            ErrorCode.MODEL_NOT_CONFIGURED.value: BusinessCode.MODEL_UNAVAILABLE.value,
            ErrorCode.EXECUTION_FAILED.value: BusinessCode.EXECUTION_FAILED.value,
            ErrorCode.EXECUTION_UNKNOWN.value: BusinessCode.EXECUTION_UNKNOWN.value,
            ErrorCode.DEPENDENCY_UNAVAILABLE.value: BusinessCode.DEPENDENCY_UNAVAILABLE.value,
        }.get(internal_code, BusinessCode.EXECUTION_FAILED.value)
        return {
            "code": code,
            "message": str(error.get("message") or "Execution failed"),
            "retryable": bool(error.get("retryable", False)),
            "request_id": str(error.get("request_id") or message_id),
        }

    def _skill_paths(
        self,
        value: Any,
        binding: UserBinding,
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        if not isinstance(value, list) or len(value) > 30:
            raise DomainError(ErrorCode.INVALID_REQUEST, "skill_paths must be an array")
        if not value:
            return ()
        paths: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                raise DomainError(
                    ErrorCode.INVALID_REQUEST,
                    f"skill_paths[{index}] must be a non-empty string",
                )
            path = Path(item)
            if not path.is_absolute():
                storage_candidate = (Path(binding.storage_root) / path).resolve(strict=False)
                user_candidate = (Path(binding.user_path) / path).resolve(strict=False)
                path = storage_candidate if storage_candidate.exists() else user_candidate
            canonical = path.resolve(strict=False).as_posix()
            if canonical not in paths:
                paths.append(canonical)
        return tuple(paths)

    @staticmethod
    def _required_string(body: Mapping[str, Any], name: str) -> str:
        value = body.get(name)
        if not isinstance(value, str) or not value.strip():
            raise DomainError(ErrorCode.INVALID_REQUEST, f"{name} is required")
        return value

    @staticmethod
    def _query_value(query: Mapping[str, list[str]], name: str) -> str | None:
        values = query.get(name)
        if not values:
            return None
        return values[0] or None

    def _required_query(self, query: Mapping[str, list[str]], name: str) -> str:
        value = self._query_value(query, name)
        if not value:
            raise DomainError(ErrorCode.INVALID_REQUEST, f"{name} is required")
        return value

    def _require_session(self, query: Mapping[str, list[str]]) -> dict[str, Any]:
        return self._require_session_id(self._required_query(query, "session_id"))

    def _require_session_id(self, session_id: str) -> dict[str, Any]:
        if not session_id:
            raise DomainError(ErrorCode.INVALID_REQUEST, "session_id is required")
        session = self.server.runtime.store.get_session_unscoped(session_id)
        if not session:
            raise DomainError(ErrorCode.SESSION_NOT_FOUND, "Session not found")
        return session

    @staticmethod
    def _int_query(
        query: Mapping[str, list[str]],
        name: str,
        default: int,
        maximum: int,
    ) -> int:
        raw = query.get(name, [None])[0]
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise DomainError(ErrorCode.INVALID_REQUEST, f"{name} must be an integer") from exc
        if value < 0:
            raise DomainError(ErrorCode.INVALID_REQUEST, f"{name} must not be negative")
        return min(value, maximum)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise DomainError(ErrorCode.INVALID_REQUEST, "Request body too large")
        if not length:
            return {}
        raw = self.rfile.read(length)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise DomainError(ErrorCode.INVALID_REQUEST, "Request body must be an object")
        return value

    def _ok(self, data: dict[str, Any], status: int = 200) -> None:
        self._json(
            status,
            {
                "code": BusinessCode.OK.value,
                "message": "success",
                "data": data,
            },
        )

    def _domain_error(self, exc: DomainError) -> None:
        mapping = {
            ErrorCode.INVALID_REQUEST: (BusinessCode.INVALID_REQUEST, 400),
            ErrorCode.SESSION_NOT_FOUND: (BusinessCode.SESSION_NOT_FOUND, 404),
            ErrorCode.RUN_NOT_FOUND: (BusinessCode.MESSAGE_NOT_FOUND, 404),
            ErrorCode.IDEMPOTENCY_CONFLICT: (BusinessCode.STATE_CONFLICT, 409),
            ErrorCode.STATE_CONFLICT: (BusinessCode.STATE_CONFLICT, 409),
            ErrorCode.INVALID_TRANSITION: (BusinessCode.STATE_CONFLICT, 409),
            ErrorCode.MESSAGE_BUSY: (BusinessCode.MESSAGE_BUSY, 409),
            ErrorCode.SKILL_NOT_FOUND: (BusinessCode.SKILL_INVALID, 422),
            ErrorCode.SKILL_INCOMPATIBLE: (BusinessCode.SKILL_INVALID, 422),
            ErrorCode.MODEL_NOT_CONFIGURED: (BusinessCode.MODEL_UNAVAILABLE, 503),
            ErrorCode.EXECUTION_FAILED: (BusinessCode.EXECUTION_FAILED, 503),
            ErrorCode.EXECUTION_UNKNOWN: (BusinessCode.EXECUTION_UNKNOWN, 503),
            ErrorCode.DEPENDENCY_UNAVAILABLE: (BusinessCode.DEPENDENCY_UNAVAILABLE, 503),
        }
        business_code, status = mapping.get(
            exc.code,
            (BusinessCode.INTERNAL_ERROR, 500),
        )
        self._business_error(business_code, status, exc.message)

    def _business_error(
        self,
        code: BusinessCode,
        status: int,
        message: str,
    ) -> None:
        self._json(
            status,
            {
                "code": code.value,
                "message": message,
                "data": {},
            },
        )

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Last-Event-ID")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")


def create_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    root: str | Path | None = None,
    registry: PluginRegistry | None = None,
    env: Mapping[str, str] | None = None,
) -> RuntimeHTTPServer:
    env = env or os.environ
    base = Path(root or env.get("FORGE_RUNTIME_DIR", ".runtime")).resolve()
    plugins = build_plugins(base, env, registry=registry)
    runtime = AgentRuntime(
        plugins.store,
        plugins.resolver,
        plugins.workspace,
        plugins.execution,
        plugins.harness,
        repository=plugins.repository,
        authorization=plugins.authorization,
    )
    runtime.materialize_user_storage()
    runtime.start_worker()
    return RuntimeHTTPServer((host, port), runtime)
