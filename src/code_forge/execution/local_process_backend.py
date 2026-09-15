"""Local subprocess ExecutionBackend for trusted development verification."""

from __future__ import annotations

import asyncio
import codecs
import os
import signal
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from code_forge.contracts import (
    DomainError,
    ErrorCode,
    MountGranularity,
    OperationResult,
    OperationSpec,
    ToolStatus,
    WorkspaceCommit,
)
from code_forge.ports import ExecutionBackend, OutputSink
from code_forge.workspace.store import WorkspaceStore

_SAFE_ENV_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "TERM",
    "TZ",
    "PYTHONIOENCODING",
    "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE",
)


def _clean_env() -> dict[str, str]:
    """Minimal environment for tool subprocesses.

    Runtime service configuration (FORGE_*, model credentials, ...) and unrelated
    secrets must never reach model-authored code. Keep only interpreter basics.
    """

    return {key: os.environ[key] for key in _SAFE_ENV_KEYS if key in os.environ}


@dataclass
class _RunningOperation:
    spec: OperationSpec
    process: asyncio.subprocess.Process
    status: ToolStatus = ToolStatus.RUNNING
    exit_code: int | None = None
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    workspace_revision: str | None = None
    workspace_commit: WorkspaceCommit | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    task: asyncio.Task[Any] | None = None
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    output_sink: OutputSink | None = None


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

    async def submit(
        self,
        spec: OperationSpec,
        *,
        on_output: OutputSink | None = None,
    ) -> str:
        async with self._lock:
            existing = self._operations.get(spec.operation_id)
            if existing:
                if existing.spec.argv != spec.argv or existing.spec.stdin_text != spec.stdin_text:
                    raise DomainError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "Operation id is already bound to different input",
                    )
                return spec.operation_id
            cwd = self.workspace_store.attempt_dir(
                spec.workspace_ref,
                spec.attempt_id,
                spec.user_binding,
            )
            if not cwd.exists():
                self.workspace_store.prepare_attempt(
                    spec.workspace_ref,
                    spec.attempt_id,
                    spec.user_binding,
                )
            if spec.working_directory is not None:
                cwd = Path(spec.working_directory).resolve(strict=False)
                self._validate_working_directory(spec, cwd)
            if spec.user_binding is not None and spec.session_id:
                self.workspace_store.ensure_user_layout(
                    spec.workspace_ref,
                    spec.user_binding,
                )
                op_dir = self.workspace_store.tool_output_dir(
                    spec.user_binding,
                    spec.session_id,
                    spec.run_id,
                    spec.operation_id,
                )
            else:
                op_dir = self.root / spec.operation_id
            op_dir.mkdir(parents=True, exist_ok=True)
            stdout_path = op_dir / "stdout.log"
            stderr_path = op_dir / "stderr.log"
            if spec.stdin_text is not None:
                input_path = op_dir / "input.txt"
                temporary_input = input_path.with_suffix(".txt.tmp")
                temporary_input.write_text(spec.stdin_text, encoding="utf-8")
                temporary_input.replace(input_path)
            process = await asyncio.create_subprocess_exec(
                *spec.argv,
                cwd=cwd,
                env=_clean_env(),
                stdin=(
                    asyncio.subprocess.PIPE
                    if spec.stdin_text is not None
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            operation = _RunningOperation(
                spec=spec,
                process=process,
                stdout_ref=str(stdout_path),
                stderr_ref=str(stderr_path),
                output_sink=on_output,
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
                "stdout",
                operation.output_sink,
            )
        )
        stderr_task = asyncio.create_task(
            self._read_bounded(
                operation.process.stderr,
                stderr_path,
                operation.spec.output_limit_bytes,
                "stderr",
                operation.output_sink,
            )
        )
        stdin_task = None
        if operation.process.stdin is not None:
            stdin_task = asyncio.create_task(
                self._write_stdin(operation.process.stdin, operation.spec.stdin_text or "")
            )
        wait_task = asyncio.create_task(operation.process.wait())
        tasks: set[asyncio.Task[Any]] = {wait_task, stdout_task, stderr_task}
        if stdin_task is not None:
            tasks.add(stdin_task)
        try:
            done, pending = await asyncio.wait(
                tasks,
                timeout=operation.spec.timeout_seconds,
                return_when=asyncio.ALL_COMPLETED,
            )
            if pending:
                self._kill_process_group(operation.process)
                await self._wait_after_termination(wait_task)
                for task in pending:
                    task.cancel()
                operation.status = (
                    ToolStatus.CANCELLED if operation.cancelled.is_set() else ToolStatus.FAILED
                )
                if operation.status == ToolStatus.FAILED:
                    operation.error_code = ErrorCode.EXECUTION_FAILED
                    operation.error_message = (
                        f"Operation timed out after {operation.spec.timeout_seconds} seconds"
                    )
                    self._append_diagnostic(stderr_path, operation.error_message)
            elif operation.cancelled.is_set():
                operation.status = ToolStatus.CANCELLED
            else:
                operation.exit_code = await wait_task
                operation.status = (
                    ToolStatus.SUCCEEDED if operation.exit_code == 0 else ToolStatus.FAILED
                )
                if operation.exit_code != 0:
                    operation.error_code = ErrorCode.EXECUTION_FAILED
                    operation.error_message = f"Process exited with {operation.exit_code}"
        except Exception as exc:
            self._kill_process_group(operation.process)
            operation.status = ToolStatus.FAILED
            operation.error_code = ErrorCode.EXECUTION_FAILED
            operation.error_message = str(exc) or exc.__class__.__name__
            self._append_diagnostic(stderr_path, operation.error_message)
        await asyncio.gather(
            stdout_task,
            stderr_task,
            *([stdin_task] if stdin_task is not None else []),
            return_exceptions=True,
        )
        operation.stdout_truncated = stdout_task.result() if not stdout_task.cancelled() else True
        operation.stderr_truncated = stderr_task.result() if not stderr_task.cancelled() else True
        if operation.status == ToolStatus.SUCCEEDED:
            try:
                revision_id = str(uuid.uuid4())
                commit = self.workspace_store.commit_attempt(
                    operation.spec.workspace_ref,
                    operation.spec.attempt_id,
                    revision_id,
                    operation.spec.user_binding,
                )
                operation.workspace_commit = commit
                operation.workspace_revision = commit.revision_id
            except Exception as exc:
                operation.status = ToolStatus.FAILED
                operation.error_code = ErrorCode.EXECUTION_FAILED
                operation.error_message = f"Workspace commit failed: {exc}"

    @staticmethod
    async def _read_bounded(
        stream: asyncio.StreamReader | None,
        path: Path,
        limit: int,
        name: str = "stdout",
        sink: OutputSink | None = None,
    ) -> bool:
        if stream is None:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        truncated = False
        notified = False
        total = 0
        with path.open("wb") as handle:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                remaining = limit - total
                if remaining <= 0:
                    truncated = True
                    if sink is not None and not notified:
                        sink(name, "", True)
                        notified = True
                    continue
                write_size = min(len(chunk), remaining)
                handle.write(chunk[:write_size])
                total += write_size
                if write_size < len(chunk):
                    truncated = True
                    if sink is not None and not notified:
                        sink(name, decoder.decode(chunk[:write_size], final=False), True)
                        notified = True
                    continue
                if sink is not None:
                    text = decoder.decode(chunk[:write_size], final=False)
                    if text:
                        sink(name, text, False)
        if sink is not None and not truncated:
            tail = decoder.decode(b"", final=True)
            if tail:
                sink(name, tail, False)
        return truncated

    @staticmethod
    async def _write_stdin(
        stream: asyncio.StreamWriter,
        text: str,
    ) -> None:
        try:
            stream.write(text.encode("utf-8"))
            await stream.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            stream.close()

    @staticmethod
    async def _wait_after_termination(wait_task: asyncio.Task[Any]) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(wait_task), timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    @staticmethod
    def _append_diagnostic(path: Path, message: str) -> None:
        try:
            with path.open("ab") as handle:
                handle.write(f"{message}\n".encode())
        except OSError:
            pass

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
        commit = operation.workspace_commit
        return OperationResult(
            operation_id=operation.spec.operation_id,
            status=operation.status,
            exit_code=operation.exit_code,
            stdout_ref=operation.stdout_ref,
            stderr_ref=operation.stderr_ref,
            workspace_revision=operation.workspace_revision,
            error_code=operation.error_code,
            manifest_digest=commit.manifest_digest if commit else None,
            manifest_ref=commit.manifest_ref if commit else None,
            storage_ref=commit.storage_ref if commit else None,
            changed_paths=commit.changed_paths if commit else (),
        )

    @staticmethod
    def _validate_working_directory(spec: OperationSpec, cwd: Path) -> None:
        binding = spec.user_binding
        if binding is None:
            if spec.mount_spec is not None:
                raise DomainError(
                    ErrorCode.INVALID_REQUEST,
                    "MountSpec requires a UserBinding",
                )
            return
        try:
            cwd.relative_to(Path(binding.project_path))
        except ValueError as exc:
            raise DomainError(
                ErrorCode.INVALID_REQUEST,
                "working_directory must be below project_path",
            ) from exc
        if spec.mount_spec is None:
            return
        if spec.mount_spec.granularity == MountGranularity.PROJECT:
            expected = binding.project_ref
        else:
            expected = spec.mount_spec.source_ref
        if (
            spec.mount_spec.source_ref != expected
            and spec.mount_spec.granularity != MountGranularity.REVISION
        ):
            raise DomainError(
                ErrorCode.INVALID_REQUEST,
                "MountSpec source does not match the execution scope",
            )


async def _kill_after_timeout(process: asyncio.subprocess.Process) -> None:
    await asyncio.sleep(5)
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
