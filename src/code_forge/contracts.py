"""Normative domain vocabulary. Wire schemas live in docs/api/openapi.json.

No framework, database, Platform, or model-provider dependencies belong here.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
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


class MessageStatus(StrEnum):
    """Platform-facing lifecycle; internal Run cancellation is not public in v1."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_USER = "WAITING_USER"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    RECOVERING = "RECOVERING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


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
    SKILL_STARTED = "skill.started"
    SKILL_FINISHED = "skill.finished"
    WORKSPACE_COMMITTED = "workspace.committed"


class PlatformEventType(StrEnum):
    """Closed public SSE event vocabulary. Internal run.* names are not exported."""

    MESSAGE_ACCEPTED = "message.accepted"
    MESSAGE_STARTED = "message.started"
    MESSAGE_WAITING = "message.waiting"
    MESSAGE_RESUMED = "message.resumed"
    MESSAGE_RECOVERING = "message.recovering"
    MESSAGE_FINISHED = "message.finished"
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETED = "message.completed"
    TOOL_PREPARED = "tool.prepared"
    TOOL_STARTED = "tool.started"
    TOOL_OUTPUT = "tool.output"
    TOOL_FINISHED = "tool.finished"
    TOOL_UNKNOWN = "tool.unknown"
    SKILL_ACTIVATED = "skill.activated"
    SKILL_STARTED = "skill.started"
    SKILL_FINISHED = "skill.finished"
    WORKSPACE_COMMITTED = "workspace.committed"


class BusinessCode(StrEnum):
    """Stable HTTP response code shared by Platform-facing endpoints."""

    OK = "0000"
    INVALID_REQUEST = "1001"
    SESSION_NOT_FOUND = "1002"
    MESSAGE_NOT_FOUND = "1003"
    STATE_CONFLICT = "1004"
    MESSAGE_BUSY = "1005"
    SKILL_INVALID = "2001"
    EXECUTION_FAILED = "3001"
    EXECUTION_UNKNOWN = "3002"
    MODEL_UNAVAILABLE = "4001"
    DEPENDENCY_UNAVAILABLE = "5001"
    INTERNAL_ERROR = "9001"


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
    MESSAGE_BUSY = "MESSAGE_BUSY"


