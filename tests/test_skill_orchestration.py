"""Skill orchestration tests for manual filesystem skills."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from code_forge.contracts import (
    DomainError,
    ErrorCode,
    RunRequest,
    SkillBinding,
)
from code_forge.harness.tooling import active_skill_names, select_active_skill
from code_forge.skills.manual_skill_resolver import ManualSkillResolver


def write_skill(root: Path, name: str, version: str, requires: str | None = None) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"name: {name}",
        f"version: {version}",
        "description: test skill",
    ]
    if requires:
        lines.append(f"requires: {requires}")
    lines.extend(["---", "", "skill body"])
    (skill_dir / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")


class SkillOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.skills_root = root / "skills"
        self.snapshot_root = root / "snapshots"
        self.skills_root.mkdir()
        self.resolver = ManualSkillResolver(self.skills_root, self.snapshot_root)

    async def test_one_skill_resolves_multiple_nested_skills(self):
        write_skill(self.skills_root, "python-analysis", "1")
        write_skill(self.skills_root, "file-writer", "1")
        write_skill(
            self.skills_root,
            "analysis-report",
            "1",
            requires='["python-analysis", "file-writer"]',
        )
        snapshot = await self.resolver.resolve_and_store(
            RunRequest(
                session_id="s",
                input="report",
                skills=(SkillBinding(name="analysis-report", version="1"),),
            )
        )
        names = [skill.name for skill in snapshot.skills]
        self.assertIn("python-analysis", names)
        self.assertIn("file-writer", names)
        self.assertEqual(names[-1], "analysis-report")
        self.assertEqual(len({skill.digest for skill in snapshot.skills}), 3)
        self.assertTrue(all(Path(skill.bundle_ref).exists() for skill in snapshot.skills))

    async def test_missing_nested_skill_is_rejected(self):
        write_skill(self.skills_root, "broken-orchestrator", "1", requires='["missing-skill"]')
        with self.assertRaises(DomainError) as error:
            await self.resolver.resolve_and_store(
                RunRequest(
                    session_id="s",
                    input="broken",
                    skills=(SkillBinding(name="broken-orchestrator", version="1"),),
                )
            )
        self.assertEqual(error.exception.code, ErrorCode.SKILL_NOT_FOUND)

    async def test_nested_skill_cycle_is_rejected(self):
        write_skill(self.skills_root, "a", "1", requires='["b"]')
        write_skill(self.skills_root, "b", "1", requires='["a"]')
        with self.assertRaises(DomainError) as error:
            await self.resolver.resolve_and_store(
                RunRequest(
                    session_id="s",
                    input="cycle",
                    skills=(SkillBinding(name="a", version="1"),),
                )
            )
        self.assertEqual(error.exception.code, ErrorCode.SKILL_INCOMPATIBLE)


class FrozenSkillSelectionTests(unittest.TestCase):
    def test_model_can_only_select_frozen_candidates(self):
        run = {
            "config_snapshot": {
                "skills": [{"name": "analysis-report"}, {"name": "python-analysis"}]
            }
        }
        self.assertEqual(
            active_skill_names(run),
            ("analysis-report", "python-analysis"),
        )
        self.assertEqual(select_active_skill(run, "python-analysis"), ("python-analysis", None))
        skill_name, error = select_active_skill(run, "filesystem")
        self.assertIsNone(skill_name)
        self.assertIsNotNone(error)
        self.assertIn("filesystem", error)

    def test_no_active_skill_ignores_model_declared_skill(self):
        self.assertEqual(select_active_skill({}, "filesystem"), ("", None))


if __name__ == "__main__":
    unittest.main()
