"""User binding, scoped Skill paths, and workspace lease tests."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from code_forge.contracts import (
    DomainError,
    ErrorCode,
    ExecutionContext,
    RunRequest,
    RunStatus,
    SkillBinding,
    TaskOutcome,
    UserBinding,
)
from code_forge.execution.local_process_backend import LocalProcessBackend
from code_forge.harness.local import LocalDeterministicHarness
from code_forge.persistence.sqlite_store import SqliteRunRepository, SqliteRuntimeStore
from code_forge.ports import DefaultAllowAuthorization
from code_forge.runtime.app import AgentRuntime
from code_forge.skills.manual_skill_resolver import ManualSkillResolver
from code_forge.workspace.store import WorkspaceStore


def write_skill(root: Path, name: str, version: str, body: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                f"name: {name}",
                f"version: {version}",
                "description: test skill",
                "---",
                "",
                body,
            ]
        ),
        encoding="utf-8",
    )


class UserBindingValidationTests(unittest.TestCase):
    def test_project_path_must_be_below_user_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(DomainError) as error:
                UserBinding(
                    scope_id="user-1",
                    tenant_ref="tenant-1",
                    user_ref="user-1",
                    user_path=str(root / "user"),
                    project_ref="project-1",
                    project_path=str(root / "outside"),
                )
            self.assertEqual(error.exception.code, ErrorCode.INVALID_REQUEST)

    def test_scope_matches_user_and_project_path_can_be_derived(self):
        with tempfile.TemporaryDirectory() as temp:
            user_path = Path(temp) / "users" / "user-1"
            binding = UserBinding(
                scope_id="user-1",
                tenant_ref="tenant-1",
                user_ref="user-1",
                user_path=str(user_path),
                project_ref="project-1",
            )
            self.assertEqual(
                binding.project_path,
                (user_path / "workspace" / "projects" / "project-1").resolve().as_posix(),
            )
            self.assertEqual(
                binding.default_skill_path,
                (user_path / "config" / "skills").resolve().as_posix(),
            )
            with self.assertRaises(DomainError):
                UserBinding(
                    scope_id="user-a",
                    tenant_ref="tenant-1",
                    user_ref="user-b",
                    user_path=str(user_path),
                    project_ref="project-1",
                )


class BoundRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.user_path = root / "users" / "user-1"
        self.project_path = self.user_path / "workspace" / "projects" / "project-1"
        self.project_path.mkdir(parents=True)
        self.alternate_path = self.user_path / "workspace" / "projects" / "project-2"
        self.alternate_path.mkdir(parents=True)

        self.store = SqliteRuntimeStore(root / "state.db")
        self.resolver = ManualSkillResolver(root / "legacy-skills", root / "snapshots")
        self.workspace = WorkspaceStore(root / "legacy-workspaces")
        self.execution = LocalProcessBackend(self.workspace, root / "operations")
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
        self.binding = UserBinding(
            scope_id="user-1",
            tenant_ref="tenant-1",
            user_ref="user-1",
            user_path=str(self.user_path),
            project_ref="project-1",
        )
        self.context = ExecutionContext(
            scope_id="user-1",
            actor_ref="tester",
            user_binding=self.binding,
        )

        self.session = self.runtime.create_session(
            scope_id="user-1",
            idempotency_key="session-key",
            title="user session",
            external_ref=None,
            context=self.context,
        )

    async def _submit(
        self,
        text: str,
        key: str,
        context: ExecutionContext | None = None,
        skill_paths: tuple[str, ...] | None = None,
    ):
        accepted = await self.runtime.service.submit(
            RunRequest(
                session_id=self.session["id"],
                input=text,
                context=context or self.context,
                skill_paths=skill_paths,
            ),
            key,
        )
        return self.store.get_run("user-1", accepted.id)

    async def test_default_skill_path_uses_user_config_skills(self):
        default_root = self.user_path / "config" / "skills"
        write_skill(default_root, "analysis", "1", "user default skill")
        snapshot = await self.resolver.resolve_and_store(
            RunRequest(
                session_id=self.session["id"],
                input="skill test",
                skills=(SkillBinding(name="analysis", version="1"),),
                context=self.context,
            )
        )
        self.assertEqual(snapshot.skill_path_source, "USER_DEFAULT")
        self.assertEqual(
            snapshot.effective_skill_paths,
            (default_root.resolve().as_posix(),),
        )
        skill = snapshot.skills[0]
        self.assertEqual(skill.source_kind, "USER_DEFAULT")
        self.assertIn(
            "user default skill",
            (Path(skill.bundle_ref) / "SKILL.md").read_text(encoding="utf-8"),
        )

    async def test_explicit_skill_paths_override_default_and_keep_order(self):
        default_root = self.user_path / "config" / "skills"
        first_root = self.user_path / "config" / "team-skills"
        second_root = self.user_path / "config" / "project-skills"
        write_skill(default_root, "analysis", "1", "default skill")
        write_skill(first_root, "analysis", "1", "first explicit skill")
        write_skill(second_root, "analysis", "1", "second explicit skill")

        snapshot = await self.resolver.resolve_and_store(
            RunRequest(
                session_id=self.session["id"],
                input="skill test",
                skills=(SkillBinding(name="analysis", version="1"),),
                context=self.context,
                skill_paths=(first_root.as_posix(), second_root.as_posix()),
            )
        )
        self.assertEqual(snapshot.skill_path_source, "EXPLICIT")
        self.assertEqual(
            snapshot.effective_skill_paths,
            (first_root.resolve().as_posix(), second_root.resolve().as_posix()),
        )
        skill = snapshot.skills[0]
        body = (Path(skill.bundle_ref) / "SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(skill.source_path, first_root.resolve().as_posix())
        self.assertIn("first explicit skill", body)
        self.assertNotIn("default skill", body)
        self.assertNotIn("second explicit skill", body)

    async def test_direct_skill_package_path_is_discovered_without_name_binding(self):
        package = self.user_path / "config" / "packages" / "analysis"
        write_skill(package.parent, "analysis", "1", "direct package skill")

        snapshot = await self.resolver.resolve_and_store(
            RunRequest(
                session_id=self.session["id"],
                input="skill test",
                context=self.context,
                skill_paths=(package.as_posix(),),
            )
        )

        self.assertEqual(snapshot.skill_path_source, "EXPLICIT")
        self.assertEqual(
            snapshot.effective_skill_paths,
            (package.resolve().as_posix(),),
        )
        self.assertEqual([skill.name for skill in snapshot.skills], ["analysis"])
        self.assertEqual(snapshot.skills[0].source_kind, "DIRECT_PACKAGE")

    async def test_empty_explicit_skill_paths_disable_user_default(self):
        default_root = self.user_path / "config" / "skills"
        write_skill(default_root, "analysis", "1", "default skill")
        with self.assertRaises(DomainError) as error:
            await self.resolver.resolve_and_store(
                RunRequest(
                    session_id=self.session["id"],
                    input="skill test",
                    skills=(SkillBinding(name="analysis", version="1"),),
                    context=self.context,
                    skill_paths=(),
                )
            )
        self.assertEqual(error.exception.code, ErrorCode.SKILL_NOT_FOUND)

    async def test_bound_execution_commits_project_revision(self):
        code = (
            "from pathlib import Path\n"
            "Path('result.txt').write_text('bound-ok')\n"
            "print('bound-ok')\n"
        )
        await self._submit(
            f"```python\n{code}```",
            "bound-key",
        )
        claimed = self.store.claim_next_run(
            "user-1",
            "test-worker",
            60,
            datetime.now(timezone.utc),
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["user_binding"], self.binding)
        result = await self.harness.execute(claimed)
        self.assertEqual(result["status"], RunStatus.SUCCEEDED.value)
        self.assertEqual(result["task_outcome"], TaskOutcome.COMPLETED.value)
        self.assertEqual(
            (self.project_path / "result.txt").read_text(encoding="utf-8"),
            "bound-ok",
        )
        self.assertEqual(list(self.project_path.glob("model_*")), [])
        workspace = self.store.get_workspace("user-1", claimed["workspace_id"])
        self.assertIsNotNone(workspace)
        self.assertEqual(workspace["user_ref"], "user-1")
        self.assertEqual(workspace["user_path"], self.user_path.resolve().as_posix())
        self.assertIsNotNone(workspace["current_revision"])
        revision = self.store.conn.execute(
            "SELECT * FROM workspace_revisions WHERE scope_id = ? AND workspace_id = ?",
            ("user-1", claimed["workspace_id"]),
        ).fetchone()
        self.assertIsNotNone(revision)
        self.assertTrue(Path(revision["storage_ref"]).is_dir())
        self.assertEqual(list(Path(revision["storage_ref"]).glob("model_*")), [])
        operation = self.store.conn.execute(
            """
            SELECT id FROM tool_executions
            WHERE scope_id = ? AND run_id = ? AND tool_ref = 'python'
            """,
            ("user-1", result["id"]),
        ).fetchone()
        self.assertIsNotNone(operation)
        operation_dir = self.workspace.tool_output_dir(
            self.binding,
            self.session["id"],
            result["id"],
            operation["id"],
        )
        self.assertEqual((operation_dir / "input.txt").read_text(encoding="utf-8"), code)

    async def test_same_project_sessions_share_workspace_and_writer_lease(self):
        second = self.runtime.create_session(
            scope_id="user-1",
            idempotency_key="session-key-2",
            title="second session",
            external_ref=None,
            context=self.context,
        )
        self.assertEqual(second["workspace_id"], self.session["workspace_id"])
        first_run = await self._submit("```python\nprint('first')\n```", "lease-1")
        second_run = await self._submit("```python\nprint('second')\n```", "lease-2")
        first_claim = self.store.claim_next_run(
            "user-1",
            "worker-1",
            60,
            datetime.now(timezone.utc),
        )
        self.assertEqual(first_claim["run"]["id"], first_run["id"])
        self.assertIsNone(
            self.store.claim_next_run(
                "user-1",
                "worker-2",
                60,
                datetime.now(timezone.utc),
            )
        )
        self.store.transition_run(
            "user-1",
            first_run["id"],
            expected_version=first_claim["run"]["state_version"],
            target=RunStatus.SUCCEEDED,
            task_outcome=TaskOutcome.COMPLETED,
            output="done",
        )
        second_claim = self.store.claim_next_run(
            "user-1",
            "worker-2",
            60,
            datetime.now(timezone.utc),
        )
        self.assertIsNotNone(second_claim)
        self.assertEqual(second_claim["run"]["id"], second_run["id"])

    async def test_same_key_with_different_skill_paths_conflicts(self):
        await self._submit("```python\nprint('a')\n```", "same-key", skill_paths=())
        with self.assertRaises(DomainError) as error:
            await self._submit(
                "```python\nprint('a')\n```",
                "same-key",
                skill_paths=((self.user_path / "other-skills").as_posix(),),
            )
        self.assertEqual(error.exception.code, ErrorCode.IDEMPOTENCY_CONFLICT)

    async def test_cross_user_session_lookup_is_denied(self):
        self.assertIsNone(self.store.get_session("user-2", self.session["id"]))


if __name__ == "__main__":
    unittest.main()
