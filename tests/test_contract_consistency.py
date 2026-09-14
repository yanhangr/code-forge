"""Cross-artifact checks to prevent implementing agents from drifting the contract."""

import dataclasses
import importlib.util
import json
import re
import unittest
from pathlib import Path

from code_forge.contracts import (
    AcceptedRun,
    AuditFields,
    BusinessCode,
    ErrorCode,
    EventType,
    ExecutionContext,
    MessageStatus,
    PlatformEventType,
    RunSnapshot,
    RunStatus,
    SkillBinding,
    SkillRef,
    TaskOutcome,
    ToolStatus,
    UserBinding,
)
from code_forge.state_machine import ALLOWED_TRANSITIONS

ROOT = Path(__file__).resolve().parents[1]


class ContractConsistencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = json.loads((ROOT / "docs/api/openapi.json").read_text())
        cls.schemas = cls.spec["components"]["schemas"]
        cls.sql = (ROOT / "db/migrations/001_runtime.sql").read_text()

    def test_export_is_current(self):
        module_spec = importlib.util.spec_from_file_location(
            "export_contracts", ROOT / "scripts/export_contracts.py"
        )
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        self.assertEqual(self.spec, module.spec, "Regenerate OpenAPI after changing source")
        expected = {
            s.value: sorted(t.value for t in targets) for s, targets in ALLOWED_TRANSITIONS.items()
        }
        self.assertEqual(
            json.loads((ROOT / "docs/api/run-state-machine.json").read_text()), expected
        )

    def test_enums_match_python_openapi_and_sql(self):
        for cls in (
            RunStatus,
            MessageStatus,
            TaskOutcome,
            ToolStatus,
            EventType,
            PlatformEventType,
            ErrorCode,
            BusinessCode,
        ):
            self.assertEqual(set(self.schemas[cls.__name__]["enum"]), {s.value for s in cls})
        for sql_name, cls in [
            ("run_status", RunStatus),
            ("task_outcome", TaskOutcome),
            ("tool_status", ToolStatus),
        ]:
            match = re.search(
                rf"CREATE TYPE runtime\.{sql_name} AS ENUM\s*\((.*?)\);", self.sql, re.S
            )
            self.assertIsNotNone(match)
            self.assertEqual(set(re.findall(r"'([^']+)'", match.group(1))), {s.value for s in cls})

    def test_shared_dto_fields_match(self):
        for cls in (
            AcceptedRun,
            AuditFields,
            ExecutionContext,
            RunSnapshot,
            SkillBinding,
            SkillRef,
            UserBinding,
        ):
            self.assertEqual(
                set(self.schemas[cls.__name__]["properties"]),
                {f.name for f in dataclasses.fields(cls)},
            )

    def test_all_refs_resolve(self):
        def walk(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    parts = value["$ref"].removeprefix("#/").split("/")
                    node = self.spec
                    for part in parts:
                        self.assertIn(part, node)
                        node = node[part]
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(self.spec)

    def test_each_event_has_a_closed_payload(self):
        mapping = self.schemas["Event"]["discriminator"]["mapping"]
        self.assertEqual(set(mapping), {e.value for e in PlatformEventType})
        for event_name, path in mapping.items():
            event = self.schemas[path.rsplit("/", 1)[1]]
            self.assertFalse(event["additionalProperties"])
            payload = event["properties"]["data"]
            self.assertFalse(payload["additionalProperties"])
            self.assertEqual(event["properties"]["type"]["enum"], [event_name])

    def test_api_operations_are_unique_and_no_management_product(self):
        operations = []
        for path, methods in self.spec["paths"].items():
            self.assertFalse(
                any(word in path for word in ("/roles", "/users", "/publish", "/approval"))
            )
            for op in methods.values():
                operations.append(op["operationId"])
        self.assertEqual(len(operations), len(set(operations)))
        self.assertEqual(
            set(self.spec["paths"]),
            {
                "/health",
                "/v1/create-session",
                "/v1/update-session",
                "/v1/get-session",
                "/v1/list-sessions",
                "/v1/send-message",
                "/v1/get-session-state",
                "/v1/get-message",
                "/v1/list-session-messages",
                "/v1/reply",
                "/v1/stream-session-events",
                "/v1/list-session-events",
                "/v1/list-session-files",
                "/v1/read-session-file",
            },
        )
        for methods in self.spec["paths"].values():
            for operation in methods.values():
                self.assertNotIn(
                    "Idempotency-Key",
                    {item["name"] for item in operation.get("parameters", [])},
                )

    def test_database_request_and_ownership_constraints_exist(self):
        for expected in (
            "UNIQUE(scope_id,session_id,idempotency_key)",
            "UNIQUE(scope_id,run_id,logical_call_key)",
            "UNIQUE(scope_id,run_id,seq)",
            "session_active_run_fk",
            "run_active_attempt_fk",
            "active_attempt_id",
            "session_requests",
        ):
            self.assertIn(expected, self.sql)
