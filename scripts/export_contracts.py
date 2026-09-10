"""Generate reviewed wire vocabulary and OpenAPI; does not implement a server.
Run: PYTHONPATH=src python3 scripts/export_contracts.py
"""

import json
from pathlib import Path

from code_forge.contracts import ErrorCode, EventType, RunStatus, TaskOutcome, ToolStatus
from code_forge.state_machine import ALLOWED_TRANSITIONS

ROOT = Path(__file__).resolve().parents[1]


def ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def string(**kwargs):
    return {"type": "string", **kwargs}


def obj(properties, required):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def array(item):
    return {"type": "array", "items": item}


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


uuid = string(format="uuid")
date = string(format="date-time")
integer = {"type": "integer", "minimum": 0}

schemas = {
    "RunStatus": string(enum=[s.value for s in RunStatus]),
    "TaskOutcome": string(enum=[s.value for s in TaskOutcome]),
    "ToolStatus": string(enum=[s.value for s in ToolStatus]),
    "EventType": string(enum=[s.value for s in EventType]),
    "ErrorCode": string(enum=[s.value for s in ErrorCode]),
    "ExecutionContext": obj(
        {
            "scope_id": string(default="default"),
            "actor_ref": nullable(string(maxLength=200)),
            "external_ref": nullable(string(maxLength=200)),
        },
        [],
    ),
    "SkillBinding": obj(
        {"name": string(minLength=1, maxLength=100), "version": nullable(string(maxLength=100))},
        ["name"],
    ),
    "SkillRef": obj(
        {"name": string(), "version": string(), "digest": string(), "bundle_ref": string()},
        ["name", "version", "digest", "bundle_ref"],
    ),
    "SkillInfo": obj(
        {"name": string(), "version": string(), "digest": string(), "description": string()},
        ["name", "version", "digest", "description"],
    ),
    "SessionCreate": obj(
        {
            "title": string(default="New session", maxLength=200),
            "external_ref": nullable(string(maxLength=200)),
            "context": ref("ExecutionContext"),
        },
        [],
    ),
    "Session": obj(
        {
            "id": uuid,
            "title": string(),
            "external_ref": nullable(string()),
            "workspace_id": uuid,
            "date_created": date,
        },
        ["id", "title", "external_ref", "workspace_id", "date_created"],
    ),
    "RunCreate": obj(
        {
            "input": string(minLength=1, maxLength=100000),
            "agent_ref": string(default="general@1"),
            "skills": {**array(ref("SkillBinding")), "maxItems": 30, "default": []},
            "context": ref("ExecutionContext"),
        },
        ["input"],
    ),
    "AcceptedRun": obj(
        {
            "id": uuid,
            "session_id": uuid,
            "status": ref("RunStatus"),
            "state_version": integer,
            "reused": {"type": "boolean"},
        },
        ["id", "session_id", "status", "state_version", "reused"],
    ),
    "RunSnapshot": obj(
        {
            "agent_digest": string(),
            "runtime_ref": string(),
            "model_profile_ref": string(),
            "execution_profile_ref": string(),
            "skills": array(ref("SkillRef")),
            "tool_refs": array(string()),
        },
        [
            "agent_digest",
            "runtime_ref",
            "model_profile_ref",
            "execution_profile_ref",
            "skills",
            "tool_refs",
        ],
    ),
    "Run": obj(
        {
            "id": uuid,
            "session_id": uuid,
            "run_seq": integer,
            "input": string(),
            "agent_ref": string(),
            "status": ref("RunStatus"),
            "state_version": integer,
            "task_outcome": nullable(ref("TaskOutcome")),
            "wait_reason": nullable(string()),
            "output": nullable(string()),
            "error": nullable(ref("Error")),
            "snapshot": ref("RunSnapshot"),
            "date_created": date,
            "date_updated": date,
        },
        [
            "id",
            "session_id",
            "run_seq",
            "input",
            "agent_ref",
            "status",
            "state_version",
            "task_outcome",
            "wait_reason",
            "output",
            "error",
            "snapshot",
            "date_created",
            "date_updated",
        ],
    ),
    "Error": obj(
        {
            "code": ref("ErrorCode"),
            "message": string(),
            "retryable": {"type": "boolean"},
            "request_id": string(),
        },
        ["code", "message", "retryable", "request_id"],
    ),
    "ErrorResponse": obj({"error": ref("Error")}, ["error"]),
    "Event": obj(
        {
            "event_id": uuid,
            "run_id": uuid,
            "seq": {"type": "integer", "minimum": 1},
            "schema_version": string(enum=["1"]),
            "type": ref("EventType"),
            "occurred_at": date,
            "data": {"type": "object", "additionalProperties": True},
        },
        ["event_id", "run_id", "seq", "schema_version", "type", "occurred_at", "data"],
    ),
    "PendingResponse": obj(
        {
            "id": uuid,
            "kind": string(enum=["clarification", "execution_reconciliation", "approval"]),
            "prompt": string(),
            "resolved": {"type": "boolean"},
        },
        ["id", "kind", "prompt", "resolved"],
    ),
    "ResponseCreate": obj(
        {
            "pending_id": uuid,
            "response_key": string(minLength=1, maxLength=200),
            "expected_state_version": integer,
            "text": string(minLength=1, maxLength=100000),
        },
        ["pending_id", "response_key", "expected_state_version", "text"],
    ),
    "RunSnapshotView": obj(
        {
            "run": ref("Run"),
            "event_cursor": string(),
            "pending_responses": array(ref("PendingResponse")),
        },
        ["run", "event_cursor", "pending_responses"],
    ),
    "FileInfo": obj(
        {"path": string(), "size_bytes": integer, "digest": string()},
        ["path", "size_bytes", "digest"],
    ),
    "FileContent": obj(
        {
            "path": string(),
            "content": string(),
            "digest": string(),
            "truncated": {"type": "boolean"},
        },
        ["path", "content", "digest", "truncated"],
    ),
    "Health": obj(
        {
            "status": string(enum=["ok", "degraded"]),
            "api_version": string(enum=["1"]),
            "model_configured": {"type": "boolean"},
            "execution_backend": string(enum=["local_process"]),
            "permission_mode": string(enum=["default_allow"]),
        },
        ["status", "api_version", "model_configured", "execution_backend", "permission_mode"],
    ),
}
# Audit fields on resource views are server-generated and never accepted from clients.
audit_fields = {
    "date_created": {**date, "readOnly": True},
    "created_by": string(maxLength=200, readOnly=True),
    "date_updated": {**date, "readOnly": True},
    "updated_by": string(maxLength=200, readOnly=True),
}
schemas["AuditFields"] = obj(audit_fields, list(audit_fields))
for resource in ("Session", "Run"):
    schemas[resource]["properties"].update(audit_fields)
    schemas[resource]["required"] = list(
        dict.fromkeys([*schemas[resource]["required"], *audit_fields])
    )

