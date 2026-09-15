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
    EventType,
    ExecutionContext,
    ExistingRequest,
    RunRequest,
    RunSnapshot,
    RunStatus,
    TaskOutcome,
    ToolStatus,
    UserBinding,
    WorkspaceCommit,
)
from code_forge.persistence.content_store import (
    ContentUnavailable,
    FileContentStore,
    ObjectRef,
    canonical_json,
    digest_text,
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

    SCHEMA_VERSION = 5

    def __init__(self, path: str | Path, content_store: FileContentStore | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._content = content_store or FileContentStore()
        self._legacy_root = self.path.parent
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
        with self._lock:
            version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                row["name"]
                for row in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            core_tables = {
                "workspaces",
                "workspace_revisions",
                "sessions",
                "session_requests",
                "runs",
                "run_attempts",
                "tool_executions",
                "pending_responses",
                "run_events",
                "artifacts",
            }
            if not core_tables.intersection(tables):
                self.conn.executescript(self._schema_v5())
                self.conn.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
                return
            if version < 2:
                self._migrate_v1_to_v4()
                self._migrate_v4_to_v5()
                return
            if version < 3:
                self._migrate_v2_to_v4()
                self._migrate_v4_to_v5()
                return
            if version < 4:
                self._migrate_v3_to_v4()
                self._migrate_v4_to_v5()
                return
            if version < 5:
                self._migrate_v4_to_v5()
                return
            if version != self.SCHEMA_VERSION:
                raise RuntimeError(
                    f"Unsupported SQLite schema version {version}; expected {self.SCHEMA_VERSION}"
                )
            self.conn.executescript(self._schema_v5())

    @staticmethod
    def _schema_v4() -> str:
        return """
        CREATE TABLE IF NOT EXISTS workspaces (
            id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL DEFAULT 'default',
            storage_ref TEXT NOT NULL,
            storage_root TEXT NOT NULL DEFAULT '',
            tenant_ref TEXT NOT NULL DEFAULT 'legacy-tenant:default',
            user_ref TEXT NOT NULL DEFAULT 'default',
            user_path TEXT NOT NULL DEFAULT '',
            user_rel_path TEXT NOT NULL DEFAULT '',
            project_ref TEXT NOT NULL DEFAULT 'legacy-project:default',
            project_path TEXT NOT NULL DEFAULT '',
            current_revision TEXT,
            active_attempt_id TEXT,
            workspace_epoch INTEGER NOT NULL DEFAULT 0,
            lease_until TEXT,
            heartbeat_at TEXT,
            date_created TEXT NOT NULL,
            created_by TEXT NOT NULL,
            date_updated TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            UNIQUE(scope_id,id),
            UNIQUE(scope_id,project_ref)
        );
        CREATE TABLE IF NOT EXISTS workspace_revisions (
            id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            parent_id TEXT,
            manifest_digest TEXT NOT NULL,
            manifest_ref TEXT NOT NULL,
            storage_ref TEXT NOT NULL DEFAULT '',
            date_created TEXT NOT NULL,
            created_by TEXT NOT NULL,
            date_updated TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            UNIQUE(scope_id,workspace_id,id),
            FOREIGN KEY(scope_id,workspace_id) REFERENCES workspaces(scope_id,id),
            FOREIGN KEY(scope_id,workspace_id,parent_id)
                REFERENCES workspace_revisions(scope_id,workspace_id,id)
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL DEFAULT 'default',
            workspace_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT 'New session',
            external_ref TEXT,
            thread_id TEXT NOT NULL UNIQUE,
            next_run_seq INTEGER NOT NULL DEFAULT 1,
            next_event_seq INTEGER NOT NULL DEFAULT 1,
            execution_epoch INTEGER NOT NULL DEFAULT 0,
            active_run_id TEXT,
            date_created TEXT NOT NULL,
            created_by TEXT NOT NULL,
            date_updated TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            UNIQUE(scope_id,id),
            FOREIGN KEY(scope_id,workspace_id) REFERENCES workspaces(scope_id,id)
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
            PRIMARY KEY(scope_id,idempotency_key),
            FOREIGN KEY(scope_id,session_id) REFERENCES sessions(scope_id,id)
        );
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL DEFAULT 'default',
            session_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
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
            UNIQUE(scope_id,session_id,id),
            UNIQUE(scope_id,session_id,run_seq),
            UNIQUE(scope_id,session_id,idempotency_key),
            FOREIGN KEY(scope_id,session_id) REFERENCES sessions(scope_id,id),
            FOREIGN KEY(scope_id,workspace_id) REFERENCES workspaces(scope_id,id)
        );
        CREATE TABLE IF NOT EXISTS run_attempts (
            id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            workspace_epoch INTEGER NOT NULL,
            worker_id TEXT NOT NULL,
            lease_until TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            workspace_base_revision TEXT,
            mount_spec_ref TEXT NOT NULL DEFAULT 'legacy',
            working_directory_ref TEXT NOT NULL DEFAULT '',
            checkpoint_ref TEXT,
            ended_at TEXT,
            date_created TEXT NOT NULL,
            created_by TEXT NOT NULL,
            date_updated TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            UNIQUE(scope_id,session_id,epoch),
            UNIQUE(scope_id,run_id,id),
            UNIQUE(scope_id,workspace_id,workspace_epoch),
            UNIQUE(scope_id,workspace_id,id),
            FOREIGN KEY(scope_id,session_id,run_id)
                REFERENCES runs(scope_id,session_id,id),
            FOREIGN KEY(scope_id,workspace_id) REFERENCES workspaces(scope_id,id)
        );
        CREATE TABLE IF NOT EXISTS tool_executions (
            id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            logical_call_key TEXT NOT NULL,
            tool_ref TEXT NOT NULL,
            params_digest TEXT NOT NULL,
            input_ref TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PREPARED',
            execution_profile_ref TEXT NOT NULL,
            external_operation_ref TEXT,
            process_ref TEXT,
            result_ref TEXT,
            input_revision_id TEXT,
            result_revision_id TEXT,
            error TEXT,
            date_created TEXT NOT NULL,
            created_by TEXT NOT NULL,
            date_updated TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            UNIQUE(scope_id,run_id,id),
            UNIQUE(scope_id,run_id,logical_call_key),
            FOREIGN KEY(scope_id,run_id) REFERENCES runs(scope_id,id),
            FOREIGN KEY(scope_id,workspace_id) REFERENCES workspaces(scope_id,id),
            FOREIGN KEY(scope_id,workspace_id,input_revision_id)
                REFERENCES workspace_revisions(scope_id,workspace_id,id),
            FOREIGN KEY(scope_id,workspace_id,result_revision_id)
                REFERENCES workspace_revisions(scope_id,workspace_id,id)
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
            UNIQUE(scope_id,run_id,response_key),
            FOREIGN KEY(scope_id,run_id) REFERENCES runs(scope_id,id)
        );
        CREATE TABLE IF NOT EXISTS run_events (
            event_id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            session_seq INTEGER NOT NULL,
            schema_version TEXT NOT NULL DEFAULT '1',
            type TEXT NOT NULL,
            data TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            date_created TEXT NOT NULL,
            created_by TEXT NOT NULL,
            date_updated TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            UNIQUE(scope_id,run_id,seq),
            FOREIGN KEY(scope_id,session_id,run_id)
                REFERENCES runs(scope_id,session_id,id)
        );
        CREATE TABLE IF NOT EXISTS artifacts (
            id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            storage_ref TEXT NOT NULL,
            media_type TEXT NOT NULL,
            digest TEXT NOT NULL,
            provenance TEXT NOT NULL DEFAULT '{}',
            date_created TEXT NOT NULL,
            created_by TEXT NOT NULL,
            date_updated TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            FOREIGN KEY(scope_id,run_id) REFERENCES runs(scope_id,id),
            FOREIGN KEY(scope_id,workspace_id) REFERENCES workspaces(scope_id,id)
        );
        CREATE INDEX IF NOT EXISTS idx_runs_status_due_created
            ON runs(status,due_at,date_created);
        CREATE INDEX IF NOT EXISTS idx_runs_scope_status_due_created
            ON runs(scope_id,status,due_at,date_created);
        CREATE INDEX IF NOT EXISTS idx_runs_session_status_seq
            ON runs(scope_id,session_id,status,run_seq);
        CREATE INDEX IF NOT EXISTS idx_attempts_ended_lease
            ON run_attempts(ended_at,lease_until);
        CREATE INDEX IF NOT EXISTS idx_attempts_run_ended
            ON run_attempts(scope_id,run_id,ended_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_events_scope_session_session_seq
            ON run_events(scope_id,session_id,session_seq);
        """

    @classmethod
    def _schema_v5(cls) -> str:
        """v5 externalizes message, event, snapshot and pending bodies to refs.

        PostgreSQL keeps only ``ref``/``digest``/``bytes``/``chars``; the same
        column shape is mirrored here so the development adapter exercises the
        production transaction and recovery semantics.
        """

        schema = cls._schema_v4()
        replacements = [
            (
                "            input TEXT NOT NULL,\n"
                "            agent_ref TEXT NOT NULL,\n"
                "            execution_context TEXT NOT NULL,\n"
                "            config_snapshot TEXT NOT NULL,\n",
                "            input_ref TEXT NOT NULL,\n"
                "            input_digest TEXT NOT NULL,\n"
                "            input_chars INTEGER NOT NULL,\n"
                "            agent_ref TEXT NOT NULL,\n"
                "            execution_context TEXT NOT NULL,\n"
                "            config_snapshot_ref TEXT NOT NULL,\n"
                "            config_snapshot_digest TEXT NOT NULL,\n"
                "            config_snapshot_bytes INTEGER NOT NULL,\n",
            ),
            (
                "            output TEXT,\n            error TEXT,\n",
                "            output_ref TEXT,\n"
                "            output_digest TEXT,\n"
                "            output_chars INTEGER,\n"
                "            error TEXT,\n",
            ),
            (
                "            params_digest TEXT NOT NULL,\n"
                "            input_ref TEXT NOT NULL,\n"
                "            status TEXT NOT NULL DEFAULT 'PREPARED',\n",
                "            params_digest TEXT NOT NULL,\n"
                "            input_ref TEXT NOT NULL,\n"
                "            input_digest TEXT,\n"
                "            input_bytes INTEGER,\n"
                "            status TEXT NOT NULL DEFAULT 'PREPARED',\n",
            ),
            (
                "            payload TEXT NOT NULL,\n"
                "            response_key TEXT,\n"
                "            response_digest TEXT,\n"
                "            response_payload TEXT,\n",
                "            payload_ref TEXT NOT NULL,\n"
                "            payload_digest TEXT NOT NULL,\n"
                "            payload_bytes INTEGER NOT NULL,\n"
                "            response_key TEXT,\n"
                "            response_digest TEXT,\n"
                "            response_payload_ref TEXT,\n"
                "            response_payload_digest TEXT,\n"
                "            response_payload_bytes INTEGER,\n",
            ),
            (
                "            type TEXT NOT NULL,\n"
                "            data TEXT NOT NULL,\n"
                "            occurred_at TEXT NOT NULL,\n",
                "            type TEXT NOT NULL,\n"
                "            data_ref TEXT NOT NULL,\n"
                "            data_offset INTEGER NOT NULL,\n"
                "            data_bytes INTEGER NOT NULL,\n"
                "            data_digest TEXT NOT NULL,\n"
                "            occurred_at TEXT NOT NULL,\n",
            ),
        ]
        for old, new in replacements:
            assert old in schema, f"v4 schema anchor missing: {old!r}"
            schema = schema.replace(old, new, 1)
        return schema

    def _migrate_v4_to_v5(self) -> None:
        """Externalize existing v4 bodies before dropping the large columns."""

        with self._lock:
            workspaces = [
                dict(row) for row in self.conn.execute("SELECT id,user_path FROM workspaces")
            ]
            user_paths = {row["id"]: row["user_path"] for row in workspaces}
            sessions = [
                dict(row) for row in self.conn.execute("SELECT id,workspace_id FROM sessions")
            ]
            base_by_session = {
                row["id"]: (
                    Path(user_paths[row["workspace_id"]])
                    if user_paths.get(row["workspace_id"])
                    else self._legacy_root
                )
                for row in sessions
            }
            runs = [dict(row) for row in self.conn.execute("SELECT * FROM runs")]
            events = [dict(row) for row in self.conn.execute("SELECT * FROM run_events")]
            pendings = [dict(row) for row in self.conn.execute("SELECT * FROM pending_responses")]

        add_columns = [
            "ALTER TABLE runs ADD COLUMN input_ref TEXT",
            "ALTER TABLE runs ADD COLUMN input_digest TEXT",
            "ALTER TABLE runs ADD COLUMN input_chars INTEGER",
            "ALTER TABLE runs ADD COLUMN config_snapshot_ref TEXT",
            "ALTER TABLE runs ADD COLUMN config_snapshot_digest TEXT",
            "ALTER TABLE runs ADD COLUMN config_snapshot_bytes INTEGER",
            "ALTER TABLE runs ADD COLUMN output_ref TEXT",
            "ALTER TABLE runs ADD COLUMN output_digest TEXT",
            "ALTER TABLE runs ADD COLUMN output_chars INTEGER",
            "ALTER TABLE tool_executions ADD COLUMN input_digest TEXT",
            "ALTER TABLE tool_executions ADD COLUMN input_bytes INTEGER",
            "ALTER TABLE pending_responses ADD COLUMN payload_ref TEXT",
            "ALTER TABLE pending_responses ADD COLUMN payload_digest TEXT",
            "ALTER TABLE pending_responses ADD COLUMN payload_bytes INTEGER",
            "ALTER TABLE pending_responses ADD COLUMN response_payload_ref TEXT",
            "ALTER TABLE pending_responses ADD COLUMN response_payload_digest TEXT",
            "ALTER TABLE pending_responses ADD COLUMN response_payload_bytes INTEGER",
            "ALTER TABLE run_events ADD COLUMN data_ref TEXT",
            "ALTER TABLE run_events ADD COLUMN data_offset INTEGER",
            "ALTER TABLE run_events ADD COLUMN data_bytes INTEGER",
            "ALTER TABLE run_events ADD COLUMN data_digest TEXT",
        ]
        drop_columns = [
            "ALTER TABLE runs DROP COLUMN input",
            "ALTER TABLE runs DROP COLUMN config_snapshot",
            "ALTER TABLE runs DROP COLUMN output",
            "ALTER TABLE pending_responses DROP COLUMN payload",
            "ALTER TABLE pending_responses DROP COLUMN response_payload",
            "ALTER TABLE run_events DROP COLUMN data",
        ]
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            for statement in add_columns:
                self.conn.execute(statement)
            for run in runs:
                base = base_by_session.get(run["session_id"], self._legacy_root)
                session_id = run["session_id"]
                input_ref = self._write_message(
                    base, session_id, run["id"], "user", run.get("input") or "", run["run_seq"]
                )
                snapshot_ref = self._write_snapshot(
                    base, session_id, run["id"], run.get("config_snapshot") or "{}"
                )
                output_ref = None
                if run.get("output") is not None:
                    output_ref = self._write_message(
                        base, session_id, run["id"], "assistant", run["output"], run["run_seq"]
                    )
                self.conn.execute(
                    """
                    UPDATE runs
                    SET input_ref = ?, input_digest = ?, input_chars = ?,
                        config_snapshot_ref = ?, config_snapshot_digest = ?,
                        config_snapshot_bytes = ?,
                        output_ref = ?, output_digest = ?, output_chars = ?
                    WHERE id = ?
                    """,
                    (
                        input_ref.ref,
                        input_ref.digest,
                        len(run.get("input") or ""),
                        snapshot_ref.ref,
                        snapshot_ref.digest,
                        snapshot_ref.bytes,
                        output_ref.ref if output_ref else None,
                        output_ref.digest if output_ref else None,
                        len(run["output"]) if output_ref else None,
                        run["id"],
                    ),
                )
            for event in events:
                base = base_by_session.get(event["session_id"], self._legacy_root)
                data = json.loads(event.get("data") or "{}")
                ref = self._write_event_content(base, event, data)
                self.conn.execute(
                    "UPDATE run_events SET data_ref = ?, data_offset = ?, data_bytes = ?, "
                    "data_digest = ? WHERE event_id = ?",
                    (ref.ref, ref.offset, ref.bytes, ref.digest, event["event_id"]),
                )
            for pending in pendings:
                run = next((item for item in runs if item["id"] == pending["run_id"]), None)
                if run is None:
                    continue
                base = base_by_session.get(run["session_id"], self._legacy_root)
                payload = self._write_object(
                    base,
                    f"sessions/{run['session_id']}/pending/{pending['id']}-payload.json",
                    (pending.get("payload") or "{}").encode("utf-8"),
                )
                response_ref = None
                if pending.get("response_payload") is not None:
                    response_ref = self._write_object(
                        base,
                        f"sessions/{run['session_id']}/pending/{pending['id']}-response.json",
                        pending["response_payload"].encode("utf-8"),
                    )
                self.conn.execute(
                    """
                    UPDATE pending_responses
                    SET payload_ref = ?, payload_digest = ?, payload_bytes = ?,
                        response_payload_ref = ?, response_payload_digest = ?,
                        response_payload_bytes = ?
                    WHERE id = ?
                    """,
                    (
                        payload.ref,
                        payload.digest,
                        payload.bytes,
                        response_ref.ref if response_ref else None,
                        response_ref.digest if response_ref else None,
                        response_ref.bytes if response_ref else None,
                        pending["id"],
                    ),
                )
            for statement in drop_columns:
                self.conn.execute(statement)
            self.conn.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def _write_message(
        self,
        base: Path,
        session_id: str,
        run_id: str,
        role: str,
        content: str,
        run_seq: int,
    ) -> ObjectRef:
        payload = canonical_json(
            {
                "schema_version": "1",
                "session_id": session_id,
                "run_id": run_id,
                "run_seq": run_seq,
                "role": role,
                "content": content,
            }
        ).encode("utf-8")
        return self._write_object(
            base, f"sessions/{session_id}/messages/{run_id}-{role}.json", payload
        )

    def _write_snapshot(self, base: Path, session_id: str, run_id: str, encoded: str) -> ObjectRef:
        return self._write_object(
            base, f"sessions/{session_id}/snapshots/{run_id}.json", encoded.encode("utf-8")
        )

    def _write_object(self, base: Path, relative_path: str, payload: bytes) -> ObjectRef:
        return self._content.write_object(base, relative_path, payload)

    def _content_base(self, session_row: Any) -> Path:
        try:
            user_path = session_row["user_path"]
        except (KeyError, IndexError, TypeError):
            user_path = ""
        return Path(user_path) if user_path else self._legacy_root

    def _base_for_session(self, session_id: str, conn: sqlite3.Connection | None = None) -> Path:
        connection = conn or self.conn
        row = connection.execute(
            """
            SELECT w.user_path
            FROM sessions s
            JOIN workspaces w ON w.scope_id = s.scope_id AND w.id = s.workspace_id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
        return Path(row["user_path"]) if row and row["user_path"] else self._legacy_root

    def _read_message(self, base: Path, ref: str | None) -> str | None:
        if not ref:
            return None
        try:
            payload = self._content.read_object(base, ref)
            record = json.loads(payload.decode("utf-8"))
        except ContentUnavailable as exc:
            raise self._content_unavailable(exc) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self._content_unavailable(exc) from exc
        return record.get("content", "")

    def _read_object_json(self, base: Path, ref: str | None) -> Any:
        if not ref:
            return None
        try:
            payload = self._content.read_object(base, ref)
            return json.loads(payload.decode("utf-8"))
        except ContentUnavailable as exc:
            raise self._content_unavailable(exc) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self._content_unavailable(exc) from exc

    @staticmethod
    def _content_unavailable(exc: Exception) -> DomainError:
        return DomainError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"Content is unavailable: {exc}")

    def _write_event_content(self, base: Path, event: dict[str, Any], data: dict[str, Any]):
        record = {
            "schema_version": "1",
            "record_id": event["event_id"],
            "session_id": event["session_id"],
            "run_id": event["run_id"],
            "session_seq": int(event["session_seq"]),
            "kind": "event",
            "type": event["type"],
            "occurred_at": event["occurred_at"],
            "data": data,
        }
        return self._content.append_record(base, f"sessions/{event['session_id']}", record)

    def _migrate_v1_to_v4(self) -> None:
        tables = [
            "workspaces",
            "workspace_revisions",
            "sessions",
            "session_requests",
            "runs",
            "run_attempts",
            "tool_executions",
            "pending_responses",
            "run_events",
            "artifacts",
        ]
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute("PRAGMA legacy_alter_table=ON")
        rename_sql = "\n".join(f"ALTER TABLE {table} RENAME TO {table}_v1;" for table in tables)
        drop_sql = "\n".join(f"DROP TABLE {table}_v1;" for table in reversed(tables))
        migration_sql = f"""
        BEGIN IMMEDIATE;
        {rename_sql}
        {self._schema_v4()}
        INSERT INTO workspaces(
            id,scope_id,storage_ref,storage_root,tenant_ref,user_ref,user_path,user_rel_path,
            project_ref,project_path,
            current_revision,active_attempt_id,workspace_epoch,lease_until,heartbeat_at,
            date_created,created_by,date_updated,updated_by
        )
        SELECT id,scope_id,storage_ref,'',
               'legacy-tenant:' || scope_id,
               scope_id,
               '',
               '',
               'legacy-project:' || id,
               '', current_revision, NULL, 0, NULL, NULL,
               date_created,created_by,date_updated,updated_by
        FROM workspaces_v1;

        INSERT INTO workspace_revisions(
            id,scope_id,workspace_id,parent_id,manifest_digest,manifest_ref,storage_ref,
            date_created,created_by,date_updated,updated_by
        )
        SELECT id,scope_id,workspace_id,parent_id,manifest_digest,manifest_ref,'',
               date_created,created_by,date_updated,updated_by
        FROM workspace_revisions_v1;

        INSERT INTO sessions(
            id,scope_id,workspace_id,title,external_ref,thread_id,
            next_run_seq,next_event_seq,execution_epoch,active_run_id,
            date_created,created_by,date_updated,updated_by
        )
        SELECT id,scope_id,workspace_id,title,external_ref,thread_id,
               next_run_seq,1,execution_epoch,active_run_id,
               date_created,created_by,date_updated,updated_by
        FROM sessions_v1;

        INSERT INTO session_requests(
            scope_id,idempotency_key,request_fingerprint,session_id,
            date_created,created_by,date_updated,updated_by
        )
        SELECT scope_id,idempotency_key,request_fingerprint,session_id,
               date_created,created_by,date_updated,updated_by
        FROM session_requests_v1;

        INSERT INTO runs(
            id,scope_id,session_id,workspace_id,run_seq,idempotency_key,
            request_fingerprint,input,agent_ref,execution_context,config_snapshot,status,
            active_attempt_id,state_version,task_outcome,wait_reason,output,error,
            cancel_requested_at,deadline_at,due_at,next_event_seq,
            date_created,created_by,date_updated,updated_by
        )
        SELECT r.id,r.scope_id,r.session_id,s.workspace_id,r.run_seq,
               r.idempotency_key,r.request_fingerprint,r.input,r.agent_ref,
               r.execution_context,r.config_snapshot,r.status,r.active_attempt_id,
               r.state_version,r.task_outcome,r.wait_reason,r.output,r.error,
               r.cancel_requested_at,r.deadline_at,r.due_at,r.next_event_seq,
               r.date_created,r.created_by,r.date_updated,r.updated_by
        FROM runs_v1 r
        JOIN sessions_v1 s
          ON s.scope_id=r.scope_id AND s.id=r.session_id;

        INSERT INTO run_attempts(
            id,scope_id,session_id,run_id,workspace_id,epoch,workspace_epoch,
            worker_id,lease_until,heartbeat_at,workspace_base_revision,
            mount_spec_ref,working_directory_ref,checkpoint_ref,ended_at,
            date_created,created_by,date_updated,updated_by
        )
        SELECT a.id,a.scope_id,a.session_id,a.run_id,s.workspace_id,a.epoch,a.epoch,
               a.worker_id,a.lease_until,a.heartbeat_at,a.workspace_base_revision,
               'legacy','',a.checkpoint_ref,a.ended_at,
               a.date_created,a.created_by,a.date_updated,a.updated_by
        FROM run_attempts_v1 a
        JOIN sessions_v1 s
          ON s.scope_id=a.scope_id AND s.id=a.session_id;

        INSERT INTO tool_executions(
            id,scope_id,run_id,workspace_id,logical_call_key,tool_ref,params_digest,
            input_ref,status,execution_profile_ref,external_operation_ref,process_ref,
            result_ref,input_revision_id,result_revision_id,error,
            date_created,created_by,date_updated,updated_by
        )
        SELECT t.id,t.scope_id,t.run_id,s.workspace_id,t.logical_call_key,t.tool_ref,
               t.params_digest,t.input_ref,t.status,t.execution_profile_ref,
               t.external_operation_ref,t.process_ref,t.result_ref,NULL,NULL,t.error,
               t.date_created,t.created_by,t.date_updated,t.updated_by
        FROM tool_executions_v1 t
        JOIN runs_v1 r
          ON r.scope_id=t.scope_id AND r.id=t.run_id
        JOIN sessions_v1 s
          ON s.scope_id=r.scope_id AND s.id=r.session_id;

        INSERT INTO pending_responses(
            id,scope_id,run_id,kind,payload,response_key,response_digest,
            response_payload,resolved_at,date_created,created_by,date_updated,updated_by
        )
        SELECT id,scope_id,run_id,kind,payload,response_key,response_digest,
               response_payload,resolved_at,date_created,created_by,date_updated,updated_by
        FROM pending_responses_v1;

        INSERT INTO run_events(
            event_id,scope_id,run_id,session_id,seq,session_seq,schema_version,type,data,occurred_at,
            date_created,created_by,date_updated,updated_by
        )
        SELECT e.event_id,e.scope_id,e.run_id,r.session_id,e.seq,
               ROW_NUMBER() OVER (
                   PARTITION BY e.scope_id,r.session_id
                   ORDER BY r.run_seq,e.seq
               ),
               e.schema_version,e.type,e.data,e.occurred_at,
               e.date_created,e.created_by,e.date_updated,e.updated_by
        FROM run_events_v1 e
        JOIN runs_v1 r
          ON r.scope_id=e.scope_id AND r.id=e.run_id;

        UPDATE sessions
        SET next_event_seq = 1 + COALESCE(
            (
                SELECT COUNT(*)
                FROM run_events e
                JOIN runs r
                  ON r.scope_id=e.scope_id AND r.id=e.run_id
                WHERE r.session_id=sessions.id
            ),
            0
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_events_scope_session_session_seq
            ON run_events(scope_id,session_id,session_seq);

        INSERT INTO artifacts(
            id,scope_id,run_id,workspace_id,storage_ref,media_type,digest,provenance,
            date_created,created_by,date_updated,updated_by
        )
        SELECT a.id,a.scope_id,a.run_id,s.workspace_id,a.storage_ref,a.media_type,
               a.digest,a.provenance,a.date_created,a.created_by,
               a.date_updated,a.updated_by
        FROM artifacts_v1 a
        JOIN runs_v1 r
          ON r.scope_id=a.scope_id AND r.id=a.run_id
        JOIN sessions_v1 s
          ON s.scope_id=r.scope_id AND s.id=r.session_id;

        {drop_sql}
        PRAGMA user_version=4;
        COMMIT;
        """
        try:
            self.conn.executescript(migration_sql)
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        finally:
            self.conn.execute("PRAGMA legacy_alter_table=OFF")
            self.conn.execute("PRAGMA foreign_keys=ON")

    def _migrate_v2_to_v4(self) -> None:
        """Add user-facing logical paths and Session event projection fields."""

        migration_sql = """
        BEGIN IMMEDIATE;
        ALTER TABLE workspaces
            ADD COLUMN tenant_ref TEXT NOT NULL DEFAULT 'legacy-tenant:default';
        ALTER TABLE workspaces
            ADD COLUMN user_ref TEXT NOT NULL DEFAULT 'default';
        ALTER TABLE workspaces
            ADD COLUMN user_path TEXT NOT NULL DEFAULT '';
        ALTER TABLE workspaces
            ADD COLUMN storage_root TEXT NOT NULL DEFAULT '';
        ALTER TABLE workspaces
            ADD COLUMN user_rel_path TEXT NOT NULL DEFAULT '';
        ALTER TABLE sessions
            ADD COLUMN next_event_seq INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE run_events
            ADD COLUMN session_id TEXT NOT NULL DEFAULT '';
        ALTER TABLE run_events
            ADD COLUMN session_seq INTEGER NOT NULL DEFAULT 1;
        UPDATE workspaces
        SET tenant_ref = 'legacy-tenant:' || scope_id,
            user_ref = scope_id,
            user_path = space_path;
        UPDATE run_events
        SET session_id = (
            SELECT session_id FROM runs
            WHERE runs.scope_id = run_events.scope_id AND runs.id = run_events.run_id
        );
        UPDATE run_events
        SET session_seq = (
            SELECT COUNT(*)
            FROM run_events prior
            JOIN runs prior_run
              ON prior_run.scope_id = prior.scope_id AND prior_run.id = prior.run_id
            JOIN runs current_run
              ON current_run.scope_id = run_events.scope_id
             AND current_run.id = run_events.run_id
            WHERE prior.scope_id = run_events.scope_id
              AND prior_run.session_id = current_run.session_id
              AND (
                  prior_run.run_seq < current_run.run_seq
                  OR (
                      prior_run.run_seq = current_run.run_seq
                      AND prior.seq <= run_events.seq
                  )
              )
        );
        UPDATE sessions
        SET next_event_seq = 1 + COALESCE(
            (
                SELECT COUNT(*)
                FROM run_events
                WHERE run_events.session_id = sessions.id
            ),
            0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_events_scope_session_session_seq
            ON run_events(scope_id,session_id,session_seq);
        PRAGMA user_version=4;
        COMMIT;
        """
        try:
            self.conn.executescript(migration_sql)
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def _migrate_v3_to_v4(self) -> None:
        """Add logical storage identity and Session-level event ordering."""

        migration_sql = """
        BEGIN IMMEDIATE;
        ALTER TABLE workspaces
            ADD COLUMN storage_root TEXT NOT NULL DEFAULT '';
        ALTER TABLE workspaces
            ADD COLUMN user_rel_path TEXT NOT NULL DEFAULT '';
        ALTER TABLE sessions
            ADD COLUMN next_event_seq INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE run_events
            ADD COLUMN session_id TEXT NOT NULL DEFAULT '';
        ALTER TABLE run_events
            ADD COLUMN session_seq INTEGER NOT NULL DEFAULT 1;
        UPDATE run_events
        SET session_id = (
            SELECT session_id FROM runs
            WHERE runs.scope_id = run_events.scope_id AND runs.id = run_events.run_id
        );
        UPDATE run_events
        SET session_seq = (
            SELECT COUNT(*)
            FROM run_events prior
            JOIN runs prior_run
              ON prior_run.scope_id = prior.scope_id AND prior_run.id = prior.run_id
            JOIN runs current_run
              ON current_run.scope_id = run_events.scope_id
             AND current_run.id = run_events.run_id
            WHERE prior.scope_id = run_events.scope_id
              AND prior_run.session_id = current_run.session_id
              AND (
                  prior_run.run_seq < current_run.run_seq
                  OR (
                      prior_run.run_seq = current_run.run_seq
                      AND prior.seq <= run_events.seq
                  )
              )
        );
        UPDATE sessions
        SET next_event_seq = 1 + COALESCE(
            (
                SELECT COUNT(*)
                FROM run_events
                WHERE run_events.session_id = sessions.id
            ),
            0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_events_scope_session_session_seq
            ON run_events(scope_id,session_id,session_seq);
        PRAGMA user_version=4;
        COMMIT;
        """
        try:
            self.conn.executescript(migration_sql)
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

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
                SELECT session_id,request_fingerprint FROM session_requests
                WHERE scope_id = ? AND idempotency_key = ?
                """,
                (scope_id, idempotency_key),
            ).fetchone()
            if row:
                if row["request_fingerprint"] != fingerprint:
                    raise DomainError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "Session key is already bound to a different request",
                    )
                existing_id = row["session_id"]
            else:
                session_id = str(uuid4())
                binding = context.user_binding
                if binding is not None:
                    workspace = conn.execute(
                        """
                        SELECT id,storage_ref,storage_root,tenant_ref,user_ref,user_path,
                               user_rel_path,project_path
                        FROM workspaces
                        WHERE scope_id = ? AND project_ref = ?
                        """,
                        (scope_id, binding.project_ref),
                    ).fetchone()
                    if workspace:
                        workspace_id = workspace["id"]
                        if (
                            workspace["tenant_ref"] != binding.tenant_ref
                            or workspace["user_ref"] != binding.user_ref
                        ):
                            raise DomainError(
                                ErrorCode.IDEMPOTENCY_CONFLICT,
                                "User project is already bound to a different identity",
                            )
                        if (
                            workspace["storage_ref"] != binding.project_path
                            or workspace["storage_root"] != binding.storage_root
                            or workspace["user_path"] != binding.user_path
                            or workspace["user_rel_path"] != binding.user_rel_path
                            or workspace["project_path"] != binding.project_path
                        ):
                            conn.execute(
                                """
                                UPDATE workspaces
                                SET storage_ref = ?, storage_root = ?, user_path = ?,
                                    user_rel_path = ?, project_path = ?,
                                    date_updated = ?, updated_by = ?
                                WHERE scope_id = ? AND id = ?
                                """,
                                (
                                    binding.project_path,
                                    binding.storage_root,
                                    binding.user_path,
                                    binding.user_rel_path,
                                    binding.project_path,
                                    now,
                                    actor,
                                    scope_id,
                                    workspace_id,
                                ),
                            )
                    else:
                        workspace_id = str(uuid4())
                        conn.execute(
                            """
                            INSERT INTO workspaces(
                                id,scope_id,storage_ref,storage_root,tenant_ref,user_ref,
                                user_path,user_rel_path,
                                project_ref,project_path,current_revision,
                                active_attempt_id,workspace_epoch,lease_until,heartbeat_at,
                                date_created,created_by,date_updated,updated_by
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL,0,NULL,NULL,?,?,?,?)
                            """,
                            (
                                workspace_id,
                                scope_id,
                                binding.project_path,
                                binding.storage_root,
                                binding.tenant_ref,
                                binding.user_ref,
                                binding.user_path,
                                binding.user_rel_path,
                                binding.project_ref,
                                binding.project_path,
                                now,
                                actor,
                                now,
                                actor,
                            ),
                        )
                else:
                    workspace_id = str(uuid4())
                    conn.execute(
                        """
                        INSERT INTO workspaces(
                            id,scope_id,storage_ref,storage_root,tenant_ref,user_ref,
                            user_path,user_rel_path,
                            project_ref,project_path,current_revision,
                            active_attempt_id,workspace_epoch,lease_until,heartbeat_at,
                            date_created,created_by,date_updated,updated_by
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL,0,NULL,NULL,?,?,?,?)
                        """,
                        (
                            workspace_id,
                            scope_id,
                            f"workspace:{workspace_id}",
                            "",
                            f"legacy-tenant:{scope_id}",
                            scope_id,
                            "",
                            "",
                            f"legacy-project:{workspace_id}",
                            "",
                            now,
                            actor,
                            now,
                            actor,
                        ),
                    )
                conn.execute(
                    """
                    INSERT INTO sessions(
                        id,scope_id,workspace_id,title,external_ref,thread_id,
                        next_run_seq,next_event_seq,execution_epoch,active_run_id,
                        date_created,created_by,date_updated,updated_by
                    ) VALUES (?,?,?,?,?,?,1,1,0,NULL,?,?,?,?)
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
                SELECT s.id,s.scope_id,s.workspace_id,s.title,s.external_ref,s.thread_id,
                       s.next_event_seq,s.date_created,s.created_by,s.date_updated,s.updated_by,
                       w.storage_ref,w.storage_root,w.tenant_ref,w.user_ref,w.user_path,w.user_rel_path,
                       w.project_ref,w.project_path,
                       w.current_revision,w.active_attempt_id AS workspace_active_attempt_id,
                       w.workspace_epoch,w.lease_until,w.heartbeat_at
                FROM sessions s
                JOIN workspaces w
                  ON w.scope_id = s.scope_id AND w.id = s.workspace_id
                WHERE s.scope_id = ? AND s.id = ?
                """,
                (scope_id, session_id),
            ).fetchone()
            if not row:
                return None
            result = self._row_dict(row)
            assert result is not None
            return result

    def get_session_unscoped(self, session_id: str) -> dict[str, Any] | None:
        """Resolve a globally unique Session ID for the Platform-facing projection."""

        with self._lock:
            row = self.conn.execute(
                """
                SELECT s.id,s.scope_id,s.workspace_id,s.title,s.external_ref,s.thread_id,
                       s.next_event_seq,s.date_created,s.created_by,s.date_updated,s.updated_by,
                       w.storage_ref,w.storage_root,w.tenant_ref,w.user_ref,w.user_path,
                       w.user_rel_path,w.project_ref,w.project_path,
                       w.current_revision,w.active_attempt_id AS workspace_active_attempt_id,
                       w.workspace_epoch,w.lease_until,w.heartbeat_at
                FROM sessions s
                JOIN workspaces w
                  ON w.scope_id = s.scope_id AND w.id = s.workspace_id
                WHERE s.id = ?
                """,
                (session_id,),
            ).fetchone()
        return self._row_dict(row)

    def get_workspace(self, scope_id: str, workspace_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM workspaces WHERE scope_id = ? AND id = ?",
                (scope_id, workspace_id),
            ).fetchone()
        return self._row_dict(row)

    def list_bound_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT s.id,s.scope_id,s.workspace_id,s.title,s.external_ref,s.thread_id,
                       s.next_event_seq,s.date_created,s.created_by,s.date_updated,s.updated_by,
                       w.storage_ref,w.storage_root,w.tenant_ref,w.user_ref,w.user_path,
                       w.user_rel_path,w.project_ref,w.project_path,
                       w.current_revision,w.active_attempt_id AS workspace_active_attempt_id,
                       w.workspace_epoch,w.lease_until,w.heartbeat_at
                FROM sessions s
                JOIN workspaces w
                  ON w.scope_id = s.scope_id AND w.id = s.workspace_id
                WHERE w.user_path <> '' AND w.project_path <> ''
                ORDER BY s.date_created,s.id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def require_session(self, scope_id: str, session_id: str) -> None:
        if self.get_session(scope_id, session_id) is None:
            raise DomainError(ErrorCode.SESSION_NOT_FOUND, "Session not found")

    def list_sessions(
        self, scope_id: str, cursor: str | None, limit: int
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: list[Any] = [scope_id]
        where = "s.scope_id = ?"
        if cursor:
            created, ident = cursor.split("|", 1)
            where += " AND (s.date_created > ? OR (s.date_created = ? AND s.id > ?))"
            params.extend([created, created, ident])
        params.append(limit + 1)
        with self._lock:
            rows = self.conn.execute(
                f"""
                SELECT s.id,s.scope_id,s.workspace_id,s.title,s.external_ref,s.thread_id,
                       s.next_event_seq,s.date_created,s.created_by,s.date_updated,s.updated_by,
                       w.storage_ref,w.storage_root,w.tenant_ref,w.user_ref,w.user_path,w.user_rel_path,
                       w.project_ref,w.project_path,
                       w.current_revision,w.active_attempt_id AS workspace_active_attempt_id,
                       w.workspace_epoch,w.lease_until,w.heartbeat_at
                FROM sessions s
                JOIN workspaces w
                  ON w.scope_id = s.scope_id AND w.id = s.workspace_id
                WHERE {where}
                ORDER BY s.date_created,s.id LIMIT ?
                """,
                params,
            ).fetchall()
        items = [dict(row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = dict(rows[limit - 1])
            next_cursor = f"{last['date_created']}|{last['id']}"
        return items, next_cursor

    def list_sessions_public(
        self,
        *,
        user_rel_path: str | None,
        project_ref: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: list[Any] = []
        clauses: list[str] = []
        if user_rel_path:
            clauses.append("w.user_rel_path = ?")
            params.append(user_rel_path)
        if project_ref:
            clauses.append("w.project_ref = ?")
            params.append(project_ref)
        if cursor:
            created, ident = cursor.split("|", 1)
            clauses.append("(s.date_created > ? OR (s.date_created = ? AND s.id > ?))")
            params.extend([created, created, ident])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit + 1)
        with self._lock:
            rows = self.conn.execute(
                f"""
                SELECT s.id,s.scope_id,s.workspace_id,s.title,s.external_ref,s.thread_id,
                       s.next_event_seq,s.date_created,s.created_by,s.date_updated,s.updated_by,
                       w.storage_ref,w.storage_root,w.tenant_ref,w.user_ref,w.user_path,
                       w.user_rel_path,w.project_ref,w.project_path,
                       w.current_revision,w.active_attempt_id AS workspace_active_attempt_id,
                       w.workspace_epoch,w.lease_until,w.heartbeat_at
                FROM sessions s
                JOIN workspaces w
                  ON w.scope_id = s.scope_id AND w.id = s.workspace_id
                {where}
                ORDER BY s.date_created,s.id LIMIT ?
                """,
                params,
            ).fetchall()
        items = [dict(row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = dict(rows[limit - 1])
            next_cursor = f"{last['date_created']}|{last['id']}"
        return items, next_cursor

    def update_session_title(self, session_id: str, title: str, actor: str) -> dict[str, Any]:
        now = _now()
        with self._transaction() as conn:
            updated = conn.execute(
                """
                UPDATE sessions
                SET title = ?, date_updated = ?, updated_by = ?
                WHERE id = ?
                """,
                (title, now, actor, session_id),
            ).rowcount
            if updated != 1:
                raise DomainError(ErrorCode.SESSION_NOT_FOUND, "Session not found")
        result = self.get_session_unscoped(session_id)
        assert result is not None
        return result

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
        reject_if_active: bool = False,
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
                """
                SELECT s.next_run_seq,s.workspace_id,
                       w.storage_root,w.tenant_ref,w.user_ref,w.user_path,w.user_rel_path,
                       w.project_ref,w.project_path
                FROM sessions s
                JOIN workspaces w
                  ON w.scope_id = s.scope_id AND w.id = s.workspace_id
                WHERE s.scope_id = ? AND s.id = ?
                """,
                (request.context.scope_id, request.session_id),
            ).fetchone()
            if not session:
                raise DomainError(ErrorCode.SESSION_NOT_FOUND, "Session not found")
            self._require_session_binding(session, request.context.user_binding)
            if reject_if_active:
                active = conn.execute(
                    """
                    SELECT 1
                    FROM runs
                    WHERE scope_id = ? AND session_id = ?
                      AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED','TIMED_OUT')
                    LIMIT 1
                    """,
                    (request.context.scope_id, request.session_id),
                ).fetchone()
                if active:
                    raise DomainError(
                        ErrorCode.MESSAGE_BUSY,
                        "Session already has a running message",
                    )
            run_id = str(uuid4())
            run_seq = session["next_run_seq"]
            base = self._content_base(session)
            input_ref = self._write_message(
                base, request.session_id, run_id, "user", request.input, run_seq
            )
            snapshot_ref = self._write_snapshot(
                base,
                request.session_id,
                run_id,
                json.dumps(asdict(snapshot), ensure_ascii=False, sort_keys=True),
            )
            conn.execute(
                """
                INSERT INTO runs(
                    id,scope_id,session_id,workspace_id,run_seq,idempotency_key,request_fingerprint,
                    input_ref,input_digest,input_chars,agent_ref,execution_context,
                    config_snapshot_ref,config_snapshot_digest,config_snapshot_bytes,status,
                    active_attempt_id,state_version,task_outcome,wait_reason,
                    output_ref,output_digest,output_chars,error,
                    cancel_requested_at,deadline_at,due_at,next_event_seq,
                    date_created,created_by,date_updated,updated_by
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,0,NULL,NULL,NULL,NULL,NULL,NULL,
                    NULL,NULL,?,1,?,?,?,?
                )
                """,
                (
                    run_id,
                    request.context.scope_id,
                    request.session_id,
                    session["workspace_id"],
                    run_seq,
                    idempotency_key,
                    fingerprint,
                    input_ref.ref,
                    input_ref.digest,
                    len(request.input),
                    request.agent_ref,
                    json.dumps(asdict(request.context), ensure_ascii=False, sort_keys=True),
                    snapshot_ref.ref,
                    snapshot_ref.digest,
                    snapshot_ref.bytes,
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
                data={
                    "status": RunStatus.QUEUED.value,
                    "snapshot_ref": {
                        "ref": snapshot_ref.ref,
                        "bytes": snapshot_ref.bytes,
                        "digest": snapshot_ref.digest,
                    },
                },
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

    @staticmethod
    def _require_session_binding(session: sqlite3.Row, binding: UserBinding | None) -> None:
        stored = (
            UserBinding(
                scope_id=session["user_ref"],
                tenant_ref=session["tenant_ref"],
                user_ref=session["user_ref"],
                user_path=session["user_path"],
                project_ref=session["project_ref"],
                project_path=session["project_path"],
                storage_root=session["storage_root"],
                user_rel_path=session["user_rel_path"],
            )
            if session["project_path"]
            else None
        )
        if binding is None:
            if stored is not None:
                raise DomainError(
                    ErrorCode.INVALID_REQUEST,
                    "Session requires its Platform user binding",
                )
            return
        if stored is None or stored != binding:
            raise DomainError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "Run user binding does not match its Session",
            )

    def get_run(self, scope_id: str, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM runs WHERE scope_id = ? AND id = ?", (scope_id, run_id)
            ).fetchone()
        if not row:
            return None
        data = self._decode_run(dict(row))
        data["task_outcome"] = data.get("task_outcome")
        return data

    def get_run_unscoped(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._decode_run(dict(row)) if row else None

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

    def list_messages_public(
        self,
        session_id: str,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: list[Any] = [session_id]
        where = "session_id = ?"
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
        next_cursor = str(dict(rows[limit - 1])["run_seq"]) if len(rows) > limit else None
        return items, next_cursor

    def get_current_message(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM runs WHERE session_id = ? ORDER BY run_seq DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return self._decode_run(dict(row)) if row else None

    def get_pending_reply(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT p.id,p.run_id,p.kind,p.payload_ref
                FROM pending_responses p
                JOIN runs r
                  ON r.scope_id = p.scope_id AND r.id = p.run_id
                WHERE r.session_id = ? AND p.resolved_at IS NULL
                ORDER BY r.run_seq DESC, p.date_created DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if not row:
                return None
            base = self._base_for_session(session_id)
            payload = self._read_object_json(base, row["payload_ref"]) or {}
        return {
            "pending_id": row["id"],
            "message_id": row["run_id"],
            "kind": row["kind"],
            "prompt": payload.get("prompt", ""),
        }

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
                SELECT id,input_ref,output_ref,run_seq,status
                FROM runs
                WHERE scope_id = ? AND session_id = ? AND run_seq < ?
                ORDER BY run_seq DESC
                LIMIT ?
                """,
                (scope_id, session_id, current["run_seq"], limit),
            ).fetchall()
            base = self._base_for_session(session_id)
            history = [
                {
                    "id": row["id"],
                    "run_seq": row["run_seq"],
                    "status": row["status"],
                    "input": self._read_message(base, row["input_ref"]),
                    "output": self._read_message(base, row["output_ref"]),
                }
                for row in rows
            ]
        history.reverse()
        return history

    def _decode_run(self, data: dict[str, Any]) -> dict[str, Any]:
        data["execution_context"] = self._parse_json(data.get("execution_context"))
        base = self._base_for_session(data["session_id"])
        data["config_snapshot"] = self._read_object_json(base, data.get("config_snapshot_ref"))
        data["input"] = self._read_message(base, data.get("input_ref"))
        data["output"] = self._read_message(base, data.get("output_ref"))
        data["error"] = self._parse_json(data.get("error"))
        for key in (
            "input_ref",
            "input_digest",
            "input_chars",
            "config_snapshot_ref",
            "config_snapshot_digest",
            "config_snapshot_bytes",
            "output_ref",
            "output_digest",
            "output_chars",
        ):
            data.pop(key, None)
        return data

    def run_state(self, scope_id: str, run_id: str) -> RunState:
        data = self.get_run(scope_id, run_id)
        if not data:
            raise DomainError(ErrorCode.RUN_NOT_FOUND, "Run not found")
        return RunState(
            run_id=run_id,
            status=RunStatus(data["status"]),
            state_version=int(data["state_version"]),
            task_outcome=(TaskOutcome(data["task_outcome"]) if data.get("task_outcome") else None),
            wait_reason=data.get("wait_reason"),
        )

    def claim_next_run(
        self,
        scope_id: str | None,
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
            scope_clause = ""
            params: list[Any] = [now_iso]
            if scope_id is not None:
                scope_clause = "AND r.scope_id = ?"
                params.append(scope_id)
            row = conn.execute(
                f"""
                SELECT r.id AS run_id, r.scope_id AS scope_id, r.session_id AS session_id,
                       r.state_version AS state_version, r.next_event_seq AS next_event_seq,
                       s.workspace_id AS workspace_id, s.execution_epoch AS execution_epoch,
                       w.current_revision AS workspace_base_revision,
                       w.workspace_epoch AS workspace_epoch,
                       w.storage_root AS storage_root,
                       w.tenant_ref AS tenant_ref, w.user_ref AS user_ref,
                       w.user_path AS user_path, w.project_ref AS project_ref,
                       w.user_rel_path AS user_rel_path,
                       w.project_path AS project_path
                FROM runs r
                JOIN sessions s ON s.scope_id = r.scope_id AND s.id = r.session_id
                JOIN workspaces w ON w.scope_id = s.scope_id AND w.id = s.workspace_id
                WHERE r.status = 'QUEUED' AND r.due_at <= ?
                  AND s.active_run_id IS NULL
                  AND w.active_attempt_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM runs prior
                      WHERE prior.scope_id = r.scope_id
                        AND prior.session_id = r.session_id
                        AND prior.status = 'QUEUED'
                        AND prior.run_seq < r.run_seq
                  )
                  {scope_clause}
                ORDER BY r.due_at, r.date_created, r.run_seq
                LIMIT 1
                """,
                params,
            ).fetchone()
            if not row:
                return None
            attempt_id = str(uuid4())
            epoch = int(row["execution_epoch"]) + 1
            workspace_epoch = int(row["workspace_epoch"]) + 1
            binding = None
            if row["project_path"]:
                binding = UserBinding(
                    scope_id=row["scope_id"],
                    tenant_ref=row["tenant_ref"],
                    user_ref=row["user_ref"],
                    user_path=row["user_path"],
                    project_ref=row["project_ref"],
                    project_path=row["project_path"],
                    storage_root=row["storage_root"],
                    user_rel_path=row["user_rel_path"],
                )
            conn.execute(
                """
                INSERT INTO run_attempts(
                    id,scope_id,session_id,run_id,workspace_id,epoch,workspace_epoch,
                    worker_id,lease_until,heartbeat_at,workspace_base_revision,
                    mount_spec_ref,working_directory_ref,checkpoint_ref,ended_at,
                    date_created,created_by,date_updated,updated_by
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?,?,?)
                """,
                (
                    attempt_id,
                    row["scope_id"],
                    row["session_id"],
                    row["run_id"],
                    row["workspace_id"],
                    epoch,
                    workspace_epoch,
                    worker_id,
                    lease_until,
                    now_iso,
                    row["workspace_base_revision"],
                    f"project:{row['project_ref']}" if binding else "legacy",
                    row["project_path"],
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
                (
                    row["run_id"],
                    epoch,
                    now_iso,
                    actor,
                    row["scope_id"],
                    row["session_id"],
                ),
            )
            updated = conn.execute(
                """
                UPDATE workspaces
                SET active_attempt_id = ?, workspace_epoch = ?,
                    lease_until = ?, heartbeat_at = ?,
                    date_updated = ?, updated_by = ?
                WHERE scope_id = ? AND id = ?
                  AND workspace_epoch = ? AND active_attempt_id IS NULL
                """,
                (
                    attempt_id,
                    workspace_epoch,
                    lease_until,
                    now_iso,
                    now_iso,
                    actor,
                    row["scope_id"],
                    row["workspace_id"],
                    row["workspace_epoch"],
                ),
            ).rowcount
            if updated != 1:
                raise DomainError(ErrorCode.STATE_CONFLICT, "Workspace lease conflict")
            updated = conn.execute(
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
                    row["scope_id"],
                    row["run_id"],
                    row["state_version"],
                ),
            ).rowcount
            if updated != 1:
                raise DomainError(ErrorCode.STATE_CONFLICT, "Run state conflict while claiming")
            self._insert_event_locked(
                conn,
                scope_id=row["scope_id"],
                run_id=row["run_id"],
                event_type=EventType.RUN_STARTED,
                data={"attempt_id": attempt_id},
                actor=actor,
                occurred_at=now_iso,
            )
        result = self.get_run(row["scope_id"], row["run_id"])
        assert result is not None
        return {
            "run": result,
            "attempt_id": attempt_id,
            "epoch": epoch,
            "workspace_id": row["workspace_id"],
            "workspace_epoch": workspace_epoch,
            "user_binding": binding,
            "working_directory": row["project_path"] or None,
        }

    def record_workspace_revision(
        self,
        scope_id: str,
        workspace_id: str,
        attempt_id: str,
        workspace_epoch: int,
        commit: WorkspaceCommit,
        actor: str,
    ) -> None:
        now = _now()
        with self._transaction() as conn:
            workspace = conn.execute(
                """
                SELECT current_revision,active_attempt_id,workspace_epoch,lease_until
                FROM workspaces
                WHERE scope_id = ? AND id = ?
                """,
                (scope_id, workspace_id),
            ).fetchone()
            if not workspace:
                raise DomainError(ErrorCode.RUN_NOT_FOUND, "Workspace not found")
            if (
                workspace["active_attempt_id"] != attempt_id
                or int(workspace["workspace_epoch"]) != workspace_epoch
            ):
                raise DomainError(ErrorCode.STATE_CONFLICT, "Workspace lease is stale")
            if workspace["lease_until"] and workspace["lease_until"] <= now:
                raise DomainError(ErrorCode.STATE_CONFLICT, "Workspace lease has expired")
            parent_id = workspace["current_revision"]
            conn.execute(
                """
                INSERT INTO workspace_revisions(
                    id,scope_id,workspace_id,parent_id,manifest_digest,manifest_ref,storage_ref,
                    date_created,created_by,date_updated,updated_by
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    commit.revision_id,
                    scope_id,
                    workspace_id,
                    parent_id,
                    commit.manifest_digest,
                    commit.manifest_ref,
                    commit.storage_ref,
                    now,
                    actor,
                    now,
                    actor,
                ),
            )
            updated = conn.execute(
                """
                UPDATE workspaces
                SET current_revision = ?, date_updated = ?, updated_by = ?
                WHERE scope_id = ? AND id = ?
                  AND current_revision IS ? AND active_attempt_id = ?
                  AND workspace_epoch = ?
                """,
                (
                    commit.revision_id,
                    now,
                    actor,
                    scope_id,
                    workspace_id,
                    parent_id,
                    attempt_id,
                    workspace_epoch,
                ),
            ).rowcount
            if updated != 1:
                raise DomainError(ErrorCode.STATE_CONFLICT, "Workspace revision CAS failed")

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
                task_outcome=(TaskOutcome(row["task_outcome"]) if row["task_outcome"] else None),
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
            output_ref = None
            if output is not None:
                base = self._base_for_session(row["session_id"], conn)
                output_ref = self._write_message(
                    base, row["session_id"], run_id, "assistant", output, int(row["run_seq"])
                )
            conn.execute(
                """
                UPDATE runs
                SET status = ?, state_version = ?, task_outcome = ?, wait_reason = ?,
                    output_ref = COALESCE(?, output_ref),
                    output_digest = COALESCE(?, output_digest),
                    output_chars = COALESCE(?, output_chars),
                    error = COALESCE(?, error),
                    date_updated = ?, updated_by = ?
                WHERE scope_id = ? AND id = ? AND state_version = ?
                """,
                (
                    after.status.value,
                    after.state_version,
                    after.task_outcome.value if after.task_outcome else None,
                    after.wait_reason,
                    output_ref.ref if output_ref else None,
                    output_ref.digest if output_ref else None,
                    len(output) if output is not None else None,
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
                    "output_ref": output_ref.ref if output_ref else None,
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
                    """
                    UPDATE workspaces
                    SET active_attempt_id = NULL, lease_until = NULL, heartbeat_at = NULL,
                        date_updated = ?, updated_by = ?
                    WHERE scope_id = ? AND id = ? AND active_attempt_id = ?
                    """,
                    (
                        now_iso,
                        actor,
                        scope_id,
                        row["workspace_id"],
                        row["active_attempt_id"],
                    ),
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
        if (
            state.status
            in {
                RunStatus.SUCCEEDED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
                RunStatus.TIMED_OUT,
            }
            or state.status == RunStatus.CANCELLING
        ):
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
            """
            SELECT r.session_id,r.run_seq,r.next_event_seq,
                   s.next_event_seq AS session_next_event_seq
            FROM runs r
            JOIN sessions s
              ON s.scope_id = r.scope_id AND s.id = r.session_id
            WHERE r.scope_id = ? AND r.id = ?
            """,
            (scope_id, run_id),
        ).fetchone()
        if not row:
            raise DomainError(ErrorCode.RUN_NOT_FOUND, "Run not found")
        seq = int(row["next_event_seq"])
        session_seq = int(row["session_next_event_seq"])
        event_id = str(uuid4())
        now = _now()
        base = self._base_for_session(row["session_id"], conn)
        record = {
            "schema_version": "1",
            "record_id": event_id,
            "session_id": row["session_id"],
            "run_id": run_id,
            "run_seq": int(row["run_seq"]),
            "session_seq": session_seq,
            "kind": "event",
            "type": event_type.value,
            "occurred_at": occurred_at,
            "actor_ref": actor,
            "data": data,
        }
        content_ref = self._content.append_record(base, f"sessions/{row['session_id']}", record)
        conn.execute(
            """
            INSERT INTO run_events(
                event_id,scope_id,run_id,session_id,seq,session_seq,
                schema_version,type,data_ref,data_offset,data_bytes,data_digest,occurred_at,
                date_created,created_by,date_updated,updated_by
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                scope_id,
                run_id,
                row["session_id"],
                seq,
                session_seq,
                "1",
                event_type.value,
                content_ref.ref,
                content_ref.offset,
                content_ref.bytes,
                content_ref.digest,
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
        conn.execute(
            "UPDATE sessions SET next_event_seq = ?, date_updated = ?, updated_by = ? "
            "WHERE scope_id = ? AND id = ?",
            (session_seq + 1, now, actor, scope_id, row["session_id"]),
        )
        return {
            "event_id": event_id,
            "run_id": run_id,
            "session_id": row["session_id"],
            "seq": seq,
            "session_seq": session_seq,
            "schema_version": "1",
            "type": event_type.value,
            "occurred_at": occurred_at,
            "data": data,
        }

    def _event_payload(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        base = self._base_for_session(row["session_id"])
        try:
            record = self._content.read_verified(
                base,
                row["data_ref"],
                int(row["data_offset"]),
                int(row["data_bytes"]),
                row["data_digest"],
            )
        except ContentUnavailable as exc:
            raise self._content_unavailable(exc) from exc
        data = dict(record.get("data") or {})
        output_ref = data.pop("output_ref", None)
        if output_ref and data.get("output") is None:
            data["output"] = self._read_message(base, output_ref)
        return data

    def _event_view(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = self._event_payload(row)
        keys = {
            "event_id",
            "scope_id",
            "run_id",
            "session_id",
            "seq",
            "session_seq",
            "schema_version",
            "type",
            "occurred_at",
        }
        item = {key: row[key] for key in row.keys() if key in keys}
        item["data"] = payload
        return item

    def list_events(
        self, scope_id: str, run_id: str, after_seq: int, limit: int
    ) -> tuple[list[dict[str, Any]], int | None]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT event_id,scope_id,run_id,session_id,seq,schema_version,type,
                       data_ref,data_offset,data_bytes,data_digest,occurred_at
                FROM run_events
                WHERE scope_id = ? AND run_id = ? AND seq > ?
                ORDER BY seq LIMIT ?
                """,
                (scope_id, run_id, after_seq, limit),
            ).fetchall()
            events = [self._event_view(row) for row in rows]
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

    def list_session_events(
        self,
        session_id: str,
        after_seq: int,
        limit: int,
    ) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT event_id,scope_id,run_id,session_id,seq,session_seq,
                       schema_version,type,data_ref,data_offset,data_bytes,data_digest,occurred_at
                FROM run_events
                WHERE session_id = ? AND session_seq > ?
                ORDER BY session_seq LIMIT ?
                """,
                (session_id, after_seq, limit),
            ).fetchall()
            events = [self._event_view(row) for row in rows]
        next_after = int(events[-1]["session_seq"]) if events else after_seq
        return events, next_after

    def session_event_cursor(self, session_id: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(session_seq), 0) AS max_seq FROM run_events "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["max_seq"])

    def get_snapshot(self, scope_id: str, run_id: str) -> dict[str, Any] | None:
        run = self.get_run(scope_id, run_id)
        if not run:
            return None
        with self._lock:
            pending_rows = self.conn.execute(
                """
                SELECT id,kind,payload_ref,response_key,response_digest,
                       response_payload_ref,resolved_at
                FROM pending_responses WHERE scope_id = ? AND run_id = ? ORDER BY date_created
                """,
                (scope_id, run_id),
            ).fetchall()
            base = self._base_for_session(run["session_id"])
            pending = []
            for row in pending_rows:
                data = dict(row)
                payload = self._read_object_json(base, data.pop("payload_ref")) or {}
                data.pop("response_payload_ref", None)
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
        workspace_id: str | None = None,
        input_revision_id: str | None = None,
        input_text: str = "",
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
            run = conn.execute(
                """
                SELECT r.workspace_id,w.current_revision
                FROM runs r
                JOIN workspaces w
                  ON w.scope_id = r.scope_id AND w.id = r.workspace_id
                WHERE r.scope_id = ? AND r.id = ?
                """,
                (scope_id, run_id),
            ).fetchone()
            if not run:
                raise DomainError(ErrorCode.RUN_NOT_FOUND, "Run not found")
            workspace_id = workspace_id or run["workspace_id"]
            if workspace_id != run["workspace_id"]:
                raise DomainError(ErrorCode.STATE_CONFLICT, "Tool workspace mismatch")
            input_revision_id = input_revision_id or run["current_revision"]
            operation_id = str(uuid4())
            input_digest = digest_text(input_text) if input_text else None
            input_bytes = len(input_text.encode("utf-8")) if input_text else None
            conn.execute(
                """
                INSERT INTO tool_executions(
                    id,scope_id,run_id,workspace_id,logical_call_key,tool_ref,params_digest,input_ref,
                    input_digest,input_bytes,
                    status,execution_profile_ref,external_operation_ref,process_ref,result_ref,
                    input_revision_id,result_revision_id,error,
                    date_created,created_by,date_updated,updated_by
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,NULL,NULL,?,?,?,?)
                """,
                (
                    operation_id,
                    scope_id,
                    run_id,
                    workspace_id,
                    logical_call_key,
                    tool_ref,
                    params_digest,
                    input_ref,
                    input_digest,
                    input_bytes,
                    ToolStatus.PREPARED.value,
                    execution_profile_ref,
                    input_revision_id,
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
                    "input": input_text,
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
        result_revision_id: str | None = None,
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
                SET status = ?, result_ref = ?, result_revision_id = ?, error = ?,
                    date_updated = ?, updated_by = ?
                WHERE scope_id = ? AND id = ?
                """,
                (
                    status.value,
                    result_ref,
                    result_revision_id,
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
                SELECT p.id,p.response_key,p.response_digest,p.resolved_at,r.session_id
                FROM pending_responses p
                JOIN runs r ON r.scope_id = p.scope_id AND r.id = p.run_id
                WHERE p.scope_id = ? AND p.run_id = ? AND p.id = ?
                """,
                (scope_id, run_id, pending_id),
            ).fetchone()
            if not pending:
                raise DomainError(ErrorCode.RUN_NOT_FOUND, "Pending response not found")
            if pending["resolved_at"]:
                if pending["response_key"] == response_key and pending["response_digest"] == digest:
                    return self.get_run(scope_id, run_id)  # type: ignore[return-value]
                raise DomainError(ErrorCode.STATE_CONFLICT, "Response is already resolved")
            base = self._base_for_session(pending["session_id"], conn)
            response_ref = self._write_object(
                base,
                f"sessions/{pending['session_id']}/pending/{pending_id}-response.json",
                json.dumps({"text": text}, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            )
            conn.execute(
                """
                UPDATE pending_responses
                SET response_key = ?, response_digest = ?,
                    response_payload_ref = ?, response_payload_digest = ?,
                    response_payload_bytes = ?,
                    resolved_at = ?, date_updated = ?, updated_by = ?
                WHERE scope_id = ? AND run_id = ? AND id = ?
                """,
                (
                    response_key,
                    digest,
                    response_ref.ref,
                    response_ref.digest,
                    response_ref.bytes,
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
        output_ref = None
        if output is not None:
            session_row = conn.execute(
                "SELECT session_id,run_seq FROM runs WHERE scope_id = ? AND id = ?",
                (scope_id, run_id),
            ).fetchone()
            base = self._base_for_session(session_row["session_id"], conn)
            output_ref = self._write_message(
                base,
                session_row["session_id"],
                run_id,
                "assistant",
                output,
                int(session_row["run_seq"]),
            )
        conn.execute(
            """
            UPDATE runs
            SET status = ?, state_version = ?, task_outcome = ?, wait_reason = ?,
                output_ref = COALESCE(?, output_ref),
                output_digest = COALESCE(?, output_digest),
                output_chars = COALESCE(?, output_chars),
                error = COALESCE(?, error),
                date_updated = ?, updated_by = ?
            WHERE scope_id = ? AND id = ? AND state_version = ?
            """,
            (
                after.status.value,
                after.state_version,
                after.task_outcome.value if after.task_outcome else None,
                after.wait_reason,
                output_ref.ref if output_ref else None,
                output_ref.digest if output_ref else None,
                len(output) if output is not None else None,
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
        self,
        request: RunRequest,
        key: str,
        fingerprint: str,
        snapshot: RunSnapshot,
        reject_if_active: bool = False,
    ) -> AcceptedRun:
        return self.store.accept_once(
            request,
            key,
            fingerprint,
            snapshot,
            reject_if_active=reject_if_active,
        )
