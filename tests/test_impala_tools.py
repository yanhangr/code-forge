"""TC-B23/B24/B25/B27/B29/B30/B35: Impala policy and adapter fixtures.

Real SQLite, files and (where installed) LangGraph; deterministic model/HS2
fixtures. These tests do not prove live LDAP, Ranger, Impala or model behavior.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, urlopen
from uuid import uuid4

from code_forge.contracts import DomainError, ErrorCode, ExecutionContext, RunRequest, RunStatus
from code_forge.integrations.impala.config import DepartmentBindings, QueryPolicy
from code_forge.integrations.impala.driver import connect, query_id
from code_forge.integrations.impala.service import (
    SNAPSHOT_PREFIX,
    ImpalaTools,
    encoded,
)
from code_forge.integrations.impala.sql import bounded_select
from code_forge.runtime.plugins import build_builtin_registry, build_plugins
from code_forge.service import RunService
from code_forge.transport.server import create_server

HAS_SQLGLOT = importlib.util.find_spec("sqlglot") is not None
HAS_IMPYLA = importlib.util.find_spec("impala") is not None


def config(path: Path, **policy) -> dict:
    data = {
        "mode": "trusted_single_department",
        "default_department": "A",
        "departments": {
            "A": {
                "host": "impala.invalid",
                "user": "account1",
                "password_env": "IM_TEST_PASSWORD",
                "policy": policy,
            }
        },
    }
    path.write_text(json.dumps(data))
    return data


class FakeCursor:
    def __init__(self, user="account1", rows=None, running=False, fail_cancel=False, wide=False):
        self.user = user
        self.rows = rows if rows is not None else [(i,) for i in range(200)]
        self.running = running
        self.fail_cancel = fail_cancel
        self.wide = wide
        self.cancelled = False
        self.closed = False
        self.executions = []
        self.fetches = 0
        self.index = 0
        self._arraysize = 10240

    @property
    def arraysize(self):
        return self._arraysize

    @arraysize.setter
    def arraysize(self, size):
        self._arraysize = size

    @property
    def buffersize(self):
        return self._arraysize

    def execute_async(self, sql, configuration=None):
        self.executions.append((sql, configuration))
        self.sql = sql
        self.index = 0

    def status(self):
        if self.cancelled:
            return "CANCELED_STATE"
        if self.sql != "SELECT effective_user()" and self.running:
            return "RUNNING_STATE"
        return "FINISHED_STATE"

    @property
    def description(self):
        return [("汉" * 128,)] * 100 if self.wide else [("value",)]

    def fetchmany(self, size):
        assert size == 1, "driver must not prefetch unlimited rows"
        if self.sql == "SELECT effective_user()":
            return [(self.user,)]
        self.fetches += 1
        batch = self.rows[self.index : self.index + size]
        self.index += size
        return batch

    def cancel_operation(self, reset_state=False):
        if self.fail_cancel:
            raise RuntimeError("FAKE_SECRET_CANCEL")
        self.cancelled = True

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


@unittest.skipUnless(HAS_SQLGLOT, "optional SQLGlot dependency not installed")
class ImpalaSQLTests(unittest.TestCase):
    def test_outer_cap_preserves_small_limit_and_caps_union_cte(self):
        for sql in [
            "SELECT * FROM t LIMIT 999999",
            "WITH x AS (SELECT * FROM t) SELECT * FROM x",
            "SELECT * FROM t UNION ALL SELECT * FROM u",
        ]:
            self.assertTrue(bounded_select(sql, 100, QueryPolicy()).endswith("LIMIT 101"))
        self.assertTrue(bounded_select("SELECT 1 LIMIT 3", 100, QueryPolicy()).endswith("LIMIT 3"))

    def test_reject_writes_nested_writes_multiple_statements_and_functions(self):
        for sql in [
            "INSERT INTO t SELECT 1",
            "CREATE TABLE t AS SELECT 1",
            "DELETE FROM t",
            "SET MEM_LIMIT=0",
            "SELECT 1; DROP TABLE t",
            "SELECT 1;;",
            "WITH x AS (DELETE FROM t) SELECT * FROM x",
            "SELECT dangerous_udf(x) FROM t",
            "SELECT * FROM read_csv('/tmp/x')",
            "SELECT 1 LIMIT -1",
            "SELECT * FROM t FOR UPDATE",
            "EXPLAIN INSERT INTO t SELECT 1",
        ]:
            with self.subTest(sql=sql), self.assertRaises(DomainError):
                bounded_select(sql, 100, QueryPolicy())

    def test_literals_with_semicolons_and_comments_are_safe(self):
        self.assertIn("'a;b'", bounded_select("SELECT 'a;b' /* DROP TABLE t */", 10, QueryPolicy()))

    def test_required_partition_filter_resists_or_and_unqualified_table_bypass(self):
        policy = replace(QueryPolicy(), required_filters=(("default.sales", "dt"),))
        for sql in [
            "SELECT * FROM sales",
            "SELECT * FROM sales WHERE dt=dt",
            "SELECT * FROM sales WHERE dt='2026-10-01' OR 1=1",
            "SELECT * FROM sales s JOIN sales t ON s.id=t.id WHERE s.dt='2026-10-01'",
        ]:
            with self.subTest(sql=sql), self.assertRaises(DomainError):
                bounded_select(sql, 100, policy)
        bounded_select(
            "SELECT * FROM sales WHERE dt>='2026-10-01' AND dt<'2026-10-02'", 100, policy
        )

    def test_limit_type_and_maximum_are_checked(self):
        for rows in [True, -1, 1001, "100"]:
            with self.assertRaises(DomainError):
                bounded_select("SELECT 1", rows, QueryPolicy())


@unittest.skipUnless(HAS_SQLGLOT, "optional SQLGlot dependency not installed")
class ImpalaAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "impala.json"
        config(self.config_path)
        self.bindings = DepartmentBindings(self.config_path)
        self.cursor = FakeCursor()
        self.connections = []

        def connector(profile, secrets):
            self.connections.append(profile)
            return FakeConnection(self.cursor)

        self.tools = ImpalaTools(
            self.bindings, self.root / "queries", {"IM_TEST_PASSWORD": "FAKE_SECRET"}, connector
        )
        self.run = {
            "id": "message1",
            "scope_id": "user1",
            "session_id": "session1",
            "config_snapshot": {
                "tool_refs": [SNAPSHOT_PREFIX + self.bindings.require("user1").digest]
            },
        }

    async def call(
        self, name="impala_query", args=None, cancelled=lambda: False, progress=lambda x: None
    ):
        return await self.tools.call(
            name, args or {"sql": "SELECT * FROM t"}, self.run, str(uuid4()), cancelled, progress
        )

    async def test_bounded_rows_and_options(self):
        result = await self.call()
        self.assertEqual(result["row_count"], 100)
        self.assertEqual(self.cursor.fetches, 101)
        self.assertEqual(result["truncation_reasons"], ["row_limit"])
        self.assertTrue(self.cursor.closed)
        self.assertEqual(self.cursor.buffersize, 1)
        self.assertEqual(self.cursor.executions[-1][1]["MEM_LIMIT"], "1024m")
        self.assertEqual(self.cursor.executions[-1][1]["EXEC_TIME_LIMIT_S"], "60")
        self.assertLessEqual(len(encoded(result)), 32000)

    async def test_bytes_cells_and_wide_metadata_are_bounded(self):
        config(self.config_path, max_bytes=4096, cell_bytes=1000)
        self.run["config_snapshot"]["tool_refs"] = [
            SNAPSHOT_PREFIX + self.bindings.require("user1").digest
        ]
        self.cursor.rows = [("汉" * 5000,)] * 100
        result = await self.call()
        self.assertIn("cell_limit", result["truncation_reasons"])
        self.assertIn("byte_limit", result["truncation_reasons"])
        self.assertLessEqual(len(encoded(result)), 4096)
        self.assertLess(self.cursor.fetches, 10)
        self.cursor.wide = True
        result = await self.call()
        self.assertEqual(result["status"], "FAILED")
        self.assertLessEqual(len(encoded(result)), 4096)

    async def test_model_cannot_choose_account_department_or_options(self):
        for key in ["department", "user", "password", "query_options", "credential_ref"]:
            with self.subTest(key=key), self.assertRaises(DomainError):
                await self.call(args={"sql": "SELECT 1", key: "account2"})
        self.assertFalse(self.connections)

    async def test_wrong_effective_account_fails_before_business_query(self):
        self.cursor.user = "account2"
        result = await self.call()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "CAPABILITY_DENIED")
        self.assertEqual(len(self.cursor.executions), 1)

    async def test_owner_required_for_status_and_cancel_including_same_department(self):
        result = await self.call()
        for run in [{**self.run, "scope_id": "user2"}, {**self.run, "session_id": "session2"}]:
            with self.assertRaises(DomainError):
                await self.tools.call(
                    "impala_query_status",
                    {"query_ref": result["query_ref"]},
                    run,
                    str(uuid4()),
                    lambda: False,
                    lambda x: None,
                )

    async def test_cancel_signals_owner_thread_and_verifies_remote_cleanup(self):
        self.cursor.running = True
        ref = str(uuid4())
        started = asyncio.Event()
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(
            self.tools.call(
                "impala_query",
                {"sql": "SELECT 1"},
                self.run,
                ref,
                lambda: False,
                lambda _: loop.call_soon_threadsafe(started.set),
            )
        )
        await asyncio.wait_for(started.wait(), 2)
        state = self.tools._control(ref, self.run, True)
        self.assertEqual(state["status"], "CANCELLING")
        result = await asyncio.wait_for(task, 2)
        self.assertEqual(result["status"], "CANCELLED")
        self.assertTrue(self.cursor.cancelled)

    async def test_cancel_failure_is_unknown_and_sanitized(self):
        self.cursor.running = True
        self.cursor.fail_cancel = True
        result = await self.call(cancelled=lambda: len(self.cursor.executions) > 1)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertNotIn("FAKE_SECRET", json.dumps(result))

    async def test_deadline_cancels_query(self):
        config(self.config_path, timeout_seconds=1, rpc_timeout_seconds=1)
        self.run["config_snapshot"]["tool_refs"] = [
            SNAPSHOT_PREFIX + self.bindings.require("user1").digest
        ]
        self.cursor.running = True
        result = await asyncio.wait_for(self.call(), 3)
        self.assertEqual(result["status"], "FAILED")
        self.assertTrue(self.cursor.cancelled)
        self.assertIn("deadline", result["error"]["message"])

    async def test_revocation_during_query_cancels(self):
        def revoke(_):
            data = json.loads(self.config_path.read_bytes())
            data["departments"]["A"]["enabled"] = False
            self.config_path.write_text(json.dumps(data))

        self.cursor.running = True
        result = await self.call(progress=revoke)
        self.assertEqual(result["status"], "FAILED")
        self.assertTrue(self.cursor.cancelled)

    async def test_profile_change_rejected_and_query_budget_survives_restart(self):
        config(self.config_path, max_queries_per_message=1)
        with self.assertRaises(DomainError):
            await self.call()
        self.run["config_snapshot"]["tool_refs"] = [
            SNAPSHOT_PREFIX + self.bindings.require("user1").digest
        ]
        await self.call()
        with self.assertRaises(DomainError):
            await self.call()
        replacement = ImpalaTools(self.bindings, self.tools.root, {}, self.tools.connector)
        with self.assertRaises(DomainError):
            await replacement.call(
                "impala_query",
                {"sql": "SELECT 1"},
                self.run,
                str(uuid4()),
                lambda: False,
                lambda _: None,
            )

    async def test_metadata_and_explain_are_registered_and_execute(self):
        self.assertEqual(len(self.tools.specs(self.run)), 6)
        for name, args, prefix in [
            ("impala_search_tables", {}, "SHOW DATABASES"),
            (
                "impala_search_tables",
                {"database": "analytics", "pattern": "sales*"},
                "SHOW TABLES IN",
            ),
            ("impala_describe_table", {"table": "sales"}, "DESCRIBE"),
            ("impala_explain", {"sql": "SELECT 1"}, "EXPLAIN"),
        ]:
            await self.tools.call(name, args, self.run, str(uuid4()), lambda: False, lambda _: None)
            self.assertTrue(self.cursor.executions[-1][0].startswith(prefix))

    async def test_hourly_budget_crosses_message_boundaries_and_restart(self):
        config(self.config_path, max_queries_per_user_hour=1)
        self.run["config_snapshot"]["tool_refs"] = [
            SNAPSHOT_PREFIX + self.bindings.require("user1").digest
        ]
        await self.call()
        self.run["id"] = "message2"
        with self.assertRaises(DomainError):
            await self.call()
        self.tools = ImpalaTools(self.bindings, self.tools.root, {}, self.tools.connector)
        with self.assertRaises(DomainError):
            await self.call()

    async def test_concurrency_is_checked_for_user_and_department(self):
        config(self.config_path, max_department_concurrency=1)
        self.run["config_snapshot"]["tool_refs"] = [
            SNAPSHOT_PREFIX + self.bindings.require("user1").digest
        ]
        self.cursor.running = True
        ref = str(uuid4())
        started = asyncio.Event()
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(
            self.tools.call(
                "impala_query",
                {"sql": "SELECT 1"},
                self.run,
                ref,
                lambda: False,
                lambda _: loop.call_soon_threadsafe(started.set),
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 2)
            with self.assertRaises(DomainError):
                await self.call()
            run2 = {**self.run, "scope_id": "user2", "id": "message2"}
            with self.assertRaises(DomainError):
                await self.tools.call(
                    "impala_query",
                    {"sql": "SELECT 1"},
                    run2,
                    str(uuid4()),
                    lambda: False,
                    lambda _: None,
                )
        finally:
            self.tools._control(ref, self.run, True)
            await asyncio.wait_for(task, 2)

    async def test_restarted_pending_query_is_unknown_without_resubmission(self):
        self.cursor.running = True
        ref = str(uuid4())
        started = asyncio.Event()
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(
            self.tools.call(
                "impala_query",
                {"sql": "SELECT 1"},
                self.run,
                ref,
                lambda: False,
                lambda _: loop.call_soon_threadsafe(started.set),
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 2)
            replacement = ImpalaTools(self.bindings, self.tools.root, {}, self.tools.connector)
            self.assertEqual(replacement._control(ref, self.run, False)["status"], "UNKNOWN")
            self.assertEqual(replacement._control(ref, self.run, True)["status"], "UNKNOWN")
            self.assertEqual(len(self.connections), 1)
        finally:
            self.tools._control(ref, self.run, True)
            await asyncio.wait_for(task, 2)

    async def test_query_id_uses_impala_shell_guid_format(self):
        cursor = SimpleNamespace(
            _last_operation=SimpleNamespace(
                handle=SimpleNamespace(operationId=SimpleNamespace(guid=bytes(range(16))))
            )
        )
        self.assertEqual(query_id(cursor), "0706050403020100:0f0e0d0c0b0a0908")

    async def test_multi_department_mapping_has_no_default_fallback(self):
        data = config(self.config_path)
        data.update(mode="mapped_departments", scope_departments={"user1": "A", "user2": "B"})
        data["departments"]["B"] = {**data["departments"]["A"], "user": "account2"}
        self.config_path.write_text(json.dumps(data))
        self.assertEqual(self.bindings.require("user2").user, "account2")
        self.assertIsNone(self.bindings.resolve("unknown"))
        self.run["config_snapshot"]["tool_refs"] = [
            SNAPSHOT_PREFIX + self.bindings.require("user1").digest
        ]
        await self.call()
        self.assertEqual(self.connections[0].user, "account1")

    @unittest.skipUnless(HAS_IMPYLA, "optional Impyla dependency not installed")
    async def test_driver_enforces_tls_certificate_validation_and_no_retry(self):
        with patch("impala.dbapi.connect") as mocked:
            connect(self.bindings.require("user1"), {"IM_TEST_PASSWORD": "FAKE_SECRET"})
        kwargs = mocked.call_args.kwargs
        self.assertTrue(kwargs["use_ssl"])
        self.assertTrue(kwargs["verify_cert"])
        self.assertEqual(kwargs["retries"], 1)
        self.assertEqual(kwargs["auth_mechanism"], "LDAP")
        self.assertEqual(kwargs["user"], "account1")
        # Exercise the installed driver's actual attempt semantics, without network.
        from impala.hiveserver2 import ThriftRPC

        transport = SimpleNamespace(isOpen=lambda: True)
        calls = []
        client = SimpleNamespace(
            _iprot=SimpleNamespace(trans=transport),
            Ping=lambda request: calls.append(request) or "ok",
        )
        rpc = ThriftRPC(client, retries=kwargs["retries"])
        self.assertEqual(rpc._execute("Ping", "request"), "ok")
        self.assertEqual(calls, ["request"])
        from impala.hiveserver2 import HiveServer2Cursor

        actual_cursor = HiveServer2Cursor(session=None)
        actual_cursor.arraysize = 1
        self.assertEqual(actual_cursor.buffersize, 1)
        self.assertIsNone(type(actual_cursor).buffersize.fset)

    async def test_unsafe_auth_and_unbounded_http_transport_are_rejected(self):
        for updates in [{"auth_mechanism": "NOSASL"}, {"use_http_transport": True}]:
            data = config(self.config_path)
            data["departments"]["A"].update(updates)
            self.config_path.write_text(json.dumps(data))
            with self.assertRaises(DomainError):
                self.bindings.require("user1")


@unittest.skipUnless(HAS_SQLGLOT and HAS_IMPYLA, "optional Impala dependencies not installed")
class ImpalaHarnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "impala.json"
        config(self.config_path)
        self.plugins = build_plugins(
            self.root,
            {
                "FORGE_MODEL": "none",
                "FORGE_HARNESS": "local",
                "FORGE_IMPALA_CONFIG": str(self.config_path),
            },
        )
        self.addCleanup(self.plugins.store.conn.close)
        self.cursor = FakeCursor(rows=[(42,)])
        self.plugins.harness.impala_dispatcher.tools.connector = lambda *_: FakeConnection(
            self.cursor
        )
        self.context = ExecutionContext(scope_id="default", actor_ref="fixture-user")
        self.session = self.plugins.store.create_session(
            "default", "session-key", "Test", None, self.context
        )

    async def claim(self):
        service = RunService(
            self.plugins.repository, self.plugins.resolver, self.plugins.authorization
        )
        await service.submit(
            RunRequest(session_id=self.session["id"], input="分析", context=self.context),
            "message-key",
        )
        return self.plugins.store.claim_next_run("default", "test", 120, datetime.now(timezone.utc))

    async def test_acceptance_snapshot_sqlite_files_and_duplicate_call_reuse(self):
        claimed = await self.claim()
        dispatcher = self.plugins.harness.impala_dispatcher
        call = {
            "id": "call1",
            "function": {"name": "impala_query", "arguments": '{"sql":"SELECT 42"}'},
        }
        result = await dispatcher.execute(
            claimed["run"], claimed["attempt_id"], call, "fixture-user"
        )
        again = await dispatcher.execute(
            claimed["run"], claimed["attempt_id"], call, "fixture-user"
        )
        self.assertEqual(result, again)
        self.assertEqual(len(self.cursor.executions), 2)
        self.assertEqual(result["rows"], [[42]])
        operation = self.plugins.store.conn.execute("SELECT * FROM tool_executions").fetchone()
        self.assertEqual(operation["status"], "SUCCEEDED")
        self.assertTrue(Path(operation["result_ref"]).exists())
        events, _ = self.plugins.store.list_events("default", claimed["run"]["id"], 0, 100)
        self.assertTrue(
            {"tool.prepared", "tool.started", "tool.output", "tool.finished"}.issubset(
                {e["type"] for e in events}
            )
        )
        self.assertNotIn("FAKE_SECRET", json.dumps(events))
        changed = {**call, "function": {"name": "impala_query", "arguments": '{"sql":"SELECT 43"}'}}
        with self.assertRaises(DomainError):
            await dispatcher.execute(claimed["run"], claimed["attempt_id"], changed, "fixture-user")

    async def test_model_loop_both_harnesses(self):
        for harness_name in ["local", "langgraph"]:
            if harness_name == "langgraph" and not importlib.util.find_spec("langgraph"):
                continue
            with self.subTest(harness=harness_name):
                # Separate messages avoid reusing a terminal execution.
                if harness_name == "langgraph":
                    service = RunService(
                        self.plugins.repository, self.plugins.resolver, self.plugins.authorization
                    )
                    await service.submit(
                        RunRequest(
                            session_id=self.session["id"], input="继续", context=self.context
                        ),
                        "message2",
                    )
                    claimed = self.plugins.store.claim_next_run(
                        "default", "test", 120, datetime.now(timezone.utc)
                    )
                else:
                    claimed = await self.claim()

                class Model:
                    calls = 0

                    async def complete(self, messages, tools=None):
                        self.calls += 1
                        if self.calls == 1:
                            assert "impala_query" in {s["function"]["name"] for s in tools}
                            return {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "model-call",
                                        "function": {
                                            "name": "impala_query",
                                            "arguments": '{"sql":"SELECT 42"}',
                                        },
                                    }
                                ],
                            }
                        result = json.loads(messages[-1]["content"])
                        assert result["rows"] == [[42]]
                        return {"role": "assistant", "content": "结果是42"}

                if harness_name == "local":
                    harness = self.plugins.harness
                    harness.model_adapter = Model()
                else:
                    from code_forge.harness.langgraph_harness import LangGraphHarness

                    harness = LangGraphHarness(
                        self.plugins.store,
                        self.plugins.execution,
                        self.plugins.workspace,
                        model_adapter=Model(),
                        impala_dispatcher=self.plugins.harness.impala_dispatcher,
                    )
                result = await harness.execute(claimed)
                self.assertEqual(result["status"], RunStatus.SUCCEEDED.value)
                self.assertEqual(result["output"], "结果是42")

    async def test_unknown_dispatch_is_not_replayed(self):
        claimed = await self.claim()
        dispatcher = self.plugins.harness.impala_dispatcher
        self.cursor.running = True
        self.cursor.fail_cancel = True
        call = {
            "id": "lost-call",
            "function": {"name": "impala_query", "arguments": '{"sql":"SELECT 42"}'},
        }
        # Deliberately lose remote state after dispatch without waiting for the 60s deadline.
        self.cursor.status = lambda: (_ for _ in ()).throw(RuntimeError("FAKE_SECRET"))
        result = await dispatcher.execute(
            claimed["run"], claimed["attempt_id"], call, "fixture-user"
        )
        self.assertEqual(result["status"], "UNKNOWN")
        executions = len(self.cursor.executions)
        again = await dispatcher.execute(
            claimed["run"], claimed["attempt_id"], call, "fixture-user"
        )
        self.assertEqual(again["status"], "UNKNOWN")
        self.assertEqual(len(self.cursor.executions), executions)

    async def test_message_cancel_is_confirmed_in_remote_and_runtime(self):
        claimed = await self.claim()
        dispatcher = self.plugins.harness.impala_dispatcher
        self.cursor.running = True
        original = self.plugins.store.append_tool_output

        def output(scope, operation, stream, text, truncated, actor):
            original(scope, operation, stream, text, truncated, actor)
            if json.loads(text).get("status") == "RUNNING":
                self.plugins.store.cancel_run(scope, claimed["run"]["id"], "fixture-user")

        with patch.object(self.plugins.store, "append_tool_output", side_effect=output):
            call = {
                "id": "cancel-call",
                "function": {"name": "impala_query", "arguments": '{"sql":"SELECT 42"}'},
            }
            result = await dispatcher.execute(
                claimed["run"], claimed["attempt_id"], call, "fixture-user"
            )
        self.assertEqual(result["status"], "CANCELLED")
        self.assertTrue(self.cursor.cancelled)
        self.assertEqual(
            self.plugins.store.get_run("default", claimed["run"]["id"])["status"], "CANCELLED"
        )

    async def test_concurrent_duplicate_tool_calls_share_result(self):
        claimed = await self.claim()
        dispatcher = self.plugins.harness.impala_dispatcher
        call = {
            "id": "duplicate-call",
            "function": {"name": "impala_query", "arguments": '{"sql":"SELECT 42"}'},
        }
        one, two = await asyncio.gather(
            *[
                dispatcher.execute(claimed["run"], claimed["attempt_id"], call, "fixture-user")
                for _ in range(2)
            ]
        )
        self.assertEqual(one, two)
        self.assertEqual(one["status"], "SUCCEEDED")
        self.assertEqual(len(self.cursor.executions), 2)  # identity + one business SQL

    async def test_authorization_port_can_deny_tool_without_dispatch(self):
        claimed = await self.claim()
        dispatcher = self.plugins.harness.impala_dispatcher

        class Deny:
            async def check(self, context, action, resource):
                raise DomainError(
                    ErrorCode.CAPABILITY_DENIED,
                    "Denied",
                )

        dispatcher.authorization = Deny()
        call = {
            "id": "deny-call",
            "function": {"name": "impala_query", "arguments": '{"sql":"SELECT 42"}'},
        }
        result = await dispatcher.execute(
            claimed["run"], claimed["attempt_id"], call, "fixture-user"
        )
        self.assertEqual(result["error"]["code"], "CAPABILITY_DENIED")
        self.assertFalse(self.cursor.executions)


@unittest.skipUnless(
    HAS_SQLGLOT and HAS_IMPYLA and importlib.util.find_spec("langgraph"),
    "optional Impala/LangGraph dependencies not installed",
)
class ImpalaHTTPTests(unittest.TestCase):
    def test_http_sse_langgraph_and_user_root_projection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.json"
            config(config_path)
            registry = build_builtin_registry()

            class Model:
                calls = 0

                async def complete(self, messages, tools=None):
                    self.calls += 1
                    if self.calls == 1:
                        return {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "http-query",
                                    "function": {
                                        "name": "impala_query",
                                        "arguments": '{"sql":"SELECT 42"}',
                                    },
                                }
                            ],
                        }
                    result = json.loads(messages[-1]["content"])
                    assert result["rows"] == [[42]]
                    return {"role": "assistant", "content": "结果是42"}

            registry.register("model", "impala-fixture", lambda **_: Model())
            server = create_server(
                "127.0.0.1",
                0,
                root=root / "runtime",
                registry=registry,
                env={
                    "FORGE_MODEL": "impala-fixture",
                    "FORGE_HARNESS": "langgraph",
                    "FORGE_IMPALA_CONFIG": str(config_path),
                },
            )
            server.runtime.harness.impala_dispatcher.tools.connector = lambda *_: FakeConnection(
                FakeCursor(rows=[(42,)])
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def post(path, body):
                request = Request(
                    base + path,
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json", "Connection": "close"},
                )
                with urlopen(request, timeout=10) as response:
                    return response.read().decode()

            try:
                created = json.loads(
                    post(
                        "/v1/create-session",
                        {
                            "storage_root": str(root / "users"),
                            "user_rel_path": "T001/users/U001",
                            "project_ref": "P001",
                        },
                    )
                )
                session_id = created["data"]["session_id"]
                stream = post(
                    "/v1/send-message",
                    {"session_id": session_id, "input": "查42", "skill_paths": []},
                )
                for name in [
                    "tool.prepared",
                    "tool.started",
                    "tool.output",
                    "tool.finished",
                    "message.finished",
                ]:
                    self.assertIn("event: " + name, stream)
                self.assertIn("impala_query", stream)
                self.assertNotIn("FAKE_SECRET", stream)
                user_root = root / "users" / "T001/users/U001"
                result_files = list(
                    (user_root / "tool-output" / session_id).glob("*/*/result.json")
                )
                self.assertEqual(len(result_files), 1)
                self.assertEqual(json.loads(result_files[0].read_bytes())["rows"], [[42]])
                transcript = list((user_root / "sessions" / session_id).glob("*.jsonl"))
                self.assertTrue(transcript)
                self.assertIn("结果是42", "".join(path.read_text() for path in transcript))
            finally:
                server.runtime.stop_worker()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                server.runtime.store.conn.close()


if __name__ == "__main__":
    unittest.main()
