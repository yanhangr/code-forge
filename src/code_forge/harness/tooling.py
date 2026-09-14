"""Shared construction and persistence helpers for tool execution adapters."""

from __future__ import annotations

from typing import Any

from code_forge.contracts import (
    DomainError,
    ErrorCode,
    MountGranularity,
    MountSpec,
    OperationResult,
    OperationSpec,
    UserBinding,
    WorkspaceCommit,
)


def build_operation_spec(
    *,
    operation_id: str,
    run_id: str,
    attempt_id: str,
    workspace_id: str,
    workspace_epoch: int,
    user_binding: UserBinding | None,
    working_directory: str | None,
    argv: tuple[str, ...],
    timeout_seconds: int,
    output_limit_bytes: int,
    environment_profile_ref: str,
) -> OperationSpec:
    cwd = working_directory or (user_binding.project_path if user_binding else None)
    mount_spec = None
    if user_binding is not None:
        if cwd is None:
            raise DomainError(ErrorCode.INVALID_REQUEST, "Bound execution requires a cwd")
        mount_spec = MountSpec(
            spec_version="1",
            granularity=MountGranularity.PROJECT,
            source_ref=user_binding.project_ref,
            target="/workspace",
            mode="rw",
            cwd=cwd,
        )
    return OperationSpec(
        operation_id=operation_id,
        run_id=run_id,
        attempt_id=attempt_id,
        workspace_ref=workspace_id,
        argv=argv,
        timeout_seconds=timeout_seconds,
        output_limit_bytes=output_limit_bytes,
        environment_profile_ref=environment_profile_ref,
        user_binding=user_binding,
        workspace_epoch=workspace_epoch,
        mount_spec=mount_spec,
        working_directory=cwd,
    )


def persist_workspace_commit(
    store: Any,
    result: OperationResult,
    *,
    scope_id: str,
    workspace_id: str,
    attempt_id: str,
    workspace_epoch: int,
    actor: str,
) -> WorkspaceCommit | None:
    if result.workspace_revision is None:
        return None
    if result.manifest_digest is None or result.manifest_ref is None or result.storage_ref is None:
        raise DomainError(
            ErrorCode.EXECUTION_FAILED,
            "Tool committed files without revision metadata",
        )
    commit = WorkspaceCommit(
        revision_id=result.workspace_revision,
        manifest_digest=result.manifest_digest,
        manifest_ref=result.manifest_ref,
        storage_ref=result.storage_ref,
        changed_paths=result.changed_paths,
    )
    store.record_workspace_revision(
        scope_id,
        workspace_id,
        attempt_id,
        workspace_epoch,
        commit,
        actor,
    )
    return commit