# Each event has a closed payload schema; Platform must not infer fields from names.
event_payloads = {
    "run.accepted": obj(
        {"status": string(enum=["QUEUED"]), "snapshot": ref("RunSnapshot")}, ["status", "snapshot"]
    ),
    "run.started": obj({"attempt_id": uuid}, ["attempt_id"]),
    "run.waiting": obj(
        {
            "status": string(enum=["WAITING_USER", "WAITING_EXTERNAL"]),
            "reason": string(),
            "pending_response": nullable(ref("PendingResponse")),
            "operation_id": nullable(uuid),
        },
        ["status", "reason", "pending_response", "operation_id"],
    ),
    "run.resumed": obj({"status": string(enum=["QUEUED"])}, ["status"]),
    "run.recovering": obj({"reason": string()}, ["reason"]),
    "run.cancel_requested": obj({"status": string(enum=["CANCELLING"])}, ["status"]),
    "run.finished": obj(
        {
            "status": string(enum=["SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"]),
            "task_outcome": nullable(ref("TaskOutcome")),
            "output": nullable(string()),
            "error": nullable(ref("Error")),
        },
        ["status", "task_outcome", "output", "error"],
    ),
    "message.delta": obj({"message_id": uuid, "text": string()}, ["message_id", "text"]),
    "message.completed": obj({"message_id": uuid, "text": string()}, ["message_id", "text"]),
    "tool.prepared": obj(
        {"operation_id": uuid, "tool_ref": string(), "input_summary": string()},
        ["operation_id", "tool_ref", "input_summary"],
    ),
    "tool.started": obj({"operation_id": uuid, "tool_ref": string()}, ["operation_id", "tool_ref"]),
    "tool.output": obj(
        {
            "operation_id": uuid,
            "stream": string(enum=["stdout", "stderr"]),
            "text": string(),
            "truncated": {"type": "boolean"},
        },
        ["operation_id", "stream", "text", "truncated"],
    ),
    "tool.finished": obj(
        {
            "operation_id": uuid,
            "status": string(enum=["SUCCEEDED", "FAILED", "CANCELLED"]),
            "result_ref": nullable(string()),
            "error": nullable(ref("Error")),
        },
        ["operation_id", "status", "result_ref", "error"],
    ),
    "tool.unknown": obj({"operation_id": uuid, "reason": string()}, ["operation_id", "reason"]),
    "skill.activated": obj(
        {"name": string(), "version": string(), "digest": string()}, ["name", "version", "digest"]
    ),
    "skill.started": obj(
        {"name": string(), "version": string(), "operation_id": uuid},
        ["name", "version", "operation_id"],
    ),
    "skill.finished": obj(
        {
            "name": string(),
            "version": string(),
            "operation_id": uuid,
            "status": string(enum=["SUCCEEDED", "FAILED", "CANCELLED"]),
        },
        ["name", "version", "operation_id", "status"],
    ),
    "workspace.committed": obj(
        {"revision_id": uuid, "changed_paths": array(string())}, ["revision_id", "changed_paths"]
    ),
}
base = schemas["Event"]["properties"]
event_refs = []
for event_type, payload in event_payloads.items():
    name = "Event_" + event_type.replace(".", "_")
    schemas[name] = obj({**base, "type": string(enum=[event_type]), "data": payload}, list(base))
    event_refs.append(ref(name))
