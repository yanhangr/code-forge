"""Local adapter integration tests for the runnable development runtime."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from code_forge.contracts import (
    DomainError,
    ErrorCode,
    ExecutionContext,
    OperationSpec,
    RunRequest,
    RunStatus,
    SkillBinding,
    TaskOutcome,
    ToolStatus,
)
from code_forge.execution.local_process_backend import LocalProcessBackend, _clean_env
from code_forge.harness.local import LocalDeterministicHarness
from code_forge.persistence.sqlite_store import SqliteRunRepository, SqliteRuntimeStore
from code_forge.ports import DefaultAllowAuthorization
from code_forge.runtime.app import AgentRuntime
from code_forge.skills.manual_skill_resolver import ManualSkillResolver
from code_forge.workspace.store import WorkspaceStore


class LocalRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        skill_dir = root / "skills" / "python-analysis"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            """---
name: python-analysis
description: Run Python analysis.
version: "1"
---

Use execute_python.
""",
            encoding="utf-8",
        )
        self.skills_root = root / "skills"
        self.state_path = root / "state.db"
        self.snapshot_root = root / "snapshots"
        self.workspace_root = root / "workspace-store"
        self.operation_root = root / "operations"

        self.store = SqliteRuntimeStore(self.state_path)
        self.resolver = ManualSkillResolver(self.skills_root, self.snapshot_root)
        self.workspace = WorkspaceStore(self.workspace_root)
        self.execution = LocalProcessBackend(self.workspace, self.operation_root)
        self.harness = LocalDeterministicHarness(
            self.store,
            self.execution,
            self.workspace,
            worker_id="test-worker",
        )
        self.runtime = AgentRuntime(
            self.store,
            self.resolver,
            self.workspace,
            self.execution,
            self.harness,
            repository=SqliteRunRepository(self.store),
            authorization=DefaultAllowAuthorization(),
            worker_id="test-worker",
        )
        self.context = ExecutionContext(scope_id="default", actor_ref="tester")
        self.session = self.runtime.create_session(
            scope_id="default",
            idempotency_key="session-key",
            title="test",
            external_ref=None,
            context=self.context,
        )

    async def _submit(self, input_text: str, key: str = "run-key", skills=()) -> dict:
        request = RunRequest(
            session_id=self.session["id"],
            input=input_text,
            agent_ref="general@1",
            skills=skills,
            context=self.context,
        )
        accepted = await self.runtime.service.submit(request, key)
        return self.store.get_run("default", accepted.id)

    async def test_session_and_skill_snapshot_are_immutable(self):
        run = await self._submit(
            "```python\nprint('hello')\n```",
            skills=(SkillBinding(name="python-analysis", version="1"),),
        )
        self.assertEqual(run["status"], RunStatus.QUEUED.value)
        old_digest = run["config_snapshot"]["skills"][0]["digest"]

        skill_file = self.skills_root / "python-analysis" / "SKILL.md"
        skill_file.write_text(
            """---
name: python-analysis
description: Updated.
version: "2"
---

