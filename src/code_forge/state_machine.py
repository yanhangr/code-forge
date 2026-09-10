"""Pure Run transitions. Persistence adapters must CAS state_version and epoch.

This module does not itself acquire leases, kill processes, or commit events.
"""

from dataclasses import dataclass, replace

from .contracts import TERMINAL_STATUSES, DomainError, ErrorCode, EventType, RunStatus, TaskOutcome

S = RunStatus
ALLOWED_TRANSITIONS = {
    S.QUEUED: frozenset({S.RUNNING, S.CANCELLING, S.FAILED, S.TIMED_OUT}),
    S.RUNNING: frozenset(
        {
            S.WAITING_USER,
            S.WAITING_EXTERNAL,
            S.RECOVERING,
            S.CANCELLING,
            S.SUCCEEDED,
            S.FAILED,
            S.TIMED_OUT,
        }
    ),
    S.WAITING_USER: frozenset({S.QUEUED, S.CANCELLING, S.FAILED, S.TIMED_OUT}),
    S.WAITING_EXTERNAL: frozenset({S.QUEUED, S.CANCELLING, S.FAILED, S.TIMED_OUT}),
    S.RECOVERING: frozenset({S.QUEUED, S.WAITING_USER, S.CANCELLING, S.FAILED, S.TIMED_OUT}),
    S.CANCELLING: frozenset({S.CANCELLED}),
    S.SUCCEEDED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
    S.TIMED_OUT: frozenset(),
}


@dataclass(frozen=True)
class RunState:
    run_id: str
    status: RunStatus = S.QUEUED
    state_version: int = 0
    task_outcome: TaskOutcome | None = None
    wait_reason: str | None = None


@dataclass(frozen=True)
class Transition:
    before: RunState
    after: RunState
    event_type: EventType
    changed: bool = True


def transition(
    state: RunState,
    target: RunStatus,
    *,
    expected_version: int,
    task_outcome: TaskOutcome | None = None,
    reason: str | None = None,
) -> Transition:
    if state.state_version != expected_version:
        raise DomainError(ErrorCode.STATE_CONFLICT, "Run state_version changed")
    if target not in ALLOWED_TRANSITIONS[state.status]:
        raise DomainError(ErrorCode.INVALID_TRANSITION, f"{state.status} -> {target} is forbidden")
    if target == S.SUCCEEDED and task_outcome is None:
        raise DomainError(
            ErrorCode.INVALID_REQUEST, "Normal completion requires an explicit task_outcome"
        )
    if target != S.SUCCEEDED and task_outcome is not None:
        raise DomainError(
            ErrorCode.INVALID_REQUEST, "task_outcome belongs to normal completion only"
        )
    waiting = target in {S.WAITING_USER, S.WAITING_EXTERNAL}
    if waiting and not reason:
        raise DomainError(ErrorCode.INVALID_REQUEST, "Waiting requires a reason")
    if target in TERMINAL_STATUSES:
        event_type = EventType.RUN_FINISHED
    else:
        event_type = {
            S.QUEUED: EventType.RUN_RESUMED,
            S.RUNNING: EventType.RUN_STARTED,
            S.WAITING_USER: EventType.RUN_WAITING,
            S.WAITING_EXTERNAL: EventType.RUN_WAITING,
            S.RECOVERING: EventType.RUN_RECOVERING,
            S.CANCELLING: EventType.RUN_CANCEL_REQUESTED,
        }[target]
    after = replace(
        state,
        status=target,
        state_version=state.state_version + 1,
        task_outcome=task_outcome,
        wait_reason=reason if waiting else None,
    )
    return Transition(state, after, event_type)


def request_cancel(state: RunState, *, expected_version: int) -> Transition:
    if state.state_version != expected_version:
        raise DomainError(ErrorCode.STATE_CONFLICT, "Run state_version changed")
    if state.status in TERMINAL_STATUSES or state.status == S.CANCELLING:
        return Transition(state, state, EventType.RUN_CANCEL_REQUESTED, changed=False)
    return transition(state, S.CANCELLING, expected_version=expected_version)
