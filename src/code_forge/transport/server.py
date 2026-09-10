"""Standard-library HTTP/SSE server for local Agent Runtime verification."""

from __future__ import annotations

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
    DomainError,
    ErrorCode,
    EventType,
    ExecutionContext,
    RunStatus,
    SkillBinding,
    TaskOutcome,
    ToolStatus,
)
from code_forge.runtime.app import AgentRuntime
from code_forge.runtime.plugins import PluginRegistry, build_plugins


TERMINAL = {
    RunStatus.SUCCEEDED.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
    RunStatus.TIMED_OUT.value,
}


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
        self._send_headers(204)

    def do_GET(self) -> None:
        try:
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            scope = self.headers.get("X-Forge-Scope", "default")

            if path == "/health":
                return self._health()
            if path == "/v1/skills":
                return self._list_skills()
            if path == "/v1/sessions":
                return self._list_sessions(scope, query)
            if match := re.fullmatch(r"/v1/sessions/([^/]+)", path):
                return self._get_session(scope, match.group(1))
            if match := re.fullmatch(r"/v1/sessions/([^/]+)/runs", path):
                return self._list_runs(scope, match.group(1), query)
            if match := re.fullmatch(r"/v1/runs/([^/]+)", path):
                return self._get_run(scope, match.group(1))
            if match := re.fullmatch(r"/v1/runs/([^/]+)/snapshot", path):
                return self._get_run_snapshot(scope, match.group(1))
            if match := re.fullmatch(r"/v1/runs/([^/]+)/event-history", path):
                return self._event_history(scope, match.group(1), query)
            if match := re.fullmatch(r"/v1/runs/([^/]+)/events", path):
                return self._stream_events(scope, match.group(1), query)
            if match := re.fullmatch(r"/v1/sessions/([^/]+)/files", path):
                return self._list_files(scope, match.group(1))
            if match := re.fullmatch(r"/v1/sessions/([^/]+)/files/content", path):
                return self._read_file(scope, match.group(1), query)
            return self._error(
                ErrorCode.INVALID_REQUEST,
                "Not found",
                404,
            )
        except DomainError as exc:
            self._domain_error(exc)
        except Exception as exc:
            self._error(ErrorCode.INVALID_REQUEST, str(exc) or exc.__class__.__name__, 400)

    def do_POST(self) -> None:
        try:
            parsed = urlsplit(self.path)
            path = parsed.path
            scope = self.headers.get("X-Forge-Scope", "default")
            body = self._read_body()

            if path == "/v1/sessions":
                return self._create_session(scope, body)
            if match := re.fullmatch(r"/v1/sessions/([^/]+)/runs", path):
                return self._create_run(scope, match.group(1), body)
            if match := re.fullmatch(r"/v1/runs/([^/]+)/cancel", path):
                return self._cancel_run(scope, match.group(1))
            if match := re.fullmatch(r"/v1/runs/([^/]+)/responses", path):
                return self._respond_to_run(scope, match.group(1), body)
            return self._error(ErrorCode.INVALID_REQUEST, "Not found", 404)
        except DomainError as exc:
            self._domain_error(exc)
        except Exception as exc:
            self._error(ErrorCode.INVALID_REQUEST, str(exc) or exc.__class__.__name__, 400)

    def _health(self) -> None:
        self._json(
            200,
            {
                "status": "ok",
                "api_version": "1",
                "model_configured": bool(
                    os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("MODEL_API_URL")
                ),
                "execution_backend": "local_process",
                "permission_mode": "default_allow",
            },
        )

    def _list_skills(self) -> None:
        self._json(
            200,
            {
                "items": self.server.runtime.resolver.list_skills(),
                "next_cursor": None,
            },
        )

    def _create_session(self, scope: str, body: dict[str, Any]) -> None:
        key = self._require_header("Idempotency-Key")
        title = body.get("title") or "New session"
        external_ref = body.get("external_ref")
        context = self._context_from_body(body.get("context"), scope)
        session = self.server.runtime.create_session(
            scope_id=scope,
            idempotency_key=key,
            title=title,
            external_ref=external_ref,
            context=context,
        )
        self._json(201, self._session_view(session))

    def _list_sessions(self, scope: str, query: dict[str, list[str]]) -> None:
        limit = self._int_query(query, "limit", 50, 100)
        cursor = query.get("cursor", [None])[0]
        items, next_cursor = self.server.runtime.store.list_sessions(scope, cursor, limit)
        self._json(
            200,
            {
                "items": [self._session_view(session) for session in items],
                "next_cursor": next_cursor,
            },
        )

    def _get_session(self, scope: str, session_id: str) -> None:
        session = self.server.runtime.store.get_session(scope, session_id)
        if not session:
            raise DomainError(ErrorCode.SESSION_NOT_FOUND, "Session not found")
        self._json(200, self._session_view(session))

    def _create_run(self, scope: str, session_id: str, body: dict[str, Any]) -> None:
        key = self._require_header("Idempotency-Key")
        input_text = body.get("input")
        if not input_text or not isinstance(input_text, str):
            raise DomainError(ErrorCode.INVALID_REQUEST, "input is required")
        agent_ref = body.get("agent_ref") or "general@1"
        context = self._context_from_body(body.get("context"), scope)
        raw_skills = body.get("skills") or []
        skills = tuple(
            SkillBinding(name=item.get("name"), version=item.get("version"))
            for item in raw_skills
            if isinstance(item, dict) and item.get("name")
        )
        accepted = self.server.runtime.submit_run(
            session_id=session_id,
            input_text=input_text,
            agent_ref=agent_ref,
            skills=skills,
            idempotency_key=key,
            context=context,
        )
        self._json(202, accepted)

    def _list_runs(self, scope: str, session_id: str, query: dict[str, list[str]]) -> None:
        self.server.runtime.store.require_session(scope, session_id)
        limit = self._int_query(query, "limit", 50, 100)
        cursor = query.get("cursor", [None])[0]
        items, next_cursor = self.server.runtime.store.list_runs(scope, session_id, cursor, limit)
        self._json(
            200,
            {
                "items": [self._run_view(run) for run in items],
                "next_cursor": next_cursor,
            },
        )

    def _get_run(self, scope: str, run_id: str) -> None:
        run = self.server.runtime.get_run(scope, run_id)
        if not run:
            raise DomainError(ErrorCode.RUN_NOT_FOUND, "Run not found")
        self._json(200, self._run_view(run))

    def _cancel_run(self, scope: str, run_id: str) -> None:
        run = self.server.runtime.cancel_run(scope, run_id)
        self._json(202, self._run_view(run))

    def _respond_to_run(self, scope: str, run_id: str, body: dict[str, Any]) -> None:
        pending_id = body.get("pending_id")
        response_key = body.get("response_key")
        expected_state_version = body.get("expected_state_version")
        text = body.get("text")
        if not all([pending_id, response_key, text]):
            raise DomainError(ErrorCode.INVALID_REQUEST, "Response fields are required")
        run = self.server.runtime.respond_to_run(
            scope_id=scope,
            run_id=run_id,
            pending_id=pending_id,
            response_key=response_key,
            expected_state_version=int(expected_state_version),
            text=text,
        )
        self._json(202, self._run_view(run))

    def _get_run_snapshot(self, scope: str, run_id: str) -> None:
        snapshot = self.server.runtime.store.get_snapshot(scope, run_id)
        if not snapshot:
            raise DomainError(ErrorCode.RUN_NOT_FOUND, "Run not found")
        self._json(
            200,
            {
                "run": self._run_view(snapshot["run"]),
                "event_cursor": snapshot["event_cursor"],
                "pending_responses": snapshot["pending_responses"],
            },
        )

    def _event_history(self, scope: str, run_id: str, query: dict[str, list[str]]) -> None:
        if not self.server.runtime.get_run(scope, run_id):
            raise DomainError(ErrorCode.RUN_NOT_FOUND, "Run not found")
        after_seq = self._int_query(query, "after_seq", 0, 10**9)
        limit = self._int_query(query, "limit", 200, 1000)
        events, next_after = self.server.runtime.store.list_events(
            scope, run_id, after_seq, limit
        )
        self._json(
            200,
            {"items": events, "next_after_seq": next_after},
        )

    def _stream_events(self, scope: str, run_id: str, query: dict[str, list[str]]) -> None:
        if not self.server.runtime.get_run(scope, run_id):
            self._domain_error(DomainError(ErrorCode.RUN_NOT_FOUND, "Run not found"))
            return
        after_seq = self._int_query(query, "after_seq", 0, 10**9)
        last_event_id = self.headers.get("Last-Event-ID")
        if last_event_id:
            cursor_run, cursor_seq = last_event_id.rsplit(":", 1)
            if cursor_run != run_id:
                self._domain_error(
                    DomainError(ErrorCode.INVALID_EVENT_CURSOR, "Cursor belongs to another Run")
                )
                return
            after_seq = max(after_seq, int(cursor_seq))

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._cors_headers()
        self.end_headers()
        cursor = after_seq
        try:
            while True:
                events, next_cursor = self.server.runtime.store.list_events(
                    scope, run_id, cursor, 200
                )
                for event in events:
                    event_id = f"{event['run_id']}:{event['seq']}"
                    payload = json.dumps(event, ensure_ascii=False, sort_keys=True)
                    self.wfile.write(
                        f"id: {event_id}\nevent: {event['type']}\ndata: {payload}\n\n".encode(
                            "utf-8"
                        )
                    )
                    self.wfile.flush()
                cursor = next_cursor
                run = self.server.runtime.get_run(scope, run_id)
                if run and run["status"] in TERMINAL and cursor >= self._current_event_seq(scope, run_id):
                    break
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _current_event_seq(self, scope: str, run_id: str) -> int:
        cursor = self.server.runtime.store.event_cursor(scope, run_id)
        return int(cursor.rsplit(":", 1)[1])

    def _list_files(self, scope: str, session_id: str) -> None:
        session = self.server.runtime.store.get_session(scope, session_id)
        if not session:
            raise DomainError(ErrorCode.SESSION_NOT_FOUND, "Session not found")
        files = self.server.runtime.workspace.list_files(session["workspace_id"])
        self._json(
            200,
            {"items": files, "next_cursor": None},
        )

    def _read_file(self, scope: str, session_id: str, query: dict[str, list[str]]) -> None:
        session = self.server.runtime.store.get_session(scope, session_id)
        if not session:
            raise DomainError(ErrorCode.SESSION_NOT_FOUND, "Session not found")
        relative_path = query.get("path", [None])[0]
        if not relative_path:
            raise DomainError(ErrorCode.INVALID_REQUEST, "path is required")
        text, digest, truncated = self.server.runtime.workspace.read_text(
            session["workspace_id"], relative_path
        )
        self._json(
            200,
            {
                "path": relative_path,
                "content": text,
                "digest": digest,
                "truncated": truncated,
            },
        )

    def _context_from_body(self, value: Any, scope: str) -> ExecutionContext:
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise DomainError(ErrorCode.INVALID_REQUEST, "context must be an object")
        body_scope = value.get("scope_id") or "default"
        if body_scope != scope:
            raise DomainError(
                ErrorCode.INVALID_REQUEST,
                "context.scope_id must match X-Forge-Scope",
            )
        return ExecutionContext(
            scope_id=scope,
            actor_ref=value.get("actor_ref"),
            external_ref=value.get("external_ref"),
        )

    def _require_header(self, name: str) -> str:
        value = self.headers.get(name)
        if not value:
            raise DomainError(ErrorCode.INVALID_REQUEST, f"{name} is required")
        return value

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise DomainError(ErrorCode.INVALID_REQUEST, "Request body too large")
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            raise DomainError(ErrorCode.INVALID_REQUEST, "Request body must be JSON")
        if not isinstance(value, dict):
            raise DomainError(ErrorCode.INVALID_REQUEST, "Request body must be an object")
        return value

    def _run_view(self, run: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": run["id"],
            "session_id": run["session_id"],
            "run_seq": run["run_seq"],
            "input": run["input"],
            "agent_ref": run["agent_ref"],
            "status": run["status"],
            "state_version": run["state_version"],
            "task_outcome": run.get("task_outcome"),
            "wait_reason": run.get("wait_reason"),
            "output": run.get("output"),
            "error": run.get("error"),
            "snapshot": run.get("config_snapshot"),
            "date_created": run["date_created"],
            "date_updated": run["date_updated"],
            "created_by": run["created_by"],
            "updated_by": run["updated_by"],
        }

    def _session_view(self, session: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": session["id"],
            "title": session["title"],
            "external_ref": session.get("external_ref"),
            "workspace_id": session["workspace_id"],
            "date_created": session["date_created"],
            "date_updated": session["date_updated"],
            "created_by": session["created_by"],
            "updated_by": session["updated_by"],
        }

    def _domain_error(self, exc: DomainError) -> None:
        status = {
            ErrorCode.INVALID_REQUEST: 400,
            ErrorCode.SKILL_INCOMPATIBLE: 422,
            ErrorCode.CAPABILITY_DENIED: 403,
            ErrorCode.SESSION_NOT_FOUND: 404,
            ErrorCode.RUN_NOT_FOUND: 404,
            ErrorCode.SKILL_NOT_FOUND: 404,
            ErrorCode.IDEMPOTENCY_CONFLICT: 409,
            ErrorCode.STATE_CONFLICT: 409,
            ErrorCode.INVALID_TRANSITION: 409,
            ErrorCode.INVALID_EVENT_CURSOR: 400,
            ErrorCode.RESOURCE_LIMIT: 429,
            ErrorCode.MODEL_NOT_CONFIGURED: 503,
            ErrorCode.DEPENDENCY_UNAVAILABLE: 503,
            ErrorCode.EXECUTION_FAILED: 503,
            ErrorCode.EXECUTION_UNKNOWN: 503,
        }.get(exc.code, 500)
        self._error(exc.code, exc.message, status)

    def _error(self, code: ErrorCode, message: str, status: int) -> None:
        self._json(
            status,
            {
                "error": {
                    "code": code.value,
                    "message": message,
                    "retryable": code
                    in {
                        ErrorCode.RESOURCE_LIMIT,
                        ErrorCode.MODEL_NOT_CONFIGURED,
                        ErrorCode.DEPENDENCY_UNAVAILABLE,
                        ErrorCode.EXECUTION_FAILED,
                        ErrorCode.EXECUTION_UNKNOWN,
                    },
                    "request_id": str(uuid4()),
                }
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

    def _send_headers(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self._cors_headers()
        self.end_headers()

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Idempotency-Key, X-Forge-Scope, Last-Event-ID",
        )
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    @staticmethod
    def _int_query(
        query: dict[str, list[str]], name: str, default: int, maximum: int
    ) -> int:
        raw = query.get(name, [None])[0]
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            raise DomainError(ErrorCode.INVALID_REQUEST, f"{name} must be an integer")
        return min(max(value, 0), maximum)


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
    runtime.start_worker()
    return RuntimeHTTPServer((host, port), runtime)