class DomainError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _canonical_path(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainError(ErrorCode.INVALID_REQUEST, f"{field} must not be empty")
    if "\x00" in value:
        raise DomainError(ErrorCode.INVALID_REQUEST, f"{field} contains an invalid character")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise DomainError(ErrorCode.INVALID_REQUEST, f"{field} must be an absolute path")
    try:
        return path.resolve(strict=False).as_posix()
    except OSError as exc:
        raise DomainError(ErrorCode.INVALID_REQUEST, f"{field} cannot be resolved") from exc


def _canonical_relative_path(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainError(ErrorCode.INVALID_REQUEST, f"{field} must not be empty")
    if "\x00" in value:
        raise DomainError(ErrorCode.INVALID_REQUEST, f"{field} contains an invalid character")
    path = Path(value)
    if path.is_absolute():
        raise DomainError(ErrorCode.INVALID_REQUEST, f"{field} must be a relative path")
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if not parts or any(part == ".." for part in parts):
        raise DomainError(ErrorCode.INVALID_REQUEST, f"{field} escapes its logical root")
    return Path(*parts).as_posix()


def canonical_skill_paths(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise DomainError(ErrorCode.INVALID_REQUEST, "skill_paths must be an array")
    canonical: list[str] = []
    for index, value in enumerate(values):
        path = _canonical_path(value, f"skill_paths[{index}]")
        if path not in canonical:
            canonical.append(path)
    return tuple(canonical)


@dataclass(frozen=True)
class UserBinding:
    """Trusted Platform-owned user storage and project execution boundary."""

    scope_id: str
    tenant_ref: str
    user_ref: str
    user_path: str
    project_ref: str
    project_path: str | None = None
    storage_root: str = ""
    user_rel_path: str = ""
    project_rel_path: str = ""

    def __post_init__(self) -> None:
        for field in ("scope_id", "tenant_ref", "user_ref", "project_ref"):
            value = getattr(self, field)
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 200
                or not re.fullmatch(r"[A-Za-z0-9._-]+", value)
            ):
                raise DomainError(
                    ErrorCode.INVALID_REQUEST,
                    f"{field} must be a stable 1-200 character reference",
                )
        if self.scope_id != self.user_ref:
            raise DomainError(
                ErrorCode.INVALID_REQUEST,
                "scope_id must equal user_ref for a user execution scope",
            )
        user_path = _canonical_path(self.user_path, "user_path")
        storage_root = ""
        user_rel_path = ""
        if self.storage_root:
            storage_root = _canonical_path(self.storage_root, "storage_root")
            user_rel_path = _canonical_relative_path(self.user_rel_path, "user_rel_path")
            expected_user_path = (
                (Path(storage_root) / user_rel_path).resolve(strict=False).as_posix()
            )
            if user_path != expected_user_path:
                raise DomainError(
                    ErrorCode.INVALID_REQUEST,
                    "user_path must equal storage_root/user_rel_path",
                )
        elif self.user_rel_path:
            raise DomainError(
                ErrorCode.INVALID_REQUEST,
                "storage_root is required when user_rel_path is provided",
            )
        if self.project_path is None:
            project_path = (
                Path(user_path) / "workspace" / "projects" / self.project_ref
            ).as_posix()
        else:
            project_path = _canonical_path(self.project_path, "project_path")
        try:
            Path(project_path).relative_to(Path(user_path))
        except ValueError as exc:
            raise DomainError(
                ErrorCode.INVALID_REQUEST,
                "project_path must be located below user_path",
            ) from exc
        if project_path == user_path:
            raise DomainError(
                ErrorCode.INVALID_REQUEST,
                "project_path must not equal user_path",
            )
        project_rel_path = ""
        if self.project_rel_path:
            project_rel_path = _canonical_relative_path(
                self.project_rel_path,
                "project_rel_path",
            )
            expected_project_path = (
                (Path(user_path) / project_rel_path).resolve(strict=False).as_posix()
            )
            if project_path != expected_project_path:
                raise DomainError(
                    ErrorCode.INVALID_REQUEST,
                    "project_path must equal user_path/project_rel_path",
                )
        else:
            project_rel_path = Path(project_path).relative_to(Path(user_path)).as_posix()
        object.__setattr__(self, "user_path", user_path)
        object.__setattr__(self, "project_path", project_path)
        object.__setattr__(self, "storage_root", storage_root)
        object.__setattr__(self, "user_rel_path", user_rel_path)
        object.__setattr__(self, "project_rel_path", project_rel_path)

    @property
    def default_skill_path(self) -> str:
        return (Path(self.user_path) / "config" / "skills").as_posix()


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
    user_binding: UserBinding | None = None

    def __post_init__(self) -> None:
        if self.user_binding is not None and self.user_binding.scope_id != self.scope_id:
            raise DomainError(
                ErrorCode.INVALID_REQUEST,
                "context.scope_id must match user_binding.scope_id",
            )


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
    source_kind: str = "LEGACY"
    source_path: str = ""
    user_ref: str = ""
    project_ref: str = ""


@dataclass(frozen=True)
class RunRequest:
    session_id: str
    input: str
    agent_ref: str = "general@1"
    skills: tuple[SkillBinding, ...] = ()
    context: ExecutionContext = ExecutionContext()
    skill_paths: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.skill_paths is None:
            return
        object.__setattr__(self, "skill_paths", canonical_skill_paths(self.skill_paths))


@dataclass(frozen=True)
class RunSnapshot:
    agent_digest: str
    runtime_ref: str
    model_profile_ref: str
    execution_profile_ref: str
    skills: tuple[SkillRef, ...]
    tool_refs: tuple[str, ...]
    user_binding: UserBinding | None = None
    effective_skill_paths: tuple[str, ...] = ()
    skill_path_source: str = "LEGACY"
    path_digest: str = ""
    # Snapshot references are immutable. Never place credentials in this object.


class MountGranularity(StrEnum):
    PROJECT = "PROJECT"
    REVISION = "REVISION"


@dataclass(frozen=True)
class MountSpec:
    spec_version: str
    granularity: MountGranularity
    source_ref: str
    target: str
    mode: str
    cwd: str


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
    user_binding: UserBinding | None = None
    workspace_epoch: int = 0
    mount_spec: MountSpec | None = None
    working_directory: str | None = None


@dataclass(frozen=True)
class OperationResult:
    operation_id: str
    status: ToolStatus
    exit_code: int | None
    stdout_ref: str | None
    stderr_ref: str | None
    workspace_revision: str | None
    error_code: ErrorCode | None = None
    manifest_digest: str | None = None
    manifest_ref: str | None = None
    storage_ref: str | None = None
    changed_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkspaceCommit:
    revision_id: str
    manifest_digest: str
    manifest_ref: str
    storage_ref: str
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class PublicMessageError:
    code: str
    message: str
    retryable: bool
    request_id: str


@dataclass(frozen=True)
class PublicMessage:
    message_id: str
    session_id: str
    message_seq: int
    input: str
    status: MessageStatus
    task_outcome: str | None
    output: str | None
    error: PublicMessageError | None
    date_created: str
    date_updated: str


@dataclass(frozen=True)
class PlatformSession:
    session_id: str
    title: str
    user_rel_path: str
    project_ref: str | None
    project_rel_path: str
    date_created: str
    date_updated: str


@dataclass(frozen=True)
class PendingReply:
    pending_id: str
    message_id: str
    kind: str
    prompt: str


@dataclass(frozen=True)
class Event:
    event_id: str
    run_id: str
    seq: int
    type: EventType
    occurred_at: str
    data: dict[str, Any]
    schema_version: str = "1"
