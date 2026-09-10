"""Normative domain vocabulary. Wire schemas live in docs/api/openapi.json.

No framework, database, Platform, or model-provider dependencies belong here.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_USER = "WAITING_USER"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    RECOVERING = "RECOVERING"
    CANCELLING = "CANCELLING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


TERMINAL_STATUSES = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.TIMED_OUT}
)


class TaskOutcome(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    BLOCKED = "blocked"


class ToolStatus(StrEnum):
    PREPARED = "PREPARED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class EventType(StrEnum):
    RUN_ACCEPTED = "run.accepted"
    RUN_STARTED = "run.started"
    RUN_WAITING = "run.waiting"
    RUN_RESUMED = "run.resumed"
    RUN_RECOVERING = "run.recovering"
    RUN_CANCEL_REQUESTED = "run.cancel_requested"
    RUN_FINISHED = "run.finished"
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETED = "message.completed"
    TOOL_PREPARED = "tool.prepared"
    TOOL_STARTED = "tool.started"
    TOOL_OUTPUT = "tool.output"
    TOOL_FINISHED = "tool.finished"
    TOOL_UNKNOWN = "tool.unknown"
    SKILL_ACTIVATED = "skill.activated"
    WORKSPACE_COMMITTED = "workspace.committed"


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    RUN_NOT_FOUND = "RUN_NOT_FOUND"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    STATE_CONFLICT = "STATE_CONFLICT"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    INVALID_EVENT_CURSOR = "INVALID_EVENT_CURSOR"
    SKILL_NOT_FOUND = "SKILL_NOT_FOUND"
    SKILL_INCOMPATIBLE = "SKILL_INCOMPATIBLE"
    MODEL_NOT_CONFIGURED = "MODEL_NOT_CONFIGURED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    EXECUTION_UNKNOWN = "EXECUTION_UNKNOWN"
    CAPABILITY_DENIED = "CAPABILITY_DENIED"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"


class DomainError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AuditFields:
    date_created: datetime
    created_by: str
    date_updated: datetime
    updated_by: str


@dataclass(frozen=True)
class ExecutionContext:
    # Default scope is for the explicitly trusted local verification deployment.
    # Future Platform authentication must derive this from a trusted principal.
    scope_id: str = "default"
    actor_ref: str | None = None
    external_ref: str | None = None


@dataclass(frozen=True)
class SkillBinding:
    name: str
    version: str | None = None


@dataclass(frozen=True)
class SkillRef:
    name: str
    version: str
    digest: str
    bundle_ref: str


@dataclass(frozen=True)
class RunRequest:
    session_id: str
    input: str
    agent_ref: str = "general@1"
    skills: tuple[SkillBinding, ...] = ()
    context: ExecutionContext = ExecutionContext()


@dataclass(frozen=True)
class RunSnapshot:
    agent_digest: str
    runtime_ref: str
    model_profile_ref: str
    execution_profile_ref: str
    skills: tuple[SkillRef, ...]
    tool_refs: tuple[str, ...]
    # Snapshot references are immutable. Never place credentials in this object.


@dataclass(frozen=True)
class AcceptedRun:
    id: str
    session_id: str
    status: RunStatus
    state_version: int
    reused: bool


@dataclass(frozen=True)
class ExistingRequest:
    fingerprint: str
    run: AcceptedRun


@dataclass(frozen=True)
class OperationSpec:
    operation_id: str
    run_id: str
    attempt_id: str
    workspace_ref: str
    argv: tuple[str, ...]
    timeout_seconds: int
    output_limit_bytes: int
    environment_profile_ref: str
    credential_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class OperationResult:
    operation_id: str
    status: ToolStatus
    exit_code: int | None
    stdout_ref: str | None
    stderr_ref: str | None
    workspace_revision: str | None
    error_code: ErrorCode | None = None


@dataclass(frozen=True)
class Event:
    event_id: str
    run_id: str
    seq: int
    type: EventType
    occurred_at: str
    data: dict[str, Any]
    schema_version: str = "1"
