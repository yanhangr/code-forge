"""Implementation boundaries used by the Runtime composition root."""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol
from typing import Any

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


class RunRepository(Protocol):
    async def find_request(
        self, scope_id: str, session_id: str, key: str
    ) -> ExistingRequest | None: ...
    async def require_session(self, scope_id: str, session_id: str) -> None: ...
    async def accept_once(
        self, request: RunRequest, key: str, fingerprint: str, snapshot: RunSnapshot
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

    def list_sessions(
        self, scope_id: str, cursor: str | None, limit: int
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    def require_session(self, scope_id: str, session_id: str) -> None: ...

    def find_request(
        self, scope_id: str, session_id: str, key: str
    ) -> ExistingRequest | None: ...

    def accept_once(
        self, request: RunRequest, key: str, fingerprint: str, snapshot: RunSnapshot
    ) -> AcceptedRun: ...

    def get_run(self, scope_id: str, run_id: str) -> dict[str, Any] | None: ...

    def list_runs(
        self, scope_id: str, session_id: str, cursor: str | None, limit: int
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    def claim_next_run(
        self,
        scope_id: str,
        worker_id: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> dict[str, Any] | None: ...

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


class ExecutionBackend(Protocol):
    async def capabilities(self) -> frozenset[str]: ...
    async def submit(self, spec: OperationSpec) -> str:
        """Return stable operation_id; same ID/different parameters must conflict."""
        ...

    async def get_status(self, operation_id: str) -> OperationResult: ...
    async def cancel(self, operation_id: str) -> OperationResult: ...
    async def release(self, operation_id: str) -> None: ...


class WorkspacePort(Protocol):
    def ensure_workspace(self, workspace_id: str) -> Any: ...

    def attempt_dir(self, workspace_id: str, attempt_id: str) -> Any: ...

    def prepare_attempt(self, workspace_id: str, attempt_id: str) -> Any: ...

    def commit_attempt(
        self, workspace_id: str, attempt_id: str, revision_id: str
    ) -> tuple[str, list[str]]: ...

    def write_text(
        self, workspace_id: str, attempt_id: str, relative_path: str, content: str
    ) -> Any: ...

    def read_text(
        self, workspace_id: str, relative_path: str, limit: int = 1024 * 1024
    ) -> tuple[str, str, bool]: ...

    def list_files(self, workspace_id: str) -> list[dict[str, object]]: ...


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
