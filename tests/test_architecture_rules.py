"""Architecture completeness checks; these do not execute the Agent integration cases."""

import ast
import importlib.util
import json
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from code_forge.audit import on_insert, on_update
from code_forge.contracts import DomainError, RunStatus, TaskOutcome
from code_forge.state_machine import ALLOWED_TRANSITIONS, RunState, transition

ROOT = Path(__file__).resolve().parents[1]
AUDIT = {"date_created", "created_by", "date_updated", "updated_by"}


class ArchitectureRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = (ROOT / "db/migrations/001_runtime.sql").read_text()
        cls.tables = re.findall(r"CREATE TABLE runtime\.(\w+) \(\n(.*?)\n\);", cls.sql, re.S)

    def test_every_table_has_audit_and_every_column_has_comment(self):
        self.assertEqual(len(self.tables), 10)
        for table, body in self.tables:
            columns = set(re.findall(r"^ ([a-z][a-z0-9_]*)\s+", body, re.M))
            self.assertTrue(AUDIT <= columns, table)
            self.assertNotIn("created_at", columns)
            self.assertNotIn("updated_at", columns)
            self.assertIn(f"COMMENT ON TABLE runtime.{table} IS", self.sql)
            for column in columns:
                self.assertIn(f"COMMENT ON COLUMN runtime.{table}.{column} IS", self.sql)
            for column in AUDIT:
                self.assertRegex(body, rf"\b{column}\s+[^\n]*NOT NULL")

    def test_indexes_are_simple_primary_normal_or_unique(self):
        statements = re.findall(r"CREATE (?:UNIQUE )?INDEX\b.*?;", self.sql, re.S)
        self.assertEqual(len(statements), 6)
        for statement in statements:
            self.assertRegex(
                statement, r"^CREATE (?:UNIQUE )?INDEX \w+ ON runtime\.\w+\([\w, ]+\);$"
            )
        for table, body in self.tables:
            for cols in re.findall(r"(?:UNIQUE|PRIMARY KEY)\s*\((.*?)\)", body):
                self.assertRegex(cols, r"^\w+(?:,\s*\w+)*$", table)
        self.assertIn("active_attempt_id uuid", self.sql)
        self.assertIn("run_active_attempt_fk", self.sql)
        self.assertIn("session_active_run_fk", self.sql)
        self.assertIn("workspace_active_attempt_fk", self.sql)
        self.assertIn("idx_runs_scope_status_due_created", self.sql)
        self.assertNotIn("one_live_attempt", self.sql)
        self.assertNotIn("one_running_run_per_session", self.sql)

    def test_resource_api_uses_readonly_audit_fields(self):
        spec = json.loads((ROOT / "docs/api/openapi.json").read_text())
        for name in ("Session", "Message"):
            schema = spec["components"]["schemas"][name]
            for field in ("date_created", "date_updated"):
                self.assertIn(field, schema["required"])
                self.assertTrue(schema["properties"][field]["readOnly"])
            self.assertNotIn("created_at", schema["properties"])
            self.assertNotIn("updated_at", schema["properties"])

    def test_every_flow_branch_has_a_case_specification(self):
        registry = json.loads((ROOT / "docs/testing/agent-workflow-cases.json").read_text())
        cases = registry["cases"]
        expected = {f"B{i:02}" for i in range(1, 49)}
        self.assertEqual(len(cases), len(expected))
        self.assertEqual({c["branch_id"] for c in cases}, expected)
        self.assertEqual(len({c["case_id"] for c in cases}), len(cases))
        workflow = (ROOT / "docs/agent-workflow.md").read_text()
        self.assertEqual(set(re.findall(r"\bB\d{2}\b", workflow)), expected)
        for case in cases:
            self.assertEqual(case["case_id"], "TC-" + case["branch_id"])
            self.assertEqual(case["status"], "SPEC_ONLY")
            for field in ("given", "when", "then"):
                self.assertTrue(case[field], (case["case_id"], field))
            self.assertTrue(case["planned_test"].startswith("tests/integration/"))
            for evidence in case["core_evidence"]:
                filename, selector = evidence.split("::")
                cls_name, method = selector.split(".")
                tree = ast.parse((ROOT / filename).read_text())
                cls = next(
                    n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name
                )
                self.assertIn(method, {getattr(n, "name", "") for n in cls.body})

    def test_case_document_is_generated_from_reviewed_registry(self):
        module_spec = importlib.util.spec_from_file_location(
            "workflow_cases", ROOT / "scripts/export_workflow_cases.py"
        )
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        registry = json.loads((ROOT / "docs/testing/agent-workflow-cases.json").read_text())
        self.assertEqual(
            module.render(registry), (ROOT / "docs/testing/agent-workflow-cases.md").read_text()
        )

    def test_core_has_no_framework_or_infrastructure_dependency(self):
        for name in ("contracts", "state_machine", "service", "ports", "audit"):
            tree = ast.parse((ROOT / f"src/code_forge/{name}.py").read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.level:
                        self.assertIn(
                            node.module, {"contracts", "state_machine", "service", "ports", "audit"}
                        )
                    else:
                        self.assertIn(node.module.split(".")[0], sys.stdlib_module_names)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertIn(alias.name.split(".")[0], sys.stdlib_module_names)


class AuditRulesTests(unittest.TestCase):
    def setUp(self):
        self.time = datetime(2026, 9, 10, tzinfo=timezone.utc)

    def test_insert_initializes_all_four_fields(self):
        audit = on_insert("actor:123", self.time)
        self.assertEqual(audit.date_created, self.time)
        self.assertEqual(audit.date_updated, self.time)
        self.assertEqual(audit.created_by, "actor:123")
        self.assertEqual(audit.updated_by, "actor:123")

    def test_worker_update_preserves_creator(self):
        original = on_insert("actor:123", self.time)
        updated = on_update(
            original, "system:runtime/worker", self.time + timedelta(seconds=1), changed=True
        )
        self.assertEqual(updated.date_created, original.date_created)
        self.assertEqual(updated.created_by, original.created_by)
        self.assertGreater(updated.date_updated, original.date_updated)
        self.assertEqual(updated.updated_by, "system:runtime/worker")

    def test_idempotent_noop_does_not_touch_audit(self):
        original = on_insert("actor:123", self.time)
        self.assertIs(
            on_update(
                original, "system:runtime/api", self.time + timedelta(seconds=5), changed=False
            ),
            original,
        )

    def test_invalid_actor_or_clock_is_rejected(self):
        for actor, timestamp in [
            ("", self.time),
            (" ", self.time),
            ("a" * 201, self.time),
            ("actor", self.time.replace(tzinfo=None)),
        ]:
            with self.assertRaises(DomainError):
                on_insert(actor, timestamp)
        original = on_insert("actor", self.time)
        with self.assertRaises(DomainError):
            on_update(original, "worker", self.time - timedelta(seconds=1), changed=True)


class StateMatrixTests(unittest.TestCase):
    def test_all_legal_and_illegal_edges_enforce_state_invariants(self):
        for source in RunStatus:
            for target in RunStatus:
                with self.subTest(source=source, target=target):
                    state = RunState("r", source, state_version=7)
                    outcome = TaskOutcome.COMPLETED if target == RunStatus.SUCCEEDED else None
                    reason = (
                        "test-wait"
                        if target in {RunStatus.WAITING_USER, RunStatus.WAITING_EXTERNAL}
                        else None
                    )
                    if target not in ALLOWED_TRANSITIONS[source]:
                        with self.assertRaises(DomainError):
                            transition(
                                state,
                                target,
                                expected_version=7,
                                task_outcome=outcome,
                                reason=reason,
                            )
                    else:
                        changed = transition(
                            state, target, expected_version=7, task_outcome=outcome, reason=reason
                        )
                        self.assertEqual(changed.after.status, target)
                        self.assertEqual(changed.after.state_version, 8)
                        self.assertEqual(changed.after.task_outcome, outcome)
                        self.assertEqual(changed.after.wait_reason, reason)
                        self.assertEqual(state.state_version, 7)

    def test_waiting_requires_reason_and_nonfinal_cannot_have_outcome(self):
        running = RunState("r", RunStatus.RUNNING)
        for target in (RunStatus.WAITING_USER, RunStatus.WAITING_EXTERNAL):
            with self.assertRaises(DomainError):
                transition(running, target, expected_version=0)
        with self.assertRaises(DomainError):
            transition(
                running, RunStatus.FAILED, expected_version=0, task_outcome=TaskOutcome.BLOCKED
            )
