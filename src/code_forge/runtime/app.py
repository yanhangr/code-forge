"""Composition root for the local Agent Runtime."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from code_forge.contracts import (
    DomainError,
    ErrorCode,
    ExecutionContext,
    RunRequest,
    SkillBinding,
    UserBinding,
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
                session = self.store.get_session_unscoped(claimed["run"]["session_id"])
                if session:
                    self._ensure_session_storage(session)
                result = await self.harness.execute(claimed)
                self._append_transcript_result(result, session)
            else:
                await asyncio.sleep(0.1)

    def materialize_user_storage(self) -> None:
        for session in self.store.list_bound_sessions():
            binding = self._binding_from_session(session)
            if binding is None:
                continue
            self.workspace.ensure_user_layout(session["workspace_id"], binding)
            self.workspace.ensure_session_storage(session, binding)
            runs, _ = self.store.list_messages_public(session["id"], None, 10_000)
            for run in runs:
                self._append_transcript_user(run, binding)
                self._append_transcript_result(run, session)

    def _ensure_session_storage(self, session: dict[str, Any]) -> None:
        binding = self._binding_from_session(session)
        if binding is None:
            return
        self.workspace.ensure_user_layout(session["workspace_id"], binding)
        self.workspace.ensure_session_storage(session, binding)

    def sync_session_storage(self, session: dict[str, Any]) -> None:
        self._ensure_session_storage(session)

    @staticmethod
    def _binding_from_session(session: dict[str, Any]) -> UserBinding | None:
        if not session.get("user_path") or not session.get("project_path"):
            return None
        try:
            return UserBinding(
                scope_id=session["user_ref"],
                tenant_ref=session["tenant_ref"],
                user_ref=session["user_ref"],
                user_path=session["user_path"],
                project_ref=session["project_ref"],
                project_path=session["project_path"],
                storage_root=session.get("storage_root") or "",
                user_rel_path=session.get("user_rel_path") or "",
            )
        except DomainError:
            return None

    def _append_transcript_user(
        self,
        run: dict[str, Any],
        binding: UserBinding,
    ) -> None:
        self.workspace.append_transcript(
            binding,
            run["session_id"],
            {
                "record_id": f"{run['id']}:user",
                "session_id": run["session_id"],
                "run_id": run["id"],
                "message_seq": run["run_seq"],
                "role": "user",
                "content": run["input"],
                "created_at": run["date_created"],
            },
        )

    def _append_transcript_result(
        self,
        run: dict[str, Any],
        session: dict[str, Any] | None,
    ) -> None:
        if session is None:
            return
        binding = self._binding_from_session(session)
        if binding is None or run.get("output") is None:
            return
        self.workspace.append_transcript(
            binding,
            run["session_id"],
            {
                "record_id": f"{run['id']}:assistant",
                "session_id": run["session_id"],
                "run_id": run["id"],
                "message_seq": run["run_seq"],
                "role": "assistant",
                "content": run["output"],
                "status": run["status"],
                "task_outcome": run.get("task_outcome"),
                "created_at": run["date_updated"],
            },
        )

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
        self.workspace.ensure_workspace(session["workspace_id"], context.user_binding)
        self._ensure_session_storage(session)
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
        session = self.store.get_session_unscoped(session_id)
        if context.user_binding is not None and session is not None:
            run = self.store.get_run(context.scope_id, accepted.id)
            if run is not None:
                self._append_transcript_user(run, context.user_binding)
        return asdict(accepted)

    def cancel_run(
        self,
        scope_id: str,
        run_id: str,
        actor_ref: str = "system:runtime/api",
    ) -> dict[str, Any]:
        run = self.store.get_run(scope_id, run_id)
        if not run:
            from code_forge.contracts import DomainError, ErrorCode

            raise DomainError(ErrorCode.RUN_NOT_FOUND, "Run not found")
        return self.store.cancel_run(scope_id, run_id, actor_ref)

    def get_run(self, scope_id: str, run_id: str) -> dict[str, Any] | None:
        return self.store.get_run(scope_id, run_id)

    def get_tool_execution(
        self,
        session_id: str,
        operation_id: str,
        *,
        limit: int = 65536,
    ) -> dict[str, Any]:
        execution = self.store.get_tool_execution(operation_id)
        if execution is None:
            raise DomainError(ErrorCode.RUN_NOT_FOUND, "Tool execution not found")
        run = self.store.get_run(execution["scope_id"], execution["run_id"])
        if run is None or run["session_id"] != session_id:
            raise DomainError(ErrorCode.RUN_NOT_FOUND, "Tool execution not found")
        session = self.store.get_session_unscoped(session_id)
        binding = self._binding_from_session(session) if session else None
        directory = self._tool_operation_dir(execution, binding, session_id)
        input_text, input_truncated = self._read_bounded_text(directory, "input.txt", limit)
        stdout_text, stdout_truncated = self._read_bounded_text(directory, "stdout.log", limit)
        stderr_text, stderr_truncated = self._read_bounded_text(directory, "stderr.log", limit)
        return {
            "operation_id": execution["id"],
            "message_id": execution["run_id"],
            "tool_ref": execution["tool_ref"],
            "status": execution["status"],
            "input": input_text or None,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "truncated": input_truncated or stdout_truncated or stderr_truncated,
            "error": execution.get("error"),
        }

    @staticmethod
    def _tool_operation_dir(
        execution: dict[str, Any],
        binding: UserBinding | None,
        session_id: str,
    ) -> Path | None:
        result_ref = execution.get("result_ref")
        if result_ref:
            return Path(str(result_ref)).parent
        if binding is not None:
            return (
                Path(binding.user_path)
                / "tool-output"
                / session_id
                / str(execution["run_id"])
                / str(execution["id"])
            )
        return None

    @staticmethod
    def _read_bounded_text(
        directory: Path | None,
        name: str,
        limit: int,
    ) -> tuple[str, bool]:
        if directory is None:
            return "", False
        path = directory / name
        if not path.is_file():
            return "", False
        try:
            with path.open("rb") as handle:
                data = handle.read(limit + 1)
        except OSError:
            return "", False
        truncated = len(data) > limit
        return data[:limit].decode("utf-8", errors="replace"), truncated

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
