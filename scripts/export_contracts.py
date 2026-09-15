"""Generate reviewed wire vocabulary and OpenAPI; does not implement a server.
Run: PYTHONPATH=src python3 scripts/export_contracts.py
"""

import json
from pathlib import Path

from code_forge.contracts import (
    BusinessCode,
    ErrorCode,
    EventType,
    MessageStatus,
    PlatformEventType,
    RunStatus,
    TaskOutcome,
    ToolStatus,
)
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
    "MessageStatus": string(enum=[s.value for s in MessageStatus]),
    "TaskOutcome": string(enum=[s.value for s in TaskOutcome]),
    "ToolStatus": string(enum=[s.value for s in ToolStatus]),
    "EventType": string(enum=[s.value for s in EventType]),
    "PlatformEventType": string(enum=[s.value for s in PlatformEventType]),
    "ErrorCode": string(enum=[s.value for s in ErrorCode]),
    "BusinessCode": string(enum=[s.value for s in BusinessCode]),
    "UserBinding": obj(
        {
            "scope_id": string(minLength=1, maxLength=200),
            "tenant_ref": string(minLength=1, maxLength=200),
            "user_ref": string(minLength=1, maxLength=200),
            "user_path": string(minLength=1),
            "project_ref": string(minLength=1, maxLength=200),
            "project_path": nullable(string(minLength=1)),
            "storage_root": string(),
            "user_rel_path": string(),
            "project_rel_path": string(),
        },
        ["scope_id", "tenant_ref", "user_ref", "user_path", "project_ref"],
    ),
    "ExecutionContext": obj(
        {
            "scope_id": string(default="default"),
            "actor_ref": nullable(string(maxLength=200)),
            "external_ref": nullable(string(maxLength=200)),
            "user_binding": nullable(ref("UserBinding")),
        },
        [],
    ),
    "SkillBinding": obj(
        {"name": string(minLength=1, maxLength=100), "version": nullable(string(maxLength=100))},
        ["name"],
    ),
    "SkillRef": obj(
        {
            "name": string(),
            "version": string(),
            "digest": string(),
            "bundle_ref": string(),
            "source_kind": string(enum=["LEGACY", "USER_DEFAULT", "EXPLICIT", "DIRECT_PACKAGE"]),
            "source_path": string(),
            "user_ref": string(),
            "project_ref": string(),
        },
        [
            "name",
            "version",
            "digest",
            "bundle_ref",
            "source_kind",
            "source_path",
            "user_ref",
            "project_ref",
        ],
    ),
    "SkillInfo": obj(
        {
            "name": string(),
            "version": string(),
            "digest": string(),
            "description": string(),
            "source_kind": string(enum=["LEGACY", "USER_DEFAULT", "EXPLICIT", "DIRECT_PACKAGE"]),
            "source_path": string(),
        },
        ["name", "version", "digest", "description", "source_kind", "source_path"],
    ),
    "SessionCreate": obj(
        {
            "storage_root": string(minLength=1),
            "user_rel_path": string(minLength=1),
            "project_ref": nullable(string(minLength=1, maxLength=200)),
            "project_rel_path": nullable(string(minLength=1)),
        },
        ["storage_root", "user_rel_path"],
    ),
    "Session": obj(
        {
            "session_id": uuid,
            "title": string(),
            "user_rel_path": string(),
            "project_ref": nullable(string()),
            "project_rel_path": string(),
            "date_created": {**date, "readOnly": True},
            "date_updated": {**date, "readOnly": True},
        },
        [
            "session_id",
            "title",
            "user_rel_path",
            "project_ref",
            "project_rel_path",
            "date_created",
            "date_updated",
        ],
    ),
    "SendMessageCreate": obj(
        {
            "session_id": uuid,
            "input": string(minLength=1, maxLength=100000),
            "agent_ref": string(default="general@1"),
            "skill_paths": nullable(
                {**array(string(minLength=1)), "maxItems": 30, "default": None}
            ),
        },
        ["session_id", "input"],
    ),
    "UpdateSession": obj(
        {"session_id": uuid, "title": string(minLength=1, maxLength=200)},
        ["session_id", "title"],
    ),
    "RunCreate": obj(
        {
            "input": string(minLength=1, maxLength=100000),
            "agent_ref": string(default="general@1"),
            "skills": {**array(ref("SkillBinding")), "maxItems": 30, "default": []},
            "skill_paths": nullable(
                {**array(string(minLength=1)), "maxItems": 30, "default": None}
            ),
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
            "user_binding": nullable(ref("UserBinding")),
            "effective_skill_paths": array(string()),
            "skill_path_source": string(enum=["LEGACY", "USER_DEFAULT", "EXPLICIT"]),
            "path_digest": string(),
        },
        [
            "agent_digest",
            "runtime_ref",
            "model_profile_ref",
            "execution_profile_ref",
            "skills",
            "tool_refs",
            "user_binding",
            "effective_skill_paths",
            "skill_path_source",
            "path_digest",
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
            "code": string(),
            "message": string(),
            "retryable": {"type": "boolean"},
            "request_id": string(),
        },
        ["code", "message", "retryable", "request_id"],
    ),
    "ErrorResponse": obj(
        {
            "code": ref("BusinessCode"),
            "message": string(),
            "data": obj({}, []),
        },
        ["code", "message", "data"],
    ),
    "Message": obj(
        {
            "message_id": uuid,
            "session_id": uuid,
            "message_seq": {"type": "integer", "minimum": 1},
            "input": string(),
            "status": ref("MessageStatus"),
            "task_outcome": nullable(ref("TaskOutcome")),
            "output": nullable(string()),
            "error": nullable(ref("Error")),
            "date_created": {**date, "readOnly": True},
            "date_updated": {**date, "readOnly": True},
        },
        [
            "message_id",
            "session_id",
            "message_seq",
            "input",
            "status",
            "task_outcome",
            "output",
            "error",
            "date_created",
            "date_updated",
        ],
    ),
    "Event": obj(
        {
            "event_id": uuid,
            "session_id": uuid,
            "message_id": uuid,
            "seq": {"type": "integer", "minimum": 1},
            "type": ref("PlatformEventType"),
            "occurred_at": date,
            "data": {"type": "object", "additionalProperties": True},
        },
        ["event_id", "session_id", "message_id", "seq", "type", "occurred_at", "data"],
    ),
    "PendingReply": obj(
        {
            "pending_id": uuid,
            "message_id": uuid,
            "kind": string(enum=["clarification", "execution_reconciliation", "approval"]),
            "prompt": string(),
        },
        ["pending_id", "message_id", "kind", "prompt"],
    ),
    "ReplyCreate": obj(
        {
            "message_id": uuid,
            "pending_id": uuid,
            "text": string(minLength=1, maxLength=100000),
        },
        ["message_id", "pending_id", "text"],
    ),
    "CancelMessage": obj(
        {"message_id": uuid},
        ["message_id"],
    ),
    "SessionState": obj(
        {
            "session": ref("Session"),
            "current_message": nullable(
                obj(
                    {"message_id": uuid, "status": ref("MessageStatus")},
                    ["message_id", "status"],
                )
            ),
            "pending_reply": nullable(ref("PendingReply")),
            "event_cursor": string(),
        },
        ["session", "current_message", "pending_reply", "event_cursor"],
    ),
    "RunSnapshotView": obj(
        {
            "run": ref("Run"),
            "event_cursor": string(),
            "pending_responses": array(ref("PendingReply")),
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
            "isolation_mode": string(enum=["trusted_logical"]),
        },
        [
            "status",
            "api_version",
            "model_configured",
            "execution_backend",
            "permission_mode",
            "isolation_mode",
        ],
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
for resource in ("Run",):
    schemas[resource]["properties"].update(audit_fields)
    schemas[resource]["required"] = list(
        dict.fromkeys([*schemas[resource]["required"], *audit_fields])
    )

# Each event has a closed payload schema; Platform must not infer fields from names.
event_payloads = {
    "message.accepted": obj(
        {
            "message_id": uuid,
            "status": string(enum=["QUEUED"]),
        },
        ["message_id", "status"],
    ),
    "message.started": obj(
        {"message_id": uuid, "status": string(enum=["RUNNING"])},
        ["message_id", "status"],
    ),
    "message.waiting": obj(
        {
            "message_id": uuid,
            "status": string(enum=["WAITING_USER", "WAITING_EXTERNAL"]),
            "pending_reply": nullable(ref("PendingReply")),
        },
        ["message_id", "status", "pending_reply"],
    ),
    "message.resumed": obj(
        {"message_id": uuid, "status": string(enum=["QUEUED"])},
        ["message_id", "status"],
    ),
    "message.recovering": obj(
        {"message_id": uuid, "reason": string()},
        ["message_id", "reason"],
    ),
    "message.cancel_requested": obj(
        {
            "message_id": uuid,
            "status": string(enum=["CANCELLING"]),
        },
        ["message_id", "status"],
    ),
    "message.finished": obj(
        {
            "message_id": uuid,
            "status": ref("MessageStatus"),
            "task_outcome": nullable(ref("TaskOutcome")),
            "output": nullable(string()),
            "error": nullable(ref("Error")),
        },
        ["message_id", "status", "task_outcome", "output", "error"],
    ),
    "message.delta": obj({"text": string()}, ["text"]),
    "message.completed": obj({"text": string()}, ["text"]),
    "tool.prepared": obj(
        {
            "operation_id": uuid,
            "tool_ref": string(),
            "input_summary": string(),
            "input": string(),
        },
        ["operation_id", "tool_ref", "input_summary", "input"],
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
            "error": nullable(ref("Error")),
        },
        ["operation_id", "status", "error"],
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

schemas["SessionPage"] = obj(
    {"items": array(ref("Session")), "next_cursor": nullable(string())},
    ["items", "next_cursor"],
)
schemas["MessagePage"] = obj(
    {"items": array(ref("Message")), "next_cursor": nullable(string())},
    ["items", "next_cursor"],
)
schemas["RunPage"] = obj(
    {"items": array(ref("Run")), "next_cursor": nullable(string())},
    ["items", "next_cursor"],
)
schemas["SkillPage"] = obj(
    {"items": array(ref("SkillInfo")), "next_cursor": nullable(string())},
    ["items", "next_cursor"],
)
schemas["FilePage"] = obj(
    {"items": array(ref("FileInfo")), "next_cursor": nullable(string())},
    ["items", "next_cursor"],
)
schemas["EventPage"] = obj(
    {"items": array(ref("Event")), "next_after_seq": nullable(integer)}, ["items", "next_after_seq"]
)


def envelope(data):
    return obj(
        {
            "code": string(enum=["0000"]),
            "message": string(enum=["success"]),
            "data": data,
        },
        ["code", "message", "data"],
    )


for name, data in [
    ("HealthEnvelope", ref("Health")),
    ("SessionEnvelope", ref("Session")),
    ("SessionPageEnvelope", ref("SessionPage")),
    ("MessageEnvelope", ref("Message")),
    ("MessagePageEnvelope", ref("MessagePage")),
    ("SessionStateEnvelope", ref("SessionState")),
    ("EventEnvelope", ref("Event")),
    ("EventPageEnvelope", ref("EventPage")),
    ("FilePageEnvelope", ref("FilePage")),
    ("FileContentEnvelope", ref("FileContent")),
]:
    schemas[name] = envelope(data)

paths = {}
errors = {
    str(n): {
        "description": label,
        "content": {"application/json": {"schema": ref("ErrorResponse")}},
    }
    for n, label in [
        (400, "Invalid request"),
        (404, "Not found"),
        (409, "Conflict"),
        (422, "Validation error"),
        (503, "Dependency unavailable"),
    ]
}


def add(
    path,
    method,
    op,
    response,
    body=None,
    status=200,
    query=(),
    required_query=(),
    sse=False,
):
    params = [
        {
            "name": name,
            "in": "query",
            "required": name in required_query,
            "schema": schema,
        }
        for name, schema in query
    ]
    entry = {
        "operationId": op,
        "parameters": params,
        "responses": {
            str(status): {
                "description": "Success",
                "content": {
                    "text/event-stream" if sse else "application/json": {
                        "schema": ref(response)
                        if not sse
                        else {"type": "string", "description": "Wrapped SSE Event JSON."}
                    }
                },
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
event_paging = (
    ("after_seq", integer),
    ("limit", {"type": "integer", "minimum": 0, "maximum": 1000, "default": 200}),
)
add("/health", "get", "health", "HealthEnvelope")
add("/v1/create-session", "post", "createSession", "SessionEnvelope", "SessionCreate", 201)
add(
    "/v1/update-session",
    "post",
    "updateSession",
    "SessionEnvelope",
    "UpdateSession",
)
add(
    "/v1/get-session",
    "get",
    "getSession",
    "SessionEnvelope",
    query=(("session_id", uuid),),
    required_query=("session_id",),
)
add(
    "/v1/list-sessions",
    "get",
    "listSessions",
    "SessionPageEnvelope",
    query=(
        ("user_rel_path", string()),
        ("project_ref", string()),
        *paging,
    ),
)
add(
    "/v1/send-message",
    "post",
    "sendMessage",
    "EventEnvelope",
    "SendMessageCreate",
    sse=True,
)
add(
    "/v1/cancel-message",
    "post",
    "cancelMessage",
    "MessageEnvelope",
    "CancelMessage",
)
add(
    "/v1/get-session-state",
    "get",
    "getSessionState",
    "SessionStateEnvelope",
    query=(("session_id", uuid),),
    required_query=("session_id",),
)
add(
    "/v1/get-message",
    "get",
    "getMessage",
    "MessageEnvelope",
    query=(("message_id", uuid),),
    required_query=("message_id",),
)
add(
    "/v1/list-session-messages",
    "get",
    "listSessionMessages",
    "MessagePageEnvelope",
    query=(("session_id", uuid), *paging),
    required_query=("session_id",),
)
add(
    "/v1/reply",
    "post",
    "reply",
    "EventEnvelope",
    "ReplyCreate",
    sse=True,
)
add(
    "/v1/stream-session-events",
    "get",
    "streamSessionEvents",
    "EventEnvelope",
    query=(("session_id", uuid), ("after_seq", integer)),
    required_query=("session_id",),
    sse=True,
)
add(
    "/v1/list-session-events",
    "get",
    "listSessionEvents",
    "EventPageEnvelope",
    query=(("session_id", uuid), *event_paging),
    required_query=("session_id",),
)
add(
    "/v1/list-session-files",
    "get",
    "listSessionFiles",
    "FilePageEnvelope",
    query=(("session_id", uuid),),
    required_query=("session_id",),
)
add(
    "/v1/read-session-file",
    "get",
    "readSessionFile",
    "FileContentEnvelope",
    query=(
        ("session_id", uuid),
        ("path", string(minLength=1, maxLength=1000)),
    ),
    required_query=("session_id", "path"),
)

spec = {
    "openapi": "3.1.0",
    "info": {
        "title": "Code Forge Runtime Integration Contract",
        "version": "0.4.0",
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