schemas["Event"] = {
    "oneOf": event_refs,
    "discriminator": {
        "propertyName": "type",
        "mapping": {t: "#/components/schemas/Event_" + t.replace(".", "_") for t in event_payloads},
    },
}

for name, item in [
    ("SessionPage", "Session"),
    ("RunPage", "Run"),
    ("SkillPage", "SkillInfo"),
    ("FilePage", "FileInfo"),
    ("EventPage", "Event"),
]:
    schemas[name] = obj(
        {"items": array(ref(item)), "next_cursor": nullable(string())}, ["items", "next_cursor"]
    )
schemas["EventPage"] = obj(
    {"items": array(ref("Event")), "next_after_seq": nullable(integer)}, ["items", "next_after_seq"]
)
paths = {}
errors = {
    str(n): {
        "description": label,
        "content": {"application/json": {"schema": ref("ErrorResponse")}},
    }
    for n, label in [
        (400, "Invalid request"),
        (403, "Capability denied"),
        (404, "Not found"),
        (409, "Conflict"),
        (422, "Validation error"),
        (429, "Resource limit"),
        (503, "Dependency unavailable"),
    ]
}


def add(path, method, op, response, body=None, status=200, query=(), idempotent=False):
    import re

    params = [
        {"name": name, "in": "path", "required": True, "schema": uuid}
        for name in re.findall(r"{([^}]+)}", path)
    ]
    params += [{"name": name, "in": "query", "schema": schema} for name, schema in query]
    if path.startswith("/v1/"):
        params.append(
            {
                "name": "X-Forge-Scope",
                "in": "header",
                "schema": string(default="default"),
                "description": "Trusted integration scope. This validation stage defaults to default; not an authentication credential.",
            }
        )
    if idempotent:
        params += [
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "schema": string(minLength=1, maxLength=200),
            }
        ]
    entry = {
        "operationId": op,
        "parameters": params,
        "responses": {
            str(status): {
                "description": "Success",
                "content": {"application/json": {"schema": ref(response)}},
            },
            **errors,
        },
    }
    if body:
        entry["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": ref(body)}},
        }
    paths.setdefault(path, {})[method] = entry


