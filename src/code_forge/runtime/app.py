"""Composition root for the local Agent Runtime."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from code_forge.contracts import (
    ExecutionContext,
    RunRequest,
    SkillBinding,
)
from code_forge.ports import (
    AgentHarnessPort,
    AuthorizationPort,
    ExecutionBackend,
    RunRepository,
    RuntimeStorePort,
    SnapshotResolver,
    WorkspacePort,
)
from code_forge.service import RunService


class AgentRuntime:
    def __init__(
        self,
        store: RuntimeStorePort,
        resolver: SnapshotResolver,
        workspace: WorkspacePort,
        execution: ExecutionBackend,
        harness: AgentHarnessPort,
        repository: RunRepository,
        authorization: AuthorizationPort,
        worker_id: str = "worker-1",
        lease_seconds: int = 120,
    ):
        self.store = store
        self.resolver = resolver
        self.workspace = workspace
        self.execution = execution
        self.harness = harness
        self.repository = repository
        self.service = RunService(repository, resolver, authorization)
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start_worker(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._worker_loop()),
            name="agent-runtime-worker",
            daemon=True,
        )
        self._thread.start()

    def stop_worker(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    async def _worker_loop(self) -> None:
        while not self._stop.is_set():
            claimed = self.store.claim_next_run(
                None,
                self.worker_id,
                self.lease_seconds,
                datetime.now(timezone.utc),
            )
            if claimed:
                await self.harness.execute(claimed)
            else:
                await asyncio.sleep(0.1)

    def create_session(
        self,
        *,
        scope_id: str,
        idempotency_key: str,
        title: str,
        external_ref: str | None,
        context: ExecutionContext,
    ) -> dict[str, Any]:
        session = self.store.create_session(
            scope_id,
            idempotency_key,
            title,
            external_ref,
            context,
        )
        assert session is not None
        self.workspace.ensure_workspace(
            session["workspace_id"],
            context.user_binding,
        )
        return session

    def submit_run(
        self,
        *,
        session_id: str,
        input_text: str,
        agent_ref: str,
        skills: tuple[SkillBinding, ...],
        skill_paths: tuple[str, ...] | None,
        idempotency_key: str,
        context: ExecutionContext,
        reject_if_active: bool = False,
    ) -> dict[str, Any]:
        request = RunRequest(
            session_id=session_id,
            input=input_text,
            agent_ref=agent_ref,
            skills=skills,
            context=context,
            skill_paths=skill_paths,
        )
        accepted = asyncio.run(
            self.service.submit(
                request,
                idempotency_key,
                reject_if_active=reject_if_active,
            )
        )
        return asdict(accepted)

    def cancel_run(self, scope_id: str, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(scope_id, run_id)
        if not run:
            from code_forge.contracts import DomainError, ErrorCode

            raise DomainError(ErrorCode.RUN_NOT_FOUND, "Run not found")
        return self.store.cancel_run(scope_id, run_id)

    def get_run(self, scope_id: str, run_id: str) -> dict[str, Any] | None:
        return self.store.get_run(scope_id, run_id)

    def respond_to_run(
        self,
        *,
        scope_id: str,
        run_id: str,
        pending_id: str,
        response_key: str,
        expected_state_version: int,
        text: str,
    ) -> dict[str, Any]:
        return self.store.respond_to_run(
            scope_id,
            run_id,
            pending_id,
            response_key,
            expected_state_version,
            text,
            "system:runtime/api",
        )
