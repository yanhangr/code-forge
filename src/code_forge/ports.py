"""Implementation boundaries. Adapters are intentionally not implemented here."""

from typing import Protocol

from .contracts import (
    AcceptedRun,
    ExecutionContext,
    ExistingRequest,
    OperationResult,
    OperationSpec,
    RunRequest,
    RunSnapshot,
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


class ExecutionBackend(Protocol):
    async def capabilities(self) -> frozenset[str]: ...
    async def submit(self, spec: OperationSpec) -> str:
        """Return stable operation_id; same ID/different parameters must conflict."""
        ...

    async def get_status(self, operation_id: str) -> OperationResult: ...
    async def cancel(self, operation_id: str) -> OperationResult: ...
    async def release(self, operation_id: str) -> None: ...
