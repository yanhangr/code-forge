"""Implementation boundaries used by the Runtime composition root."""

from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Any, Protocol

from .contracts import (
    AcceptedRun,
    ExecutionContext,
    ExistingRequest,
    OperationResult,
    OperationSpec,
    RunRequest,
    RunSnapshot,
    RunStatus,
    TaskOutcome,
    ToolStatus,
    UserBinding,
    WorkspaceCommit,
)


class AuthorizationPort(Protocol):
    async def check(self, context: ExecutionContext, action: str, resource_ref: str) -> None:
        """Return on allow; raise CAPABILITY_DENIED on deny. No business roles here."""
        ...


class DefaultAllowAuthorization:
    """Explicitly selected ONLY for the trusted, local main-flow verification stage."""

    async def check(self, context: ExecutionContext, action: str, resource_ref: str) -> None:
        return None


class SnapshotResolver(Protocol):
    async def resolve_and_store(self, request: RunRequest) -> RunSnapshot:
        """Read manual files once and persist immutable content before returning refs.

        Later, a Platform registry adapter may implement the same contract.
        """
        ...

    def list_skills(
        self,
        user_binding: UserBinding | None = None,
        skill_paths: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]: ...


class RunRepository(Protocol):
    async def find_request(
        self, scope_id: str, session_id: str, key: str
    ) -> ExistingRequest | None: ...
    async def require_session(self, scope_id: str, session_id: str) -> None: ...
    async def accept_once(
        self,
        request: RunRequest,
        key: str,
        fingerprint: str,
        snapshot: RunSnapshot,
        reject_if_active: bool = False,
    ) -> AcceptedRun:
        """One transaction: unique request key, Session seq, input, snapshot, Run, event.

        A concurrent identical key returns its original Run with reused=True;
        different fingerprint raises IDEMPOTENCY_CONFLICT. Re-check Session scope.
        Do not overwrite original config snapshots or advance seq on reuse.
        """
        ...


