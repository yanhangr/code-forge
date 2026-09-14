"""Verify in-place migration from the pre-space SQLite schema."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from code_forge.persistence.sqlite_store import SqliteRuntimeStore

V1_SCHEMA = """
CREATE TABLE workspaces (
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
CREATE TABLE workspace_revisions (
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
CREATE TABLE sessions (
 id TEXT PRIMARY KEY,
 scope_id TEXT NOT NULL DEFAULT 'default',
 workspace_id TEXT NOT NULL,
 title TEXT NOT NULL,
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
CREATE TABLE session_requests (
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
CREATE TABLE runs (
 id TEXT PRIMARY KEY,
 scope_id TEXT NOT NULL,
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
CREATE TABLE run_attempts (
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
CREATE TABLE tool_executions (
 id TEXT PRIMARY KEY,
 scope_id TEXT NOT NULL,
 run_id TEXT NOT NULL,
 logical_call_key TEXT NOT NULL,
 tool_ref TEXT NOT NULL,
 params_digest TEXT NOT NULL,
 input_ref TEXT NOT NULL,
 status TEXT NOT NULL,
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
CREATE TABLE pending_responses (
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
CREATE TABLE run_events (
 event_id TEXT PRIMARY KEY,
 scope_id TEXT NOT NULL,
 run_id TEXT NOT NULL,
 seq INTEGER NOT NULL,
 schema_version TEXT NOT NULL,
 type TEXT NOT NULL,
 data TEXT NOT NULL,
 occurred_at TEXT NOT NULL,
 date_created TEXT NOT NULL,
 created_by TEXT NOT NULL,
 date_updated TEXT NOT NULL,
 updated_by TEXT NOT NULL,
 UNIQUE(scope_id,run_id,seq)
);
CREATE TABLE artifacts (
 id TEXT PRIMARY KEY,
 scope_id TEXT NOT NULL,
 run_id TEXT NOT NULL,
 storage_ref TEXT NOT NULL,
 media_type TEXT NOT NULL,
 digest TEXT NOT NULL,
 provenance TEXT NOT NULL,
 date_created TEXT NOT NULL,
 created_by TEXT NOT NULL,
 date_updated TEXT NOT NULL,
 updated_by TEXT NOT NULL
);
"""


class SqliteMigrationTests(unittest.TestCase):
    def test_v1_database_is_upgraded_without_losing_legacy_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            conn = sqlite3.connect(path)
            conn.executescript(V1_SCHEMA)
            conn.execute(
                """
                INSERT INTO workspaces(
                    id,scope_id,storage_ref,current_revision,
                    date_created,created_by,date_updated,updated_by
                ) VALUES (?,?,?,NULL,?,?,?,?)
                """,
                (
                    "workspace-1",
                    "default",
                    "workspace:workspace-1",
                    "now",
                    "tester",
                    "now",
                    "tester",
                ),
            )
            conn.execute(
                """
                INSERT INTO sessions(
                    id,scope_id,workspace_id,title,external_ref,thread_id,
                    next_run_seq,execution_epoch,active_run_id,
                    date_created,created_by,date_updated,updated_by
                ) VALUES (?,?,?,?,NULL,?,1,0,NULL,?,?,?,?)
                """,
                (
                    "session-1",
                    "default",
                    "workspace-1",
                    "legacy",
                    "thread:session-1",
                    "now",
                    "tester",
                    "now",
                    "tester",
                ),
            )
            conn.commit()
            conn.close()

            store = SqliteRuntimeStore(path)
            self.addCleanup(store.close)
            self.assertEqual(
                store.conn.execute("PRAGMA user_version").fetchone()[0],
                store.SCHEMA_VERSION,
            )
            workspace = store.get_workspace("default", "workspace-1")
            self.assertIsNotNone(workspace)
            self.assertEqual(workspace["project_ref"], "legacy-project:workspace-1")
            self.assertEqual(workspace["tenant_ref"], "legacy-tenant:default")
            self.assertEqual(workspace["user_ref"], "default")
            self.assertEqual(workspace["user_path"], "")
            self.assertEqual(workspace["storage_root"], "")
            self.assertEqual(workspace["user_rel_path"], "")
            self.assertEqual(workspace["project_path"], "")
            session = store.get_session("default", "session-1")
            self.assertIsNotNone(session)
            self.assertEqual(session["next_event_seq"], 1)
            event_columns = {
                row["name"]
                for row in store.conn.execute("PRAGMA table_info(run_events)").fetchall()
            }
            self.assertIn("session_seq", event_columns)
            indexes = {
                row["name"] for row in store.conn.execute("PRAGMA index_list(sessions)").fetchall()
            }
            scoped_workspace_indexes = []
            for index_name in indexes:
                columns = [
                    row["name"]
                    for row in store.conn.execute(f"PRAGMA index_info({index_name})").fetchall()
                ]
                if columns == ["scope_id", "workspace_id"]:
                    scoped_workspace_indexes.append(index_name)
            self.assertEqual(scoped_workspace_indexes, [])


if __name__ == "__main__":
    unittest.main()