Use execute_python carefully.
""",
            encoding="utf-8",
        )
        repeated = await self._submit(
            "```python\nprint('hello')\n```",
            key="run-key",
            skills=(SkillBinding(name="python-analysis", version="1"),),
        )
        self.assertEqual(repeated["id"], run["id"])
        self.assertEqual(repeated["config_snapshot"]["skills"][0]["digest"], old_digest)

        new_run = await self._submit(
            "```python\nprint('hello again')\n```",
            key="run-key-2",
            skills=(SkillBinding(name="python-analysis", version="2"),),
        )
        self.assertNotEqual(new_run["id"], run["id"])
        self.assertNotEqual(new_run["config_snapshot"]["skills"][0]["digest"], old_digest)

    async def test_idempotency_conflict_is_reported(self):
        await self._submit("```python\nprint('a')\n```", key="same")
        with self.assertRaises(DomainError) as error:
            await self._submit("```python\nprint('b')\n```", key="same")
        self.assertEqual(error.exception.code, ErrorCode.IDEMPOTENCY_CONFLICT)

    async def test_python_execution_commits_workspace_and_finishes(self):
        code = (
            "from pathlib import Path\n"
            "value = sum(i * i for i in range(1, 101))\n"
            "print(value)\n"
            "Path('result.txt').write_text(str(value))\n"
        )
        await self._submit(f"```python\n{code}```")
        claimed = self.store.claim_next_run(
            "default",
            "test-worker",
            60,
            datetime.now(timezone.utc),
        )
        self.assertIsNotNone(claimed)
        result = await self.harness.execute(claimed)
        self.assertEqual(result["status"], RunStatus.SUCCEEDED.value)
        self.assertEqual(result["task_outcome"], TaskOutcome.COMPLETED.value)
        self.assertIn("338350", result["output"])

        files = self.workspace.list_files(claimed["workspace_id"])
        paths = {item["path"] for item in files}
        self.assertIn("result.txt", paths)
        self.assertFalse(any(Path(path).name.startswith("model_") for path in paths))
        operation = self.store.conn.execute(
            """
            SELECT id FROM tool_executions
            WHERE scope_id = ? AND run_id = ? AND tool_ref = 'python'
            """,
            ("default", result["id"]),
        ).fetchone()
        self.assertIsNotNone(operation)
        operation_dir = self.operation_root / operation["id"]
        self.assertEqual((operation_dir / "input.txt").read_text(encoding="utf-8"), code)
        events, _ = self.store.list_events("default", result["id"], 0, 100)
        event_types = [event["type"] for event in events]
        self.assertIn("tool.prepared", event_types)
        self.assertIn("tool.started", event_types)
        self.assertIn("tool.finished", event_types)
        self.assertIn("run.finished", event_types)

    async def test_operation_timeout_writes_diagnostic_to_stderr(self):
        spec = OperationSpec(
            operation_id="timeout-operation",
            run_id="timeout-run",
            session_id=None,
            attempt_id="timeout-attempt",
            workspace_ref="timeout-workspace",
            argv=("python3", "-"),
            timeout_seconds=1,
            output_limit_bytes=4096,
            environment_profile_ref="local@1",
            stdin_text="import time\ntime.sleep(5)\n",
        )
        await self.execution.submit(spec)
        while True:
            result = await self.execution.get_status(spec.operation_id)
            if result.status != ToolStatus.RUNNING:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(result.status, ToolStatus.FAILED)
        self.assertIn(
            "Operation timed out after 1 seconds",
            Path(result.stderr_ref or "").read_text(encoding="utf-8"),
        )

    async def test_no_model_returns_blocked_instead_of_fake_success(self):
        await self._submit("请帮我完成一个没有代码块的普通任务")
        claimed = self.store.claim_next_run(
            "default",
            "test-worker",
            60,
            datetime.now(timezone.utc),
        )
        result = await self.harness.execute(claimed)
        self.assertEqual(result["status"], RunStatus.SUCCEEDED.value)
        self.assertEqual(result["task_outcome"], TaskOutcome.BLOCKED.value)
        self.assertIn("未配置真实模型", result["output"])

    async def test_cancel_queued_run_is_terminal(self):
        run = await self._submit("```python\nprint('queued')\n```")
        cancelled = self.runtime.cancel_run("default", run["id"])
        self.assertEqual(cancelled["status"], RunStatus.CANCELLED.value)
        events, _ = self.store.list_events("default", run["id"], 0, 100)
        event_types = [event["type"] for event in events]
        self.assertIn("run.cancel_requested", event_types)
        self.assertIn("run.finished", event_types)


class ToolEnvironmentTests(unittest.TestCase):
    def test_tool_subprocess_does_not_inherit_runtime_config(self):
        import os

        previous = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(previous)))
        os.environ["FORGE_SKILLS_DIR"] = "/tmp/other-skills"
        os.environ["DEEPSEEK_API_KEY"] = "secret"
        os.environ["PATH"] = "/usr/bin"

        env = _clean_env()

        self.assertNotIn("FORGE_SKILLS_DIR", env)
        self.assertNotIn("DEEPSEEK_API_KEY", env)
        self.assertEqual(env["PATH"], "/usr/bin")


if __name__ == "__main__":
    unittest.main()