class RuntimeStorePort(Protocol):
    """Persistence surface shared by transport and Runtime coordinators."""

    def create_session(
        self,
        scope_id: str,
        idempotency_key: str,
        title: str,
        external_ref: str | None,
        context: ExecutionContext,
    ) -> dict[str, Any]: ...

    def get_session(self, scope_id: str, session_id: str) -> dict[str, Any] | None: ...

    def list_bound_sessions(self) -> list[dict[str, Any]]: ...

    def list_sessions(
        self, scope_id: str, cursor: str | None, limit: int
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    def require_session(self, scope_id: str, session_id: str) -> None: ...

    def get_workspace(self, scope_id: str, workspace_id: str) -> dict[str, Any] | None: ...

    def find_request(self, scope_id: str, session_id: str, key: str) -> ExistingRequest | None: ...

    def accept_once(
        self,
        request: RunRequest,
        key: str,
        fingerprint: str,
        snapshot: RunSnapshot,
        reject_if_active: bool = False,
    ) -> AcceptedRun: ...

    def get_run(self, scope_id: str, run_id: str) -> dict[str, Any] | None: ...

    def list_runs(
        self, scope_id: str, session_id: str, cursor: str | None, limit: int
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    def claim_next_run(
        self,
        scope_id: str | None,
        worker_id: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> dict[str, Any] | None: ...

    def record_workspace_revision(
        self,
        scope_id: str,
        workspace_id: str,
        attempt_id: str,
        workspace_epoch: int,
        commit: WorkspaceCommit,
        actor: str,
    ) -> None: ...

    def transition_run(
        self,
        scope_id: str,
        run_id: str,
        *,
        expected_version: int,
        target: RunStatus,
        task_outcome: TaskOutcome | None = None,
        reason: str | None = None,
        output: str | None = None,
        error: dict[str, Any] | None = None,
        extra_data: dict[str, Any] | None = None,
        actor_ref: str | None = None,
    ) -> dict[str, Any]: ...

    def cancel_run(
        self, scope_id: str, run_id: str, actor_ref: str | None = None
    ) -> dict[str, Any]: ...

    def append_event(
        self,
        scope_id: str,
        run_id: str,
        event_type: Any,
        data: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]: ...

    def list_events(
        self, scope_id: str, run_id: str, after_seq: int, limit: int
    ) -> tuple[list[dict[str, Any]], int | None]: ...

    def event_cursor(self, scope_id: str, run_id: str) -> str: ...

    def get_snapshot(self, scope_id: str, run_id: str) -> dict[str, Any] | None: ...

    def prepare_tool(
        self,
        scope_id: str,
        run_id: str,
        logical_call_key: str,
        tool_ref: str,
        params_digest: str,
        input_ref: str,
        execution_profile_ref: str,
        input_summary: str,
        actor: str,
        workspace_id: str | None = None,
        input_revision_id: str | None = None,
        input_text: str = "",
    ) -> tuple[str, ToolStatus]: ...

    def start_tool(self, scope_id: str, operation_id: str, actor: str) -> None: ...

    def append_tool_output(
        self,
        scope_id: str,
        operation_id: str,
        stream: str,
        text: str,
        truncated: bool,
        actor: str,
    ) -> None: ...

    def finish_tool(
        self,
        scope_id: str,
        operation_id: str,
        status: ToolStatus,
        result_ref: str | None,
        error: dict[str, Any] | None,
        actor: str,
        result_revision_id: str | None = None,
    ) -> None: ...

    def respond_to_run(
        self,
        scope_id: str,
        run_id: str,
        pending_id: str,
        response_key: str,
        expected_state_version: int,
        text: str,
        actor: str,
    ) -> dict[str, Any]: ...

    def conversation_history(
        self, scope_id: str, session_id: str, before_run_id: str, limit: int = 200
    ) -> list[dict[str, Any]]: ...


class ContentStorePort(Protocol):
    """Body storage boundary for the PostgreSQL + JSONL split.

    PostgreSQL keeps relational facts and content references; implementations
    keep the actual bodies (messages, event payloads, snapshots, pending
    responses) in JSONL logs and immutable object files. Records are written
    and flushed before the referencing database transaction commits, so a crash
    between the two leaves a reclaimable orphan rather than a dangling promise.
    """

    def append_record(
        self,
        base: Any,
        relative_dir: str,
        record: dict[str, Any],
        *,
        prefix: str = "content",
    ) -> Any:
        """Append one JSONL record and return its ref/offset/bytes/digest."""
        ...

    def read_record(self, base: Any, ref: str, offset: int, length: int) -> dict[str, Any]: ...

    def read_verified(
        self,
        base: Any,
        ref: str,
        offset: int,
        length: int,
        expected_digest: str | None,
    ) -> dict[str, Any]: ...

    def write_object(self, base: Any, relative_path: str, payload: bytes) -> Any:
        """Write one immutable object file; the same path is idempotent."""
        ...

    def read_object(self, base: Any, ref: str) -> bytes: ...


OutputSink = Callable[[str, str, bool], None]


class ExecutionBackend(Protocol):
    async def capabilities(self) -> frozenset[str]: ...

    async def submit(
        self,
        spec: OperationSpec,
        *,
        on_output: OutputSink | None = None,
    ) -> str:
        """Return stable operation_id; same ID/different parameters must conflict.

        ``on_output`` is invoked incrementally with ``(stream, text, truncated)`` so the
        owning harness can persist progress without the backend depending on the store.
        """
        ...

    async def get_status(self, operation_id: str) -> OperationResult: ...
    async def cancel(self, operation_id: str) -> OperationResult: ...
    async def release(self, operation_id: str) -> None: ...


class WorkspacePort(Protocol):
    def ensure_workspace(
        self, workspace_id: str, user_binding: UserBinding | None = None
    ) -> Any: ...

    def attempt_dir(
        self,
        workspace_id: str,
        attempt_id: str,
        user_binding: UserBinding | None = None,
    ) -> Any: ...

    def prepare_attempt(
        self,
        workspace_id: str,
        attempt_id: str,
        user_binding: UserBinding | None = None,
    ) -> Any: ...

    def commit_attempt(
        self,
        workspace_id: str,
        attempt_id: str,
        revision_id: str,
        user_binding: UserBinding | None = None,
    ) -> WorkspaceCommit: ...

    def write_text(
        self,
        workspace_id: str,
        attempt_id: str,
        relative_path: str,
        content: str,
        user_binding: UserBinding | None = None,
    ) -> Any: ...

    def read_text(
        self,
        workspace_id: str,
        relative_path: str,
        limit: int = 1024 * 1024,
        user_binding: UserBinding | None = None,
    ) -> tuple[str, str, bool]: ...

    def list_files(
        self, workspace_id: str, user_binding: UserBinding | None = None
    ) -> list[dict[str, object]]: ...

    def ensure_user_layout(
        self,
        workspace_id: str,
        user_binding: UserBinding,
    ) -> Any: ...

    def ensure_session_storage(
        self,
        session: dict[str, Any],
        user_binding: UserBinding,
    ) -> Any: ...

    def append_transcript(
        self,
        user_binding: UserBinding,
        session_id: str,
        record: dict[str, Any],
    ) -> Any: ...


class ContextManagerPort(Protocol):
    def build(
        self,
        system_prompt: str,
        history: list[dict[str, str]],
        current_input: str,
    ) -> list[dict[str, str]]: ...


class ModelAdapterPort(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...

    def complete_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]: ...


class AgentHarnessPort(Protocol):
    async def execute(self, claimed: dict[str, Any]) -> dict[str, Any]: ...
