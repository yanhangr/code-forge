"""Local subprocess ExecutionBackend for trusted development verification."""

from __future__ import annotations

import asyncio
import os
import signal
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from code_forge.contracts import DomainError, ErrorCode, OperationResult, OperationSpec, ToolStatus
from code_forge.ports import ExecutionBackend
from code_forge.workspace.store import WorkspaceStore


def _clean_env() -> dict[str, str]:
    blocked = {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "DATABASE_URL",
    }
    return {key: value for key, value in os.environ.items() if key not in blocked}


@dataclass
class _RunningOperation:
    spec: OperationSpec
    process: asyncio.subprocess.Process
    status: ToolStatus = ToolStatus.RUNNING
    exit_code: int | None = None
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    workspace_revision: str | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    task: asyncio.Task[Any] | None = None
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)


class LocalProcessBackend(ExecutionBackend):
    """Runs argv as a child process group; not a security sandbox."""

    def __init__(self, workspace_store: WorkspaceStore, root: str | Path):
        self.workspace_store = workspace_store
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._operations: dict[str, _RunningOperation] = {}
        self._lock = asyncio.Lock()

    async def capabilities(self) -> frozenset[str]:
        return frozenset({"python", "command"})

    async def submit(self, spec: OperationSpec) -> str:
        async with self._lock:
            existing = self._operations.get(spec.operation_id)
            if existing:
                if existing.spec.argv != spec.argv:
                    raise DomainError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "Operation id is already bound to different argv",
                    )
                return spec.operation_id
            cwd = self.workspace_store.attempt_dir(spec.workspace_ref, spec.attempt_id)
            if not cwd.exists():
                self.workspace_store.prepare_attempt(spec.workspace_ref, spec.attempt_id)
            op_dir = self.root / spec.operation_id
            op_dir.mkdir(parents=True, exist_ok=True)
            stdout_path = op_dir / "stdout.log"
            stderr_path = op_dir / "stderr.log"
            process = await asyncio.create_subprocess_exec(
                *spec.argv,
                cwd=cwd,
                env=_clean_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            operation = _RunningOperation(
                spec=spec,
                process=process,
                stdout_ref=str(stdout_path),
                stderr_ref=str(stderr_path),
            )
            self._operations[spec.operation_id] = operation
            operation.task = asyncio.create_task(self._run_operation(operation))
            return spec.operation_id

    async def get_status(self, operation_id: str) -> OperationResult:
        async with self._lock:
            operation = self._operations.get(operation_id)
            if not operation:
                raise DomainError(ErrorCode.RUN_NOT_FOUND, "Operation not found")
            return self._result(operation)

    async def cancel(self, operation_id: str) -> OperationResult:
        async with self._lock:
            operation = self._operations.get(operation_id)
            if not operation:
                raise DomainError(ErrorCode.RUN_NOT_FOUND, "Operation not found")
            operation.cancelled.set()
            self._kill_process_group(operation.process)
        if operation.task:
            try:
                await asyncio.wait_for(operation.task, timeout=10)
            except asyncio.TimeoutError:
                pass
        return self._result(operation)

    async def release(self, operation_id: str) -> None:
        async with self._lock:
            self._operations.pop(operation_id, None)

    async def _run_operation(self, operation: _RunningOperation) -> None:
        stdout_path = Path(operation.stdout_ref or "")
        stderr_path = Path(operation.stderr_ref or "")
        stdout_task = asyncio.create_task(
            self._read_bounded(
                operation.process.stdout,
                stdout_path,
                operation.spec.output_limit_bytes,
            )
        )
        stderr_task = asyncio.create_task(
            self._read_bounded(
                operation.process.stderr,
                stderr_path,
                operation.spec.output_limit_bytes,
            )
        )
        wait_task = asyncio.create_task(operation.process.wait())
        try:
            done, pending = await asyncio.wait(
                {wait_task, stdout_task, stderr_task},
                timeout=operation.spec.timeout_seconds,
                return_when=asyncio.ALL_COMPLETED,
            )
            if wait_task not in done:
                self._kill_process_group(operation.process)
                await asyncio.wait_for(wait_task, timeout=10)
                operation.status = ToolStatus.FAILED
                operation.error_code = ErrorCode.EXECUTION_FAILED
                operation.error_message = "Operation timed out"
            elif operation.cancelled.is_set():
                operation.status = ToolStatus.CANCELLED
            else:
                operation.exit_code = await wait_task
                operation.status = ToolStatus.SUCCEEDED if operation.exit_code == 0 else ToolStatus.FAILED
                if operation.exit_code != 0:
                    operation.error_code = ErrorCode.EXECUTION_FAILED
                    operation.error_message = f"Process exited with {operation.exit_code}"
        except Exception as exc:
            self._kill_process_group(operation.process)
            operation.status = ToolStatus.FAILED
            operation.error_code = ErrorCode.EXECUTION_FAILED
            operation.error_message = str(exc) or exc.__class__.__name__
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        operation.stdout_truncated = stdout_task.result() if not stdout_task.cancelled() else True
        operation.stderr_truncated = stderr_task.result() if not stderr_task.cancelled() else True
        if operation.status == ToolStatus.SUCCEEDED:
            try:
                revision_id = str(uuid.uuid4())
                _, _ = self.workspace_store.commit_attempt(
                    operation.spec.workspace_ref,
                    operation.spec.attempt_id,
                    revision_id,
                )
                operation.workspace_revision = revision_id
            except Exception as exc:
                operation.status = ToolStatus.FAILED
                operation.error_code = ErrorCode.EXECUTION_FAILED
                operation.error_message = f"Workspace commit failed: {exc}"

    @staticmethod
    async def _read_bounded(
        stream: asyncio.StreamReader | None,
        path: Path,
        limit: int,
    ) -> bool:
        if stream is None:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        truncated = False
        total = 0
        with path.open("wb") as handle:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                remaining = limit - total
                if remaining <= 0:
                    truncated = True
                    continue
                write_size = min(len(chunk), remaining)
                handle.write(chunk[:write_size])
                total += write_size
                if write_size < len(chunk):
                    truncated = True
        return truncated

    @staticmethod
    def _kill_process_group(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            asyncio.create_task(_kill_after_timeout(process))
        except RuntimeError:
            pass

    @staticmethod
    def _result(operation: _RunningOperation) -> OperationResult:
        return OperationResult(
            operation_id=operation.spec.operation_id,
            status=operation.status,
            exit_code=operation.exit_code,
            stdout_ref=operation.stdout_ref,
            stderr_ref=operation.stderr_ref,
            workspace_revision=operation.workspace_revision,
            error_code=operation.error_code,
        )


async def _kill_after_timeout(process: asyncio.subprocess.Process) -> None:
    await asyncio.sleep(5)
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
