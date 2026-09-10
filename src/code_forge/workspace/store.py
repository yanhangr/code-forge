"""Local workspace adapter with attempt staging and revision commit."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Iterable

from code_forge.contracts import DomainError, ErrorCode


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class WorkspaceStore:
    """Filesystem-backed workspace; not a sandbox boundary."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def workspace_root(self, workspace_id: str) -> Path:
        return self.root / "current" / workspace_id

    def ensure_workspace(self, workspace_id: str) -> Path:
        path = self.workspace_root(workspace_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def attempt_dir(self, workspace_id: str, attempt_id: str) -> Path:
        return self.root / "attempts" / workspace_id / attempt_id

    def prepare_attempt(self, workspace_id: str, attempt_id: str) -> Path:
        current = self.ensure_workspace(workspace_id)
        attempt = self.attempt_dir(workspace_id, attempt_id)
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
    ) -> tuple[str, list[str]]:
        attempt = self.attempt_dir(workspace_id, attempt_id)
        if not attempt.exists():
            raise DomainError(ErrorCode.EXECUTION_FAILED, "Attempt directory is missing")
        current = self.ensure_workspace(workspace_id)
        before = self._manifest(current)
        after = self._manifest(attempt)
        changed = [
            path
            for path in sorted(set(before) | set(after))
            if before.get(path) != after.get(path)
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
        manifest_digest = self._write_manifest(current, after)
        return manifest_digest, changed

    def _manifest(self, root: Path) -> dict[str, str]:
        manifest: dict[str, str] = {}
        if not root.exists():
            return manifest
        for path in root.rglob("*"):
            if path.is_file() and path.name != ".forge-manifest.json":
                manifest[path.relative_to(root).as_posix()] = _digest(path)
        return manifest

    def _write_manifest(self, root: Path, manifest: dict[str, str]) -> str:
        encoded = json.dumps(
            {"files": manifest, "schema_version": 1},
            ensure_ascii=False,
            sort_keys=True,
        )
        manifest_path = root / ".forge-manifest.json"
        manifest_path.write_text(encoded, encoding="utf-8")
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _safe_relative(root: Path, relative_path: str) -> Path:
        candidate = (root / relative_path).resolve()
        root_resolved = root.resolve()
        if candidate != root_resolved and root_resolved not in candidate.parents:
            raise DomainError(ErrorCode.INVALID_REQUEST, "Path escapes workspace")
        return candidate

    def write_text(self, workspace_id: str, attempt_id: str, relative_path: str, content: str) -> Path:
        attempt = self.attempt_dir(workspace_id, attempt_id)
        target = self._safe_relative(attempt, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def read_text(self, workspace_id: str, relative_path: str, limit: int = 1024 * 1024) -> tuple[str, str, bool]:
        current = self.ensure_workspace(workspace_id)
        target = self._safe_relative(current, relative_path)
        if not target.is_file():
            raise DomainError(ErrorCode.INVALID_REQUEST, "File not found")
        size = target.stat().st_size
        with target.open("rb") as handle:
            data = handle.read(limit + 1)
        truncated = len(data) > limit
        text = data[:limit].decode("utf-8", errors="replace")
        return text, _digest(target), truncated

    def list_files(self, workspace_id: str) -> list[dict[str, object]]:
        current = self.ensure_workspace(workspace_id)
        files = []
        for path in current.rglob("*"):
            if not path.is_file() or path.name == ".forge-manifest.json":
                continue
            relative = path.relative_to(current).as_posix()
            files.append(
                {
                    "path": relative,
                    "size_bytes": path.stat().st_size,
                    "digest": _digest(path),
                }
            )
        return sorted(files, key=lambda item: str(item["path"]))

    def changed_paths(self, paths: Iterable[str]) -> list[str]:
        return sorted(set(paths))