paging = (
    ("cursor", string()),
    ("limit", {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}),
)
add("/health", "get", "health", "Health")
add("/v1/sessions", "post", "createSession", "Session", "SessionCreate", 201, idempotent=True)
add("/v1/sessions", "get", "listSessions", "SessionPage", query=paging)
add("/v1/sessions/{session_id}", "get", "getSession", "Session")
add(
    "/v1/sessions/{session_id}/runs",
    "post",
    "createRun",
    "AcceptedRun",
    "RunCreate",
    202,
    idempotent=True,
)
add("/v1/sessions/{session_id}/runs", "get", "listRuns", "RunPage", query=paging)
add("/v1/runs/{run_id}", "get", "getRun", "Run")
add("/v1/runs/{run_id}/cancel", "post", "cancelRun", "Run", status=202)
add("/v1/runs/{run_id}/responses", "post", "respondToRun", "Run", "ResponseCreate", 202)
add("/v1/runs/{run_id}/snapshot", "get", "getRunSnapshot", "RunSnapshotView")
add(
    "/v1/runs/{run_id}/event-history",
    "get",
    "listRunEvents",
    "EventPage",
    query=(
        ("after_seq", integer),
        ("limit", {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200}),
    ),
)
add("/v1/runs/{run_id}/events", "get", "streamRunEvents", "Event", query=(("after_seq", integer),))
stream = paths["/v1/runs/{run_id}/events"]["get"]
stream["parameters"].append({"name": "Last-Event-ID", "in": "header", "schema": string()})
stream["responses"]["200"] = {
    "description": "SSE id is run_id:seq; data is Event JSON. Replay then live events. Disconnect does not cancel Run.",
    "content": {"text/event-stream": {"schema": {"type": "string"}}},
}
add("/v1/skills", "get", "listSkills", "SkillPage", query=paging)
add("/v1/sessions/{session_id}/files", "get", "listWorkspaceFiles", "FilePage", query=paging)
add(
    "/v1/sessions/{session_id}/files/content",
    "get",
    "readWorkspaceFile",
    "FileContent",
    query=(("path", string(minLength=1, maxLength=1000)),),
)
next(
    p
    for p in paths["/v1/sessions/{session_id}/files/content"]["get"]["parameters"]
    if p["name"] == "path"
)["required"] = True

spec = {
    "openapi": "3.1.0",
    "info": {
        "title": "Code Forge Runtime Integration Contract",
        "version": "0.2.0",
        "description": "Architecture contract; endpoints are not implemented yet. Trusted local verification uses default permissions and local subprocess execution.",
    },
    "servers": [{"url": "http://127.0.0.1:8000"}],
    "paths": paths,
    "components": {"schemas": schemas},
}

if __name__ == "__main__":
    (ROOT / "docs/api/openapi.json").write_text(
        json.dumps(spec, ensure_ascii=False, indent=2) + "\n"
    )
    machine = {
        s.value: sorted(t.value for t in targets) for s, targets in ALLOWED_TRANSITIONS.items()
    }
    (ROOT / "docs/api/run-state-machine.json").write_text(json.dumps(machine, indent=2) + "\n")
