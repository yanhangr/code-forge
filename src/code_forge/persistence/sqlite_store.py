"""Local SQLite Runtime store.

This is a standard-library development adapter for running the complete
Runtime on a developer machine. It mirrors the PostgreSQL DDL semantics that
the architecture requires for production, but it is intentionally not
presented as the multi-tenant PostgreSQL implementation. The production
PostgreSQL repository must be verified separately with real database
concurrency tests.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from code_forge.contracts import (
    AcceptedRun,
    DomainError,
    ErrorCode,
    Event,
    EventType,
    ExecutionContext,
    ExistingRequest,
    RunRequest,
    RunSnapshot,
    RunStatus,
    TaskOutcome,
    ToolStatus,
)
from code_forge.ports import RunRepository
from code_forge.state_machine import RunState, transition


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _now() -> str:
    return _iso(datetime.now(timezone.utc))


def _actor(context: ExecutionContext) -> str:
    return context.actor_ref or "system:runtime/api"


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SqliteRuntimeStore:
    """Synchronous store used by HTTP workers and the async RunRepository."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def _create_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS workspaces (
            id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL DEFAULT 'default',
            storage_ref TEXT NOT NULL,
            current_revision TEXT,
            date_created TEXT NOT NULL,
            created_by TEXT NOT NULL,
            date_updated TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            UNIQUE(scope_id,id)
        );
        CREATE TABLE IF NOT EXISTS workspace_revisions (
            id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            parent_id TEXT,
            manifest_digest TEXT NOT NULL,
            manifest_ref TEXT NOT NULL,
            date_created TEXT NOT NULL,
            created_by TEXT NOT NULL,
            date_updated TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            UNIQUE(scope_id,workspace_id,id)
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL DEFAULT 'default',
            workspace_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT 'New session',
            external_ref TEXT,
            thread_id TEXT NOT NULL UNIQUE,
            next_run_seq INTEGER NOT NULL DEFAULT 1,
            execution_epoch INTEGER NOT NULL DEFAULT 0,
            active_run_id TEXT,
            date_created TEXT NOT NULL,
            created_by TEXT NOT NULL,
            date_updated TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            UNIQUE(scope_id,id),
            UNIQUE(scope_id,workspace_id)
        );
        CREATE TABLE IF NOT EXISTS session_requests (
            scope_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            session_id TEXT NOT NULL,
            date_created TEXT NOT NULL,
            created_by TEXT NOT NULL,
            date_updated TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            PRIMARY KEY(scope_id,idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL DEFAULT 'default',
            session_id TEXT NOT NULL,
            run_seq INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            input TEXT NOT NULL,
            agent_ref TEXT NOT NULL,
            execution_context TEXT NOT NULL,
            config_snapshot TEXT NOT NULL,
            status TEXT NOT NULL,
            active_attempt_id TEXT,
            state_version INTEGER NOT NULL DEFAULT 0,
            task_outcome TEXT,
            wait_reason TEXT,
            output TEXT,
            error TEXT,
            cancel_requested_at TEXT,
            deadline_at TEXT,
            due_at TEXT NOT NULL,
            next_event_seq INTEGER NOT NULL DEFAULT 1,
            date_created TEXT NOT NULL,
            created_by TEXT NOT NULL,
            date_updated TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            UNIQUE(scope_id,id),
            UNIQUE(scope_id,session_id,run_seq),
            UNIQUE(scope_id,session_id,idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS run_attempts (
            id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            worker_id TEXT NOT NULL,
            lease_until TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            workspace_base_revision TEXT,
            checkpoint_ref TEXT,
            ended_at TEXT,
            date_created TEXT NOT NULL,
            created_by TEXT NOT NULL,
            date_updated TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            UNIQUE(scope_id,session_id,epoch),
            UNIQUE(scope_id,run_id,id)
        );
        CREATE TABLE IF NOT EXISTS tool_executions (
            id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            logical_call_key TEXT NOT NULL,
            tool_ref TEXT NOT NULL,
            params_digest TEXT NOT NULL,
            input_ref TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PREPARED',
            execution_profile_ref TEXT NOT NULL,
            external_operation_ref TEXT,
            process_ref TEXT,
            result_ref TEXT,
            error TEXT,
            date_created TEXT NOT NULL,
            created_by TEXT NOT NULL,
            date_updated TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            UNIQUE(scope_id,run_id,id),
            UNIQUE(scope_id,run_id,logical_call_key)
        );
        CREATE TABLE IF NOT EXISTS pending_responses (
            id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            response_key TEXT,
            response_digest TEXT,
            response_payload TEXT,
            resolved_at TEXT,
            date_created TEXT NOT NULL,
            created_by TEXT NOT NULL,
            date_updated TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            UNIQUE(scope_id,run_id,id),
            UNIQUE(scope_id,run_id,response_key)
        );
        CREATE TABLE IF NOT EXISTS run_events (
            event_id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            schema_version TEXT NOT NULL DEFAULT '1',
            type TEXT NOT NULL,
            data TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            date_created TEXT NOT NULL,
            created_by TEXT NOT NULL,
            date_updated TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            UNIQUE(scope_id,run_id,seq)
        );
        CREATE TABLE IF NOT EXISTS artifacts (
            id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            storage_ref TEXT NOT NULL,
            media_type TEXT NOT NULL,
            digest TEXT NOT NULL,
            provenance TEXT NOT NULL DEFAULT '{}',
            date_created TEXT NOT NULL,
            created_by TEXT NOT NULL,
            date_updated TEXT NOT NULL,
            updated_by TEXT NOT NULL
        );
        """
        with self._lock:
            self.conn.executescript(schema)

    @staticmethod
    def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    @staticmethod
    def _parse_json(value: str | None) -> Any:
        return json.loads(value) if value else None

    def create_session(
        self,
        scope_id: str,
        idempotency_key: str,
        title: str,
        external_ref: str | None,
        context: ExecutionContext,
    ) -> dict[str, Any]:
        now = _now()
        actor = _actor(context)
        fingerprint = _fingerprint(
            {
                "scope_id": scope_id,
                "idempotency_key": idempotency_key,
                "title": title,
                "external_ref": external_ref,
                "context": asdict(context),
            }
        )
        existing_id: str | None = None
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT session_id FROM session_requests
                WHERE scope_id = ? AND idempotency_key = ?
                """,
                (scope_id, idempotency_key),
            ).fetchone()
            if row:
                existing_id = row["session_id"]
            else:
                workspace_id = str(uuid4())
                session_id = str(uuid4())
                conn.execute(
                    """
                    INSERT INTO workspaces(
                        id,scope_id,storage_ref,date_created,created_by,date_updated,updated_by
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (workspace_id, scope_id, f"workspace:{workspace_id}", now, actor, now, actor),
                )
                conn.execute(
                    """
                    INSERT INTO sessions(
                        id,scope_id,workspace_id,title,external_ref,thread_id,
                        next_run_seq,execution_epoch,active_run_id,
                        date_created,created_by,date_updated,updated_by
                    ) VALUES (?,?,?,?,?,?,1,0,NULL,?,?,?,?)
                    """,
                    (
                        session_id,
                        scope_id,
                        workspace_id,
                        title,
                        external_ref,
                        f"thread:{session_id}",
                        now,
                        actor,
                        now,
                        actor,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO session_requests(
                        scope_id,idempotency_key,request_fingerprint,session_id,
                        date_created,created_by,date_updated,updated_by
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        scope_id,
                        idempotency_key,
                        fingerprint,
                        session_id,
                        now,
                        actor,
                        now,
                        actor,
                    ),
                )
                existing_id = session_id
        assert existing_id is not None
        return self.get_session(scope_id, existing_id)  # type: ignore[return-value]

    def get_session(self, scope_id: str, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT id,scope_id,workspace_id,title,external_ref,thread_id,
                       date_created,created_by,date_updated,updated_by
                FROM sessions WHERE scope_id = ? AND id = ?
                """,
                (scope_id, session_id),
            ).fetchone()
            if not row:
                return None
            result = self._row_dict(row)
            assert result is not None
            return result

    def require_session(self, scope_id: str, session_id: str) -> None:
        if self.get_session(scope_id, session_id) is None:
            raise DomainError(ErrorCode.SESSION_NOT_FOUND, "Session not found")

    def list_sessions(
        self, scope_id: str, cursor: str | None, limit: int
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: list[Any] = [scope_id]
        where = "scope_id = ?"
        if cursor:
            created, ident = cursor.split("|", 1)
            where += " AND (date_created > ? OR (date_created = ? AND id > ?))"
            params.extend([created, created, ident])
        params.append(limit + 1)
        with self._lock:
            rows = self.conn.execute(
                f"""
                SELECT id,scope_id,workspace_id,title,external_ref,thread_id,
                       date_created,created_by,date_updated,updated_by
                FROM sessions WHERE {where}
                ORDER BY date_created,id LIMIT ?
                """,
                params,
            ).fetchall()
        items = [dict(row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = dict(rows[limit - 1])
            next_cursor = f"{last['date_created']}|{last['id']}"
        return items, next_cursor

    def find_request(
        self, scope_id: str, session_id: str, idempotency_key: str
    ) -> ExistingRequest | None:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT id,status,state_version,request_fingerprint
                FROM runs
                WHERE scope_id = ? AND session_id = ? AND idempotency_key = ?
                """,
                (scope_id, session_id, idempotency_key),
            ).fetchone()
        if not row:
            return None
        return ExistingRequest(
            fingerprint=row["request_fingerprint"],
            run=AcceptedRun(
                id=row["id"],
                session_id=session_id,
                status=RunStatus(row["status"]),
                state_version=row["state_version"],
                reused=True,
            ),
        )

    def accept_once(
        self,
        request: RunRequest,
        idempotency_key: str,
        fingerprint: str,
        snapshot: RunSnapshot,
    ) -> AcceptedRun:
        now = _now()
        actor = _actor(request.context)
        with self._transaction() as conn:
            existing = conn.execute(
                """
                SELECT id,status,state_version,request_fingerprint
                FROM runs
                WHERE scope_id = ? AND session_id = ? AND idempotency_key = ?
                """,
                (request.context.scope_id, request.session_id, idempotency_key),
            ).fetchone()
            if existing:
                if existing["request_fingerprint"] != fingerprint:
                    raise DomainError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "Key is already bound to different input",
                    )
                return AcceptedRun(
                    id=existing["id"],
                    session_id=request.session_id,
                    status=RunStatus(existing["status"]),
                    state_version=existing["state_version"],
                    reused=True,
                )

            session = conn.execute(
                "SELECT next_run_seq FROM sessions WHERE scope_id = ? AND id = ?",
                (request.context.scope_id, request.session_id),
            ).fetchone()
            if not session:
                raise DomainError(ErrorCode.SESSION_NOT_FOUND, "Session not found")
            run_id = str(uuid4())
            run_seq = session["next_run_seq"]
            conn.execute(
                """
                INSERT INTO runs(
                    id,scope_id,session_id,run_seq,idempotency_key,request_fingerprint,
                    input,agent_ref,execution_context,config_snapshot,status,
                    active_attempt_id,state_version,task_outcome,wait_reason,output,error,
                    cancel_requested_at,deadline_at,due_at,next_event_seq,
                    date_created,created_by,date_updated,updated_by
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,NULL,0,NULL,NULL,NULL,NULL,NULL,NULL,?,1,?,?,?,?
                )
                """,
                (
                    run_id,
                    request.context.scope_id,
                    request.session_id,
                    run_seq,
                    idempotency_key,
                    fingerprint,
                    request.input,
                    request.agent_ref,
                    json.dumps(asdict(request.context), ensure_ascii=False, sort_keys=True),
                    json.dumps(asdict(snapshot), ensure_ascii=False, sort_keys=True),
                    RunStatus.QUEUED.value,
                    now,
                    now,
                    actor,
                    now,
                    actor,
                ),
            )
            self._insert_event_locked(
                conn,
                scope_id=request.context.scope_id,
                run_id=run_id,
                event_type=EventType.RUN_ACCEPTED,
                data={"status": RunStatus.QUEUED.value, "snapshot": asdict(snapshot)},
                actor=actor,
                occurred_at=now,
            )
            conn.execute(
                "UPDATE sessions SET next_run_seq = ?, date_updated = ?, updated_by = ? "
                "WHERE scope_id = ? AND id = ?",
                (run_seq + 1, now, actor, request.context.scope_id, request.session_id),
            )
        return AcceptedRun(
            id=run_id,
            session_id=request.session_id,
            status=RunStatus.QUEUED,
            state_version=0,
            reused=False,
        )

    def get_run(self, scope_id: str, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM runs WHERE scope_id = ? AND id = ?", (scope_id, run_id)
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["execution_context"] = self._parse_json(data.get("execution_context"))
        data["config_snapshot"] = self._parse_json(data.get("config_snapshot"))
        data["error"] = self._parse_json(data.get("error"))
        data["task_outcome"] = data.get("task_outcome")
        return data

    def list_runs(
        self, scope_id: str, session_id: str, cursor: str | None, limit: int
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: list[Any] = [scope_id, session_id]
        where = "scope_id = ? AND session_id = ?"
        if cursor:
            where += " AND run_seq > ?"
            params.append(int(cursor))
        params.append(limit + 1)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM runs WHERE {where} ORDER BY run_seq LIMIT ?",
                params,
            ).fetchall()
        items = [self._decode_run(dict(row)) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = str(dict(rows[limit - 1])["run_seq"])
        return items, next_cursor

    def conversation_history(
        self,
        scope_id: str,
        session_id: str,
        before_run_id: str,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        with self._lock:
            current = self.conn.execute(
                "SELECT run_seq FROM runs WHERE scope_id = ? AND session_id = ? AND id = ?",
                (scope_id, session_id, before_run_id),
            ).fetchone()
            if not current:
                return []
            rows = self.conn.execute(
                """
                SELECT id,input,output,run_seq,status
                FROM runs
                WHERE scope_id = ? AND session_id = ? AND run_seq < ?
                ORDER BY run_seq DESC
                LIMIT ?
                """,
                (scope_id, session_id, current["run_seq"], limit),
            ).fetchall()
        history = [dict(row) for row in rows]
        history.reverse()
        return history

    def _decode_run(self, data: dict[str, Any]) -> dict[str, Any]:
        data["execution_context"] = self._parse_json(data.get("execution_context"))
        data["config_snapshot"] = self._parse_json(data.get("config_snapshot"))
        data["error"] = self._parse_json(data.get("error"))
        return data

    def run_state(self, scope_id: str, run_id: str) -> RunState:
        data = self.get_run(scope_id, run_id)
        if not data:
            raise DomainError(ErrorCode.RUN_NOT_FOUND, "Run not found")
        return RunState(
            run_id=run_id,
            status=RunStatus(data["status"]),
            state_version=int(data["state_version"]),
            task_outcome=(
                TaskOutcome(data["task_outcome"]) if data.get("task_outcome") else None
            ),
            wait_reason=data.get("wait_reason"),
        )

    def claim_next_run(
        self,
        scope_id: str,
        worker_id: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        if now is None:
            now = datetime.now(timezone.utc)
        now_iso = _iso(now)
        lease_until = _iso(now + timedelta(seconds=lease_seconds))
        actor = f"system:runtime/{worker_id}"
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT r.id AS run_id, r.scope_id AS scope_id, r.session_id AS session_id,
                       r.state_version AS state_version, r.next_event_seq AS next_event_seq,
                       s.workspace_id AS workspace_id, s.execution_epoch AS execution_epoch,
                       w.current_revision AS workspace_base_revision
                FROM runs r
                JOIN sessions s ON s.scope_id = r.scope_id AND s.id = r.session_id
                JOIN workspaces w ON w.scope_id = s.scope_id AND w.id = s.workspace_id
                WHERE r.scope_id = ? AND r.status = 'QUEUED' AND r.due_at <= ?
                  AND s.active_run_id IS NULL
                ORDER BY r.run_seq
                LIMIT 1
                """,
                (scope_id, now_iso),
            ).fetchone()
            if not row:
                return None
            attempt_id = str(uuid4())
            epoch = int(row["execution_epoch"]) + 1
            conn.execute(
                """
                INSERT INTO run_attempts(
                    id,scope_id,session_id,run_id,epoch,worker_id,lease_until,heartbeat_at,
                    workspace_base_revision,checkpoint_ref,ended_at,
                    date_created,created_by,date_updated,updated_by
                ) VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,?,?,?,?)
                """,
                (
                    attempt_id,
                    scope_id,
                    row["session_id"],
                    row["run_id"],
                    epoch,
                    worker_id,
                    lease_until,
                    now_iso,
                    row["workspace_base_revision"],
                    now_iso,
                    actor,
                    now_iso,
                    actor,
                ),
            )
            conn.execute(
                """
                UPDATE sessions
                SET active_run_id = ?, execution_epoch = ?, date_updated = ?, updated_by = ?
                WHERE scope_id = ? AND id = ?
                """,
                (row["run_id"], epoch, now_iso, actor, scope_id, row["session_id"]),
            )
            conn.execute(
                """
                UPDATE runs
                SET status = ?, active_attempt_id = ?, state_version = state_version + 1,
                    due_at = ?, date_updated = ?, updated_by = ?
                WHERE scope_id = ? AND id = ? AND state_version = ?
                """,
                (
                    RunStatus.RUNNING.value,
                    attempt_id,
                    lease_until,
                    now_iso,
                    actor,
                    scope_id,
                    row["run_id"],
                    row["state_version"],
                ),
            )
            self._insert_event_locked(
                conn,
                scope_id=scope_id,
                run_id=row["run_id"],
                event_type=EventType.RUN_STARTED,
                data={"attempt_id": attempt_id},
                actor=actor,
                occurred_at=now_iso,
            )
        result = self.get_run(scope_id, row["run_id"])
        assert result is not None
        return {
            "run": result,
            "attempt_id": attempt_id,
            "epoch": epoch,
            "workspace_id": row["workspace_id"],
        }

    def transition_run(
        self,
        scope_id: str,
        run_id: str,
        *,
        expected_version: int,
        target: RunStatus,
        task_outcome: TaskOutcome | None = None,
        reason: str | None = None,
        output: str | None = None,
        error: dict[str, Any] | None = None,
        extra_data: dict[str, Any] | None = None,
        actor_ref: str | None = None,
    ) -> dict[str, Any]:
        actor = actor_ref or "system:runtime/worker"
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE scope_id = ? AND id = ?", (scope_id, run_id)
            ).fetchone()
            if not row:
                raise DomainError(ErrorCode.RUN_NOT_FOUND, "Run not found")
            before = RunState(
                run_id=run_id,
                status=RunStatus(row["status"]),
                state_version=int(row["state_version"]),
                task_outcome=(
                    TaskOutcome(row["task_outcome"]) if row["task_outcome"] else None
                ),
                wait_reason=row["wait_reason"],
            )
            changed = transition(
                before,
                target,
                expected_version=expected_version,
                task_outcome=task_outcome,
                reason=reason,
            )
            now_iso = _now()
            if not changed.changed:
                return self.get_run(scope_id, run_id)  # type: ignore[return-value]
            after = changed.after
            conn.execute(
                """
                UPDATE runs
                SET status = ?, state_version = ?, task_outcome = ?, wait_reason = ?,
                    output = COALESCE(?, output), error = COALESCE(?, error),
                    date_updated = ?, updated_by = ?
                WHERE scope_id = ? AND id = ? AND state_version = ?
                """,
                (
                    after.status.value,
                    after.state_version,
                    after.task_outcome.value if after.task_outcome else None,
                    after.wait_reason,
                    output,
                    json.dumps(error, ensure_ascii=False, sort_keys=True) if error else None,
                    now_iso,
                    actor,
                    scope_id,
                    run_id,
                    expected_version,
                ),
            )
            data = extra_data if extra_data is not None else {}
            if changed.event_type == EventType.RUN_FINISHED:
                data = {
                    "status": after.status.value,
                    "task_outcome": after.task_outcome.value if after.task_outcome else None,
                    "output": output,
                    "error": error,
                }
            elif after.status in {RunStatus.WAITING_USER, RunStatus.WAITING_EXTERNAL}:
                data = {"status": after.status.value, "reason": after.wait_reason, **data}
            elif after.status == RunStatus.QUEUED:
                data = {"status": after.status.value, **data}
            elif after.status == RunStatus.RECOVERING:
                data = {"reason": reason or "", **data}
            elif after.status == RunStatus.CANCELLING:
                data = {"status": after.status.value, **data}
            self._insert_event_locked(
                conn,
                scope_id=scope_id,
                run_id=run_id,
                event_type=changed.event_type,
                data=data,
                actor=actor,
                occurred_at=now_iso,
            )
            if after.status in {
                RunStatus.SUCCEEDED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
                RunStatus.TIMED_OUT,
            }:
                conn.execute(
                    "UPDATE run_attempts SET ended_at = ?, date_updated = ?, updated_by = ? "
                    "WHERE scope_id = ? AND id = ?",
                    (now_iso, now_iso, actor, scope_id, row["active_attempt_id"]),
                )
                conn.execute(
                    "UPDATE sessions SET active_run_id = NULL, date_updated = ?, updated_by = ? "
                    "WHERE scope_id = ? AND active_run_id = ?",
                    (now_iso, actor, scope_id, run_id),
                )
                conn.execute(
                    "UPDATE runs SET active_attempt_id = NULL, date_updated = ?, updated_by = ? "
                    "WHERE scope_id = ? AND id = ?",
                    (now_iso, actor, scope_id, run_id),
                )
        return self.get_run(scope_id, run_id)  # type: ignore[return-value]

    def cancel_run(
        self,
        scope_id: str,
        run_id: str,
        actor_ref: str | None = None,
    ) -> dict[str, Any]:
        state = self.run_state(scope_id, run_id)
        if state.status in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
        } or state.status == RunStatus.CANCELLING:
            return self.get_run(scope_id, run_id)  # type: ignore[return-value]
        actor = actor_ref or "system:runtime/api"
        current = self.transition_run(
            scope_id,
            run_id,
            expected_version=state.state_version,
            target=RunStatus.CANCELLING,
            actor_ref=actor,
        )
        current_version = int(current["state_version"])
        if state.status == RunStatus.QUEUED:
            current = self.transition_run(
                scope_id,
                run_id,
                expected_version=current_version,
                target=RunStatus.CANCELLED,
                actor_ref=actor,
            )
        return current

    def append_event(
        self,
        scope_id: str,
        run_id: str,
        event_type: EventType,
        data: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT next_event_seq FROM runs WHERE scope_id = ? AND id = ?",
                (scope_id, run_id),
            ).fetchone()
            if not row:
                raise DomainError(ErrorCode.RUN_NOT_FOUND, "Run not found")
            event = self._insert_event_locked(
                conn,
                scope_id=scope_id,
                run_id=run_id,
                event_type=event_type,
                data=data,
                actor=actor,
                occurred_at=_now(),
            )
        return event

    def _insert_event_locked(
        self,
        conn: sqlite3.Connection,
        *,
        scope_id: str,
        run_id: str,
        event_type: EventType,
        data: dict[str, Any],
        actor: str,
        occurred_at: str,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT next_event_seq FROM runs WHERE scope_id = ? AND id = ?",
            (scope_id, run_id),
        ).fetchone()
        if not row:
            raise DomainError(ErrorCode.RUN_NOT_FOUND, "Run not found")
        seq = int(row["next_event_seq"])
        event_id = str(uuid4())
        now = _now()
        conn.execute(
            """
            INSERT INTO run_events(
                event_id,scope_id,run_id,seq,schema_version,type,data,occurred_at,
                date_created,created_by,date_updated,updated_by
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                scope_id,
                run_id,
                seq,
                "1",
                event_type.value,
                json.dumps(data, ensure_ascii=False, sort_keys=True, default=_json_default),
                occurred_at,
                now,
                actor,
                now,
                actor,
            ),
        )
        conn.execute(
            "UPDATE runs SET next_event_seq = ?, date_updated = ?, updated_by = ? "
            "WHERE scope_id = ? AND id = ?",
            (seq + 1, now, actor, scope_id, run_id),
        )
        return {
            "event_id": event_id,
            "run_id": run_id,
            "seq": seq,
            "schema_version": "1",
            "type": event_type.value,
            "occurred_at": occurred_at,
            "data": data,
        }

    def list_events(
        self, scope_id: str, run_id: str, after_seq: int, limit: int
    ) -> tuple[list[dict[str, Any]], int | None]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT event_id,scope_id,run_id,seq,schema_version,type,data,occurred_at
                FROM run_events
                WHERE scope_id = ? AND run_id = ? AND seq > ?
                ORDER BY seq LIMIT ?
                """,
                (scope_id, run_id, after_seq, limit),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            data["data"] = self._parse_json(data["data"])
            events.append(data)
        next_after = events[-1]["seq"] if events else after_seq
        return events, next_after

    def event_cursor(self, scope_id: str, run_id: str) -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM run_events "
                "WHERE scope_id = ? AND run_id = ?",
                (scope_id, run_id),
            ).fetchone()
        return f"{run_id}:{row['max_seq']}"

    def get_snapshot(self, scope_id: str, run_id: str) -> dict[str, Any] | None:
        run = self.get_run(scope_id, run_id)
        if not run:
            return None
        with self._lock:
            pending_rows = self.conn.execute(
                """
                SELECT id,kind,payload,response_key,response_digest,response_payload,resolved_at
                FROM pending_responses WHERE scope_id = ? AND run_id = ? ORDER BY date_created
                """,
                (scope_id, run_id),
            ).fetchall()
        pending = []
        for row in pending_rows:
            data = dict(row)
            payload = self._parse_json(data.pop("payload")) or {}
            data["prompt"] = payload.get("prompt", "")
            data["resolved"] = data.get("resolved_at") is not None
            pending.append(data)
        return {
            "run": run,
            "event_cursor": self.event_cursor(scope_id, run_id),
            "pending_responses": pending,
        }

    def prepare_tool(
        self,
        scope_id: str,
        run_id: str,
        logical_call_key: str,
        tool_ref: str,
        params_digest: str,
        input_ref: str,
        execution_profile_ref: str,
        input_summary: str,
        actor: str,
    ) -> tuple[str, ToolStatus]:
        now = _now()
        with self._transaction() as conn:
            existing = conn.execute(
                """
                SELECT id,params_digest,status FROM tool_executions
                WHERE scope_id = ? AND run_id = ? AND logical_call_key = ?
                """,
                (scope_id, run_id, logical_call_key),
            ).fetchone()
            if existing:
                if existing["params_digest"] != params_digest:
                    raise DomainError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "Tool call key is already bound to different parameters",
                    )
                return existing["id"], ToolStatus(existing["status"])
            operation_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO tool_executions(
                    id,scope_id,run_id,logical_call_key,tool_ref,params_digest,input_ref,
                    status,execution_profile_ref,external_operation_ref,process_ref,result_ref,error,
                    date_created,created_by,date_updated,updated_by
                ) VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,?,?,?,?)
                """,
                (
                    operation_id,
                    scope_id,
                    run_id,
                    logical_call_key,
                    tool_ref,
                    params_digest,
                    input_ref,
                    ToolStatus.PREPARED.value,
                    execution_profile_ref,
                    now,
                    actor,
                    now,
                    actor,
                ),
            )
            self._insert_event_locked(
                conn,
                scope_id=scope_id,
                run_id=run_id,
                event_type=EventType.TOOL_PREPARED,
                data={
                    "operation_id": operation_id,
                    "tool_ref": tool_ref,
                    "input_summary": input_summary,
                },
                actor=actor,
                occurred_at=now,
            )
            return operation_id, ToolStatus.PREPARED

    def start_tool(self, scope_id: str, operation_id: str, actor: str) -> None:
        now = _now()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT run_id,tool_ref FROM tool_executions WHERE scope_id = ? AND id = ?",
                (scope_id, operation_id),
            ).fetchone()
            if not row:
                raise DomainError(ErrorCode.RUN_NOT_FOUND, "Tool execution not found")
            conn.execute(
                """
                UPDATE tool_executions
                SET status = ?, date_updated = ?, updated_by = ?
                WHERE scope_id = ? AND id = ?
                """,
                (ToolStatus.RUNNING.value, now, actor, scope_id, operation_id),
            )
            self._insert_event_locked(
                conn,
                scope_id=scope_id,
                run_id=row["run_id"],
                event_type=EventType.TOOL_STARTED,
                data={"operation_id": operation_id, "tool_ref": row["tool_ref"]},
                actor=actor,
                occurred_at=now,
            )

    def append_tool_output(
        self,
        scope_id: str,
        operation_id: str,
        stream: str,
        text: str,
        truncated: bool,
        actor: str,
    ) -> None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT run_id FROM tool_executions WHERE scope_id = ? AND id = ?",
                (scope_id, operation_id),
            ).fetchone()
            if not row:
                raise DomainError(ErrorCode.RUN_NOT_FOUND, "Tool execution not found")
            self._insert_event_locked(
                conn,
                scope_id=scope_id,
                run_id=row["run_id"],
                event_type=EventType.TOOL_OUTPUT,
                data={
                    "operation_id": operation_id,
                    "stream": stream,
                    "text": text,
                    "truncated": truncated,
                },
                actor=actor,
                occurred_at=_now(),
            )

    def finish_tool(
        self,
        scope_id: str,
        operation_id: str,
        status: ToolStatus,
        result_ref: str | None,
        error: dict[str, Any] | None,
        actor: str,
    ) -> None:
        now = _now()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT run_id,tool_ref FROM tool_executions WHERE scope_id = ? AND id = ?",
                (scope_id, operation_id),
            ).fetchone()
            if not row:
                raise DomainError(ErrorCode.RUN_NOT_FOUND, "Tool execution not found")
            conn.execute(
                """
                UPDATE tool_executions
                SET status = ?, result_ref = ?, error = ?,
                    date_updated = ?, updated_by = ?
                WHERE scope_id = ? AND id = ?
                """,
                (
                    status.value,
                    result_ref,
                    json.dumps(error, ensure_ascii=False, sort_keys=True) if error else None,
                    now,
                    actor,
                    scope_id,
                    operation_id,
                ),
            )
            event_type = (
                EventType.TOOL_UNKNOWN if status == ToolStatus.UNKNOWN else EventType.TOOL_FINISHED
            )
            data: dict[str, Any] = {"operation_id": operation_id}
            if status == ToolStatus.UNKNOWN:
                data["reason"] = error.get("message", "unknown") if error else "unknown"
            else:
                data.update(
                    {
                        "status": status.value,
                        "result_ref": result_ref,
                        "error": error,
                    }
                )
            self._insert_event_locked(
                conn,
                scope_id=scope_id,
                run_id=row["run_id"],
                event_type=event_type,
                data=data,
                actor=actor,
                occurred_at=now,
            )

    def respond_to_run(
        self,
        scope_id: str,
        run_id: str,
        pending_id: str,
        response_key: str,
        expected_state_version: int,
        text: str,
        actor: str,
    ) -> dict[str, Any]:
        digest = _fingerprint({"text": text})
        with self._transaction() as conn:
            pending = conn.execute(
                """
                SELECT id,response_key,response_digest,resolved_at
                FROM pending_responses
                WHERE scope_id = ? AND run_id = ? AND id = ?
                """,
                (scope_id, run_id, pending_id),
            ).fetchone()
            if not pending:
                raise DomainError(ErrorCode.RUN_NOT_FOUND, "Pending response not found")
            if pending["resolved_at"]:
                if pending["response_key"] == response_key and pending["response_digest"] == digest:
                    return self.get_run(scope_id, run_id)  # type: ignore[return-value]
                raise DomainError(ErrorCode.STATE_CONFLICT, "Response is already resolved")
            conn.execute(
                """
                UPDATE pending_responses
                SET response_key = ?, response_digest = ?, response_payload = ?,
                    resolved_at = ?, date_updated = ?, updated_by = ?
                WHERE scope_id = ? AND run_id = ? AND id = ?
                """,
                (
                    response_key,
                    digest,
                    json.dumps({"text": text}, ensure_ascii=False, sort_keys=True),
                    _now(),
                    _now(),
                    actor,
                    scope_id,
                    run_id,
                    pending_id,
                ),
            )
            row = conn.execute(
                "SELECT state_version FROM runs WHERE scope_id = ? AND id = ?",
                (scope_id, run_id),
            ).fetchone()
            if not row:
                raise DomainError(ErrorCode.RUN_NOT_FOUND, "Run not found")
            if int(row["state_version"]) != expected_state_version:
                raise DomainError(ErrorCode.STATE_CONFLICT, "Expected state version mismatch")
            self._transition_run_locked(
                conn,
                scope_id,
                run_id,
                target=RunStatus.QUEUED,
                expected_version=expected_state_version,
                actor=actor,
            )
        return self.get_run(scope_id, run_id)  # type: ignore[return-value]

    def _transition_run_locked(
        self,
        conn: sqlite3.Connection,
        scope_id: str,
        run_id: str,
        *,
        target: RunStatus,
        expected_version: int,
        task_outcome: TaskOutcome | None = None,
        reason: str | None = None,
        output: str | None = None,
        error: dict[str, Any] | None = None,
        actor: str,
    ) -> None:
        # Used only by local respond_to_run. The public transition_run keeps event
        # construction centralized; this small helper intentionally mirrors that
        # transition for the same-transaction response path.
        row = conn.execute(
            "SELECT status,state_version,task_outcome,wait_reason FROM runs "
            "WHERE scope_id = ? AND id = ?",
            (scope_id, run_id),
        ).fetchone()
        if not row:
            raise DomainError(ErrorCode.RUN_NOT_FOUND, "Run not found")
        before = RunState(
            run_id=run_id,
            status=RunStatus(row["status"]),
            state_version=int(row["state_version"]),
            task_outcome=TaskOutcome(row["task_outcome"]) if row["task_outcome"] else None,
            wait_reason=row["wait_reason"],
        )
        changed = transition(
            before,
            target,
            expected_version=expected_version,
            task_outcome=task_outcome,
            reason=reason,
        )
        if not changed.changed:
            return
        after = changed.after
        conn.execute(
            """
            UPDATE runs
            SET status = ?, state_version = ?, task_outcome = ?, wait_reason = ?,
                output = COALESCE(?, output), error = COALESCE(?, error),
                date_updated = ?, updated_by = ?
            WHERE scope_id = ? AND id = ? AND state_version = ?
            """,
            (
                after.status.value,
                after.state_version,
                after.task_outcome.value if after.task_outcome else None,
                after.wait_reason,
                output,
                json.dumps(error, ensure_ascii=False, sort_keys=True) if error else None,
                _now(),
                actor,
                scope_id,
                run_id,
                expected_version,
            ),
        )
        if after.status == RunStatus.QUEUED:
            data = {"status": after.status.value}
        else:
            data = {}
        self._insert_event_locked(
            conn,
            scope_id=scope_id,
            run_id=run_id,
            event_type=changed.event_type,
            data=data,
            actor=actor,
            occurred_at=_now(),
        )


class SqliteRunRepository(RunRepository):
    """Async RunRepository adapter for the synchronous local store."""

    def __init__(self, store: SqliteRuntimeStore):
        self.store = store

    async def find_request(
        self, scope_id: str, session_id: str, key: str
    ) -> ExistingRequest | None:
        return self.store.find_request(scope_id, session_id, key)

    async def require_session(self, scope_id: str, session_id: str) -> None:
        self.store.require_session(scope_id, session_id)

    async def accept_once(
        self, request: RunRequest, key: str, fingerprint: str, snapshot: RunSnapshot
    ) -> AcceptedRun:
        return self.store.accept_once(request, key, fingerprint, snapshot)
