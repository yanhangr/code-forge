"""Local workspace adapter with attempt staging and revision commit."""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Iterable

from code_forge.contracts import DomainError, ErrorCode, UserBinding, WorkspaceCommit


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class WorkspaceStore:
    """Filesystem-backed workspace; not a sandbox boundary."""

    internal_dirname = ".forge"

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._storage_lock = threading.Lock()

    @staticmethod
    def user_root(user_binding: UserBinding) -> Path:
        return Path(user_binding.user_path)

    def ensure_user_layout(
        self,
        workspace_id: str,
        user_binding: UserBinding,
    ) -> Path:
        """Materialize the trusted User Root while keeping logical relations in DB."""

        user_root = self.user_root(user_binding)
        for relative in (
            "workspace/projects",
            "sessions",
            "config/agents",
            "config/skills",
            "tool-output",
            "snapshots",
        ):
            (user_root / relative).mkdir(parents=True, exist_ok=True)
        return self.ensure_workspace(workspace_id, user_binding)

    def session_dir(self, user_binding: UserBinding, session_id: str) -> Path:
        return self.user_root(user_binding) / "sessions" / session_id

    def ensure_session_storage(
        self,
        session: Mapping[str, object],
        user_binding: UserBinding,
    ) -> Path:
        session_id = str(session["id"])
        session_directory = self.session_dir(user_binding, session_id)
        session_directory.mkdir(parents=True, exist_ok=True)
        metadata = {
            "session_id": session_id,
            "title": str(session.get("title") or ""),
            "workspace_id": str(session.get("workspace_id") or ""),
            "user_ref": user_binding.user_ref,
            "tenant_ref": user_binding.tenant_ref,
            "user_rel_path": user_binding.user_rel_path,
            "project_ref": user_binding.project_ref,
            "project_rel_path": user_binding.project_rel_path,
            "project_path": user_binding.project_path,
            "thread_id": str(session.get("thread_id") or ""),
            "date_created": str(session.get("date_created") or ""),
            "date_updated": str(session.get("date_updated") or ""),
        }
        metadata_path = session_directory / "session.json"
        with self._storage_lock:
            temporary = metadata_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(metadata_path)
            transcript = session_directory / "transcript-000001.jsonl"
            transcript.touch(exist_ok=True)
        return session_directory

    def append_transcript(
        self,
        user_binding: UserBinding,
        session_id: str,
        record: Mapping[str, object],
        *,
        max_file_bytes: int = 10 * 1024 * 1024,
    ) -> Path:
        """Idempotently append one JSONL transcript record and rotate by file size."""

        session_directory = self.session_dir(user_binding, session_id)
        session_directory.mkdir(parents=True, exist_ok=True)
        encoded = (
            json.dumps(dict(record), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        record_id = str(record.get("record_id") or "")
        with self._storage_lock:
            files = sorted(session_directory.glob("transcript-*.jsonl"))
            if not files:
                files = [session_directory / "transcript-000001.jsonl"]
            for path in files:
                if record_id and self._transcript_contains(path, record_id):
                    return path
            target = files[-1]
            if (
                target.exists()
                and target.stat().st_size
                and (target.stat().st_size + len(encoded) > max_file_bytes)
            ):
                next_index = len(files) + 1
                target = session_directory / f"transcript-{next_index:06d}.jsonl"
            with target.open("ab") as handle:
                handle.write(encoded)
            return target

    @staticmethod
    def _transcript_contains(path: Path, record_id: str) -> bool:
        if not path.is_file():
            return False
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("record_id") == record_id:
                        return True
        except OSError:
            return False
        return False

    def tool_output_dir(
        self,
        user_binding: UserBinding,
        session_id: str,
        run_id: str,
        operation_id: str,
    ) -> Path:
        path = self.user_root(user_binding) / "tool-output" / session_id / run_id / operation_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def workspace_root(self, workspace_id: str, user_binding: UserBinding | None = None) -> Path:
        if user_binding is not None:
            return Path(user_binding.project_path)
        return self.root / "current" / workspace_id

    def ensure_workspace(self, workspace_id: str, user_binding: UserBinding | None = None) -> Path:
        path = self.workspace_root(workspace_id, user_binding)
        path.mkdir(parents=True, exist_ok=True)
        if user_binding is not None:
            (path / self.internal_dirname / "revisions").mkdir(parents=True, exist_ok=True)
            (path / self.internal_dirname / "manifests").mkdir(parents=True, exist_ok=True)
        return path

    def attempt_dir(
        self,
        workspace_id: str,
        attempt_id: str,
        user_binding: UserBinding | None = None,
    ) -> Path:
        if user_binding is not None:
            return self.ensure_workspace(workspace_id, user_binding)
        return self.root / "attempts" / workspace_id / attempt_id

    def prepare_attempt(
        self,
        workspace_id: str,
        attempt_id: str,
        user_binding: UserBinding | None = None,
    ) -> Path:
        current = self.ensure_workspace(workspace_id, user_binding)
        attempt = self.attempt_dir(workspace_id, attempt_id, user_binding)
        if user_binding is not None:
            return attempt
        if attempt.exists():
            shutil.rmtree(attempt)
        attempt.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(current, attempt)
        return attempt

    def commit_attempt(
        self,
        workspace_id: str,
        attempt_id: str,
        revision_id: str,
        user_binding: UserBinding | None = None,
    ) -> WorkspaceCommit:
        attempt = self.attempt_dir(workspace_id, attempt_id, user_binding)
        if not attempt.exists():
            raise DomainError(ErrorCode.EXECUTION_FAILED, "Attempt directory is missing")
        current = self.ensure_workspace(workspace_id, user_binding)
        if user_binding is not None:
            return self._commit_bound_project(
                workspace_id,
                revision_id,
                user_binding,
                current,
            )

        before = self._manifest(current)
        after = self._manifest(attempt)
        changed = [
            path for path in sorted(set(before) | set(after)) if before.get(path) != after.get(path)
        ]
        revision_dir = self.root / "revisions" / workspace_id / revision_id
        revision_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(attempt, revision_dir, dirs_exist_ok=True)

        for path in sorted(set(before) - set(after)):
            target = current / path
            if target.exists() or target.is_symlink():
                target.unlink()
        for path in sorted(after):
            source = attempt / path
            target = current / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        manifest_digest, manifest_ref = self._write_manifest(current, after)
        return WorkspaceCommit(
            revision_id=revision_id,
            manifest_digest=manifest_digest,
            manifest_ref=manifest_ref,
            storage_ref=current.as_posix(),
            changed_paths=tuple(changed),
        )

    def _commit_bound_project(
        self,
        workspace_id: str,
        revision_id: str,
        user_binding: UserBinding,
        project: Path,
    ) -> WorkspaceCommit:
        internal = project / self.internal_dirname
        manifest_dir = internal / "manifests"
        revision_dir = internal / "revisions" / revision_id
        if revision_dir.exists():
            raise DomainError(ErrorCode.IDEMPOTENCY_CONFLICT, "Revision already exists")

        previous_manifest_path = manifest_dir / "current.json"
        before = self._load_manifest(previous_manifest_path)
        after = self._manifest(project)
        changed = [
            path for path in sorted(set(before) | set(after)) if before.get(path) != after.get(path)
        ]
        revision_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            project,
            revision_dir,
            ignore=shutil.ignore_patterns(self.internal_dirname),
        )
        manifest = {"files": after, "schema_version": 2}
        encoded = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        manifest_digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        manifest_path = manifest_dir / f"{revision_id}.json"
        manifest_path.write_text(encoded, encoding="utf-8")
        previous_manifest_path.write_text(encoded, encoding="utf-8")
        return WorkspaceCommit(
            revision_id=revision_id,
            manifest_digest=manifest_digest,
            manifest_ref=manifest_path.as_posix(),
            storage_ref=revision_dir.as_posix(),
            changed_paths=tuple(changed),
        )

    def _manifest(self, root: Path) -> dict[str, str]:
        manifest: dict[str, str] = {}
        if not root.exists():
            return manifest
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            if relative.parts and relative.parts[0] == self.internal_dirname:
                continue
            if path.is_file() and path.name != ".forge-manifest.json":
                manifest[path.relative_to(root).as_posix()] = _digest(path)
        return manifest

    @staticmethod
    def _load_manifest(path: Path) -> dict[str, str]:
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        files = value.get("files")
        return files if isinstance(files, dict) else {}

    def _write_manifest(self, root: Path, manifest: dict[str, str]) -> tuple[str, str]:
        encoded = json.dumps(
            {"files": manifest, "schema_version": 1},
            ensure_ascii=False,
            sort_keys=True,
        )
        manifest_path = root / ".forge-manifest.json"
        manifest_path.write_text(encoded, encoding="utf-8")
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest(), manifest_path.as_posix()

    @staticmethod
    def _safe_relative(root: Path, relative_path: str) -> Path:
        candidate = (root / relative_path).resolve()
        root_resolved = root.resolve()
        if candidate != root_resolved and root_resolved not in candidate.parents:
            raise DomainError(ErrorCode.INVALID_REQUEST, "Path escapes workspace")
        return candidate

    def write_text(
        self,
        workspace_id: str,
        attempt_id: str,
        relative_path: str,
        content: str,
        user_binding: UserBinding | None = None,
    ) -> Path:
        attempt = self.attempt_dir(workspace_id, attempt_id, user_binding)
        target = self._safe_relative(attempt, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def read_text(
        self,
        workspace_id: str,
        relative_path: str,
        limit: int = 1024 * 1024,
        user_binding: UserBinding | None = None,
    ) -> tuple[str, str, bool]:
        if relative_path == self.internal_dirname or relative_path.startswith(
            f"{self.internal_dirname}/"
        ):
            raise DomainError(ErrorCode.INVALID_REQUEST, "Runtime metadata is not readable")
        current = self.ensure_workspace(workspace_id, user_binding)
        target = self._safe_relative(current, relative_path)
        if not target.is_file():
            raise DomainError(ErrorCode.INVALID_REQUEST, "File not found")
        with target.open("rb") as handle:
            data = handle.read(limit + 1)
        truncated = len(data) > limit
        text = data[:limit].decode("utf-8", errors="replace")
        return text, _digest(target), truncated

    def list_files(
        self, workspace_id: str, user_binding: UserBinding | None = None
    ) -> list[dict[str, object]]:
        current = self.ensure_workspace(workspace_id, user_binding)
        files = []
        for path in current.rglob("*"):
            relative = path.relative_to(current)
            if relative.parts and relative.parts[0] == self.internal_dirname:
                continue
            if not path.is_file() or path.name == ".forge-manifest.json":
                continue
            files.append(
                {
                    "path": relative.as_posix(),
                    "size_bytes": path.stat().st_size,
                    "digest": _digest(path),
                }
            )
        return sorted(files, key=lambda item: str(item["path"]))

    def changed_paths(self, paths: Iterable[str]) -> list[str]:
        return sorted(set(paths))
