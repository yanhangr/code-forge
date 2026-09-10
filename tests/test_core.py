import asyncio
import unittest
from dataclasses import replace

from code_forge.contracts import (
    TERMINAL_STATUSES,
    AcceptedRun,
    DomainError,
    ErrorCode,
    EventType,
    ExistingRequest,
    RunRequest,
    RunSnapshot,
    RunStatus,
    TaskOutcome,
)
from code_forge.ports import DefaultAllowAuthorization
from code_forge.service import RunService, request_fingerprint
from code_forge.state_machine import ALLOWED_TRANSITIONS, RunState, request_cancel, transition


class StateTests(unittest.TestCase):
    def test_terminal_cannot_resume(self):
        for final in TERMINAL_STATUSES:
            for target in RunStatus:
                with self.assertRaises(DomainError):
                    transition(RunState("r", final), target, expected_version=0)

    def test_stale_writer_rejected(self):
        with self.assertRaises(DomainError) as error:
            transition(RunState("r", state_version=4), RunStatus.RUNNING, expected_version=3)
        self.assertEqual(error.exception.code, ErrorCode.STATE_CONFLICT)

    def test_wait_resume_and_completion(self):
        state = transition(RunState("r"), RunStatus.RUNNING, expected_version=0).after
        state = transition(
            state, RunStatus.WAITING_EXTERNAL, expected_version=1, reason="python"
        ).after
        state = transition(state, RunStatus.QUEUED, expected_version=2).after
        self.assertIsNone(state.wait_reason)
        state = transition(state, RunStatus.RUNNING, expected_version=3).after
        with self.assertRaises(DomainError):
            transition(state, RunStatus.SUCCEEDED, expected_version=4)
        result = transition(
            state, RunStatus.SUCCEEDED, expected_version=4, task_outcome=TaskOutcome.PARTIAL
        )
        self.assertEqual(result.after.task_outcome, TaskOutcome.PARTIAL)
        self.assertEqual(result.event_type, EventType.RUN_FINISHED)

    def test_cancel_idempotent_and_not_success(self):
        initial = RunState("r", RunStatus.RUNNING)
        cancelled = request_cancel(initial, expected_version=0)
        self.assertEqual(cancelled.after.status, RunStatus.CANCELLING)
        self.assertFalse(request_cancel(cancelled.after, expected_version=1).changed)
        with self.assertRaises(DomainError):
            transition(
                cancelled.after,
                RunStatus.SUCCEEDED,
                expected_version=1,
                task_outcome=TaskOutcome.COMPLETED,
            )
        final = transition(cancelled.after, RunStatus.CANCELLED, expected_version=1).after
        self.assertFalse(request_cancel(final, expected_version=2).changed)

    def test_unknown_execution_waits_for_reconciliation(self):
        state = RunState("r", RunStatus.RECOVERING)
        result = transition(
            state, RunStatus.WAITING_USER, expected_version=0, reason="execution_unknown"
        )
        self.assertEqual(result.after.wait_reason, "execution_unknown")

    def test_no_unregistered_status(self):
        self.assertEqual(set(ALLOWED_TRANSITIONS), set(RunStatus))


class FakeRepository:
    def __init__(self):
        self.record = None
        self.commits = 0
        self.lock = asyncio.Lock()

    async def require_session(self, scope_id, session_id):
        if session_id != "s":
            raise DomainError(ErrorCode.SESSION_NOT_FOUND, "missing")

    async def find_request(self, scope_id, session_id, key):
        return self.record

    async def accept_once(self, request, key, fingerprint, snapshot):
        async with self.lock:
            if self.record:
                if self.record.fingerprint != fingerprint:
                    raise DomainError(ErrorCode.IDEMPOTENCY_CONFLICT, "conflict")
                return replace(self.record.run, reused=True)
            self.commits += 1
            self.record = ExistingRequest(
                fingerprint, AcceptedRun("r", "s", RunStatus.QUEUED, 0, False)
            )
            return self.record.run


class FakeResolver:
    def __init__(self):
        self.calls = 0

    async def resolve_and_store(self, request):
        self.calls += 1
        await asyncio.sleep(0)
        return RunSnapshot("a", "rt@1", "model@1", "local@1", (), ())


class AcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = FakeRepository()
        self.resolver = FakeResolver()
        self.service = RunService(self.repo, self.resolver, DefaultAllowAuthorization())
        self.request = RunRequest("s", "calculate")

    async def test_retry_does_not_resolve_new_skill(self):
        original = await self.service.submit(self.request, "key")
        repeated = await self.service.submit(self.request, "key")
        self.assertEqual(original.id, repeated.id)
        self.assertTrue(repeated.reused)
        self.assertEqual(self.resolver.calls, 1)

    async def test_concurrent_duplicate_acceptance(self):
        runs = await asyncio.gather(*(self.service.submit(self.request, "key") for _ in range(8)))
        self.assertEqual(self.repo.commits, 1)
        self.assertEqual(sum(not r.reused for r in runs), 1)

    async def test_key_conflict_and_input_whitespace_preserved(self):
        await self.service.submit(self.request, "key")
        with self.assertRaises(DomainError) as error:
            await self.service.submit(replace(self.request, input="calculate "), "key")
        self.assertEqual(error.exception.code, ErrorCode.IDEMPOTENCY_CONFLICT)
        self.assertNotEqual(
            request_fingerprint(self.request),
            request_fingerprint(replace(self.request, input="calculate ")),
        )

    async def test_authorization_precedes_resolution(self):
        class Deny:
            async def check(self, *args):
                raise DomainError(ErrorCode.CAPABILITY_DENIED, "denied")

        service = RunService(self.repo, self.resolver, Deny())
        with self.assertRaises(DomainError):
            await service.submit(self.request, "key")
        self.assertEqual(self.resolver.calls, 0)
        self.assertEqual(self.repo.commits, 0)


if __name__ == "__main__":
    unittest.main()
