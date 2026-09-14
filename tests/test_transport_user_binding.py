"""HTTP contract checks for the Platform-facing Session/Message API."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

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

        _, updated = self.request(
            "POST",
            "/v1/update-session",
            body={"session_id": session["session_id"], "title": "销售分析"},
            expected_status=200,
        )
        assert isinstance(updated, dict)
        self.assertEqual(updated["data"]["title"], "销售分析")

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


if __name__ == "__main__":
    unittest.main()
