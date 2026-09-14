"""Conversation context and session history tests."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from code_forge.contracts import ExecutionContext, RunRequest, RunStatus, TaskOutcome
from code_forge.harness.context import ConversationContextManager
from code_forge.persistence.sqlite_store import SqliteRunRepository, SqliteRuntimeStore
from code_forge.ports import DefaultAllowAuthorization
from code_forge.service import RunService
from code_forge.skills.manual_skill_resolver import ManualSkillResolver


class ConversationContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        skills = root / "skills"
        skills.mkdir()
        self.store = SqliteRuntimeStore(root / "state.db")
        self.resolver = ManualSkillResolver(skills, root / "snapshots")
        self.service = RunService(
            SqliteRunRepository(self.store),
            self.resolver,
            DefaultAllowAuthorization(),
        )
        self.context = ExecutionContext(scope_id="default", actor_ref="tester")
        self.session = self.store.create_session(
            "default", "session-key", "history", None, self.context
        )

    async def _accept(self, text: str, key: str) -> str:
        accepted = await self.service.submit(
            RunRequest(self.session["id"], text, context=self.context),
            key,
        )
        return accepted.id

    async def test_conversation_history_is_scoped_to_session_and_order(self):
        first = await self._accept("first", "first-key")
        claimed = self.store.claim_next_run(
            "default", "test-worker", 60, datetime.now(timezone.utc)
        )
        self.store.transition_run(
            "default",
            first,
            expected_version=claimed["run"]["state_version"],
            target=RunStatus.SUCCEEDED,
            task_outcome=TaskOutcome.COMPLETED,
            output="first answer",
        )
        second = await self._accept("second", "second-key")
        history = self.store.conversation_history("default", self.session["id"], second)
        self.assertEqual([item["input"] for item in history], ["first"])
        self.assertEqual(history[0]["output"], "first answer")

    async def test_context_manager_compresses_older_turns(self):
        manager = ConversationContextManager(max_chars=2_000)
        history = [
            {"role": "user", "content": "old user " * 100},
            {"role": "assistant", "content": "old assistant " * 100},
            {"role": "user", "content": "recent user"},
            {"role": "assistant", "content": "recent answer"},
        ]
        messages = manager.build("system", history, "current question")
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("Earlier conversation summary", messages[1]["content"])
        self.assertEqual(messages[-1]["content"], "current question")


if __name__ == "__main__":
    unittest.main()
