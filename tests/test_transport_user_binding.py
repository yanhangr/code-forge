"""HTTP contract checks for the Platform-facing Session/Message API."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from code_forge.contracts import DomainError
from code_forge.transport.server import create_server


def write_skill(root: Path, name: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                f"name: {name}",
                'version: "1"',
                "description: transport test skill",
                "---",
                "",
                "Use the default skill root.",
            ]
        ),
        encoding="utf-8",
    )


def parse_sse(text: str) -> list[dict]:
    events: list[dict] = []
    payload: dict | None = None
    event_type: str | None = None
    for line in text.splitlines():
        if line.startswith("event: "):
            event_type = line.removeprefix("event: ")
        elif line.startswith("data: "):
            payload = json.loads(line.removeprefix("data: "))
        elif not line and payload is not None:
            assert event_type is not None
            events.append({"event": event_type, "envelope": payload})
            payload = None
            event_type = None
    return events


class PlatformApiTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime_root = Path(self.temp.name) / "runtime"
        self.storage_root = Path(self.temp.name) / "nas"
        self.user_path = self.storage_root / "T001" / "users" / "U001"
        write_skill(self.user_path / "config" / "skills", "analysis")

        self.server = create_server(
            "127.0.0.1",
            0,
            root=self.runtime_root,
            env={},
        )
        self.addCleanup(self.server.runtime.stop_worker)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        expected_status: int | None = None,
    ) -> tuple[int, dict | str]:
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            self.base_url + path,
            data=encoded,
            method=method,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        try:
            with urlopen(request, timeout=15) as response:
                raw = response.read().decode("utf-8")
                status = response.status
                content_type = response.headers.get("Content-Type", "")
        except HTTPError as error:
            raw = error.read().decode("utf-8")
            status = error.code
            content_type = error.headers.get("Content-Type", "")
        if expected_status is not None:
            self.assertEqual(status, expected_status, raw)
        return status, raw if "text/event-stream" in content_type else json.loads(raw)

    def test_create_send_disconnect_state_and_history_contract(self):
        _, created = self.request(
            "POST",
            "/v1/create-session",
            body={
                "storage_root": self.storage_root.as_posix(),
                "user_rel_path": "T001/users/U001",
                "project_ref": "P001",
            },
            expected_status=201,
        )
        assert isinstance(created, dict)
        self.assertEqual(created["code"], "0000")
        session = created["data"]
        self.assertEqual(session["project_rel_path"], "workspace/projects/P001")
        self.assertNotIn("run_id", session)
        session_storage = self.user_path / "sessions" / session["session_id"]
        self.assertTrue((session_storage / "session.json").is_file())

        _, other = self.request(
            "POST",
            "/v1/create-session",
            body={
                "storage_root": self.storage_root.as_posix(),
                "user_rel_path": "T001/users/U002",
                "project_ref": "P002",
            },
            expected_status=201,
        )
        assert isinstance(other, dict)
        _, filtered = self.request(
            "GET",
            "/v1/list-sessions?user_rel_path=T001%2Fusers%2FU001&limit=100",
            expected_status=200,
        )
        assert isinstance(filtered, dict)
        self.assertEqual(
            [item["session_id"] for item in filtered["data"]["items"]],
            [session["session_id"]],
        )
        self.assertNotIn(
            other["data"]["session_id"], [item["session_id"] for item in filtered["data"]["items"]]
        )

        _, updated = self.request(
            "POST",
            "/v1/update-session",
            body={"session_id": session["session_id"], "title": "销售分析"},
            expected_status=200,
        )
        assert isinstance(updated, dict)
        self.assertEqual(updated["data"]["title"], "销售分析")
        self.assertEqual(
            json.loads((session_storage / "session.json").read_text(encoding="utf-8"))["title"],
            "销售分析",
        )

        _, stream_text = self.request(
            "POST",
            "/v1/send-message",
            body={
                "session_id": session["session_id"],
                "input": "```python\nprint(2 + 3)\n```",
            },
            expected_status=200,
        )
        assert isinstance(stream_text, str)
        events = parse_sse(stream_text)
        self.assertGreaterEqual(len(events), 4)
        self.assertEqual(events[0]["event"], "message.accepted")
        self.assertEqual(events[-1]["event"], "message.finished")
        event_types = [item["event"] for item in events]
        self.assertIn("message.started", event_types)
        self.assertIn("skill.activated", event_types)
        self.assertIn("tool.output", event_types)
        self.assertIn("message.completed", event_types)
        sequence = [item["envelope"]["data"]["seq"] for item in events]
        self.assertEqual(sequence, sorted(sequence))
        self.assertEqual(sequence, list(range(sequence[0], sequence[0] + len(sequence))))
        self.assertNotIn("run_id", json.dumps(events))
        message_id = events[0]["envelope"]["data"]["message_id"]

        prepared = next(item for item in events if item["event"] == "tool.prepared")
        self.assertEqual(prepared["envelope"]["data"]["data"]["tool_ref"], "python")
        self.assertEqual(prepared["envelope"]["data"]["data"]["input"], "print(2 + 3)\n")
        outputs = [item for item in events if item["event"] == "tool.output"]
        self.assertTrue(outputs)
        self.assertEqual(
            "".join(item["envelope"]["data"]["data"]["text"] for item in outputs),
            "5\n",
        )
        finished = next(item for item in events if item["event"] == "tool.finished")
        self.assertEqual(finished["envelope"]["data"]["data"]["status"], "SUCCEEDED")
        self.assertNotIn("result_ref", json.dumps(events))

        transcript = session_storage / "transcript-000001.jsonl"
        for _ in range(50):
            lines = (
                transcript.read_text(encoding="utf-8").splitlines() if transcript.is_file() else []
            )
            records = [json.loads(line) for line in lines if line]
            if {record["role"] for record in records} == {"user", "assistant"}:
                break
            threading.Event().wait(0.02)
        self.assertEqual([record["role"] for record in records], ["user", "assistant"])
        self.assertEqual(records[0]["content"], "```python\nprint(2 + 3)\n```")
        self.assertEqual(records[1]["content"], "5")
        self.assertTrue(
            (self.user_path / "tool-output" / session["session_id"] / message_id).is_dir()
        )

        _, message = self.request(
            "GET",
            f"/v1/get-message?message_id={message_id}",
            expected_status=200,
        )
        assert isinstance(message, dict)
        self.assertEqual(message["data"]["message_id"], message_id)
        self.assertEqual(message["data"]["status"], "SUCCEEDED")
        self.assertEqual(message["data"]["task_outcome"], "completed")
        self.assertEqual(message["data"]["output"], "5")

        _, state = self.request(
            "GET",
            f"/v1/get-session-state?session_id={session['session_id']}",
            expected_status=200,
        )
        assert isinstance(state, dict)
        self.assertEqual(state["data"]["current_message"]["message_id"], message_id)
        self.assertEqual(
            state["data"]["event_cursor"],
            f"{session['session_id']}:{sequence[-1]}",
        )

        _, history = self.request(
            "GET",
            f"/v1/list-session-messages?session_id={session['session_id']}",
            expected_status=200,
        )
        assert isinstance(history, dict)
        self.assertEqual([item["message_id"] for item in history["data"]["items"]], [message_id])

        _, replay = self.request(
            "GET",
            f"/v1/list-session-events?session_id={session['session_id']}&after_seq=0",
            expected_status=200,
        )
        assert isinstance(replay, dict)
        self.assertEqual(replay["data"]["items"][0]["type"], "message.accepted")
        self.assertEqual(replay["data"]["next_after_seq"], sequence[-1])

    def test_create_session_rebinds_workspace_when_storage_root_changes(self):
        _, created = self.request(
            "POST",
            "/v1/create-session",
            body={
                "storage_root": self.storage_root.as_posix(),
                "user_rel_path": "T001/users/U001",
                "project_ref": "P001",
            },
            expected_status=201,
        )
        assert isinstance(created, dict)
        first = created["data"]

        migrated_root = Path(self.temp.name) / "nas-new"
        _, migrated = self.request(
            "POST",
            "/v1/create-session",
            body={
                "storage_root": migrated_root.as_posix(),
                "user_rel_path": "T001/users/U001",
                "project_ref": "P001",
            },
            expected_status=201,
        )
        assert isinstance(migrated, dict)
        second = migrated["data"]
        self.assertNotEqual(second["session_id"], first["session_id"])
        self.assertEqual(second["project_rel_path"], "workspace/projects/P001")

        rebound = self.server.runtime.store.get_session_unscoped(first["session_id"])
        second_row = self.server.runtime.store.get_session_unscoped(second["session_id"])
        assert rebound is not None and second_row is not None
        self.assertEqual(rebound["workspace_id"], second_row["workspace_id"])
        self.assertEqual(rebound["storage_root"], migrated_root.resolve().as_posix())
        self.assertEqual(
            rebound["project_path"],
            (migrated_root / "T001/users/U001/workspace/projects/P001").resolve().as_posix(),
        )
        self.assertTrue(
            (migrated_root / "T001/users/U001/sessions" / second["session_id"]).is_dir()
        )

    def test_create_session_rejects_changed_user_identity(self):
        self.request(
            "POST",
            "/v1/create-session",
            body={
                "storage_root": self.storage_root.as_posix(),
                "user_rel_path": "T001/users/U001",
                "project_ref": "P001",
            },
            expected_status=201,
        )
        workspace_id = self.server.runtime.store.list_bound_sessions()[0]["workspace_id"]
        with self.server.runtime.store._transaction() as conn:
            conn.execute(
                "UPDATE workspaces SET user_ref = 'u-tampered' WHERE id = ?",
                (workspace_id,),
            )
        status, payload = self.request(
            "POST",
            "/v1/create-session",
            body={
                "storage_root": self.storage_root.as_posix(),
                "user_rel_path": "T001/users/U001",
                "project_ref": "P001",
            },
        )
        self.assertEqual(status, 409)
        assert isinstance(payload, dict)
        self.assertEqual(payload["code"], "1004")

    def test_busy_session_returns_business_code_1005(self):
        _, created = self.request(
            "POST",
            "/v1/create-session",
            body={
                "storage_root": self.storage_root.as_posix(),
                "user_rel_path": "T001/users/U001",
                "project_ref": "P001",
            },
        )
        assert isinstance(created, dict)
        session_id = created["data"]["session_id"]
        self.server.runtime.stop_worker()

        result: dict[str, object] = {}

        def open_stream() -> None:
            try:
                self.request(
                    "POST",
                    "/v1/send-message",
                    body={"session_id": session_id, "input": "first"},
                )
            except Exception as error:  # pragma: no cover - assertion below carries details
                result["error"] = error

        thread = threading.Thread(target=open_stream, daemon=True)
        thread.start()
        for _ in range(50):
            if self.server.runtime.store.session_event_cursor(session_id):
                break
            threading.Event().wait(0.02)
        status, payload = self.request(
            "POST",
            "/v1/send-message",
            body={"session_id": session_id, "input": "second"},
        )
        self.assertEqual(status, 409)
        assert isinstance(payload, dict)
        self.assertEqual(payload["code"], "1005")

    def test_cancel_message_is_public_and_idempotent(self):
        _, created = self.request(
            "POST",
            "/v1/create-session",
            body={
                "storage_root": self.storage_root.as_posix(),
                "user_rel_path": "T001/users/U001",
                "project_ref": "P001",
            },
        )
        assert isinstance(created, dict)
        session_id = created["data"]["session_id"]
        self.server.runtime.stop_worker()

        result: dict[str, object] = {}

        def open_stream() -> None:
            try:
                result["response"] = self.request(
                    "POST",
                    "/v1/send-message",
                    body={"session_id": session_id, "input": "first"},
                )
            except Exception as error:  # pragma: no cover - assertion below carries details
                result["error"] = error

        thread = threading.Thread(target=open_stream, daemon=True)
        thread.start()
        for _ in range(50):
            if self.server.runtime.store.session_event_cursor(session_id):
                break
            threading.Event().wait(0.02)

        current = self.server.runtime.store.get_current_message(session_id)
        self.assertIsNotNone(current)
        status, cancelled = self.request(
            "POST",
            "/v1/cancel-message",
            body={"message_id": current["id"]},
        )
        self.assertEqual(status, 200)
        assert isinstance(cancelled, dict)
        self.assertEqual(cancelled["code"], "0000")
        self.assertEqual(cancelled["data"]["status"], "CANCELLED")
        message_id = cancelled["data"]["message_id"]

        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", result)
        _, stream_text = result["response"]
        assert isinstance(stream_text, str)
        events = parse_sse(stream_text)
        self.assertEqual(
            [event["event"] for event in events][-2:],
            ["message.cancel_requested", "message.finished"],
        )
        self.assertEqual(events[-1]["envelope"]["data"]["data"]["status"], "CANCELLED")

        _, repeated = self.request(
            "POST",
            "/v1/cancel-message",
            body={"message_id": message_id},
        )
        assert isinstance(repeated, dict)
        self.assertEqual(repeated["data"]["status"], "CANCELLED")
        _, replay = self.request(
            "GET",
            f"/v1/list-session-events?session_id={session_id}&after_seq=0",
        )
        assert isinstance(replay, dict)
        self.assertEqual(
            sum(item["type"] == "message.cancel_requested" for item in replay["data"]["items"]),
            1,
        )

    def test_startup_survives_session_with_missing_content_object(self):
        _, created = self.request(
            "POST",
            "/v1/create-session",
            body={
                "storage_root": self.storage_root.as_posix(),
                "user_rel_path": "T001/users/U001",
                "project_ref": "P001",
            },
            expected_status=201,
        )
        assert isinstance(created, dict)
        session_id = created["data"]["session_id"]
        self.request(
            "POST",
            "/v1/send-message",
            body={"session_id": session_id, "input": "first"},
            expected_status=200,
        )
        self.server.runtime.stop_worker()

        snapshots = sorted((self.user_path / "sessions" / session_id / "snapshots").glob("*.json"))
        self.assertTrue(snapshots)
        for snapshot in snapshots:
            snapshot.unlink()

        restarted = create_server("127.0.0.1", 0, root=self.runtime_root, env={})
        self.addCleanup(restarted.runtime.stop_worker)
        self.addCleanup(restarted.server_close)
        self.assertIn(
            session_id,
            [item["id"] for item in restarted.runtime.store.list_bound_sessions()],
        )
        with self.assertRaises(DomainError):
            restarted.runtime.store.list_messages_public(session_id, None, 10)


if __name__ == "__main__":
    unittest.main()
