"""Local workspace adapter with attempt staging and revision commit."""

from __future__ import annotations

import hashlib
import json
import shutil
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
