"""Manual filesystem Skill resolver and immutable snapshot storage."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

from code_forge.contracts import (
    DomainError,
    ErrorCode,
    RunRequest,
    RunSnapshot,
    SkillBinding,
    SkillRef,
    UserBinding,
)
from code_forge.ports import SnapshotResolver

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)


def _parse_frontmatter(text: str) -> dict[str, Any]:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}
    result: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key == "requires":
            result[key] = _parse_requires(value)
        else:
            result[key] = value
    return result


def _parse_requires(value: str) -> list[str]:
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = []
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in value.split(",") if item.strip()]


def _bundle_digest(root: Path) -> str:
    hasher = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                hasher.update(chunk)
        hasher.update(b"\0")
    return hasher.hexdigest()


class ManualSkillResolver(SnapshotResolver):
    """Reads an ordered Skill root list and stores immutable byte-for-byte bundles."""

    def __init__(self, skills_root: str | Path, snapshot_root: str | Path):
        self.skills_root = Path(skills_root)
        self.snapshot_root = Path(snapshot_root)
        self.snapshot_root.mkdir(parents=True, exist_ok=True)

    def list_skills(
        self,
        user_binding: UserBinding | None = None,
        skill_paths: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        by_name: dict[str, dict[str, Any]] = {}
        for source_kind, root in self._skill_roots(user_binding, skill_paths):
            for item in self._list_root(root):
                by_name.setdefault(item["name"], {**item, "source_kind": source_kind})
        return [by_name[name] for name in sorted(by_name)]

    def _list_root(self, root: Path) -> list[dict[str, Any]]:
        if not root.exists():
            return []
        skills: list[dict[str, Any]] = []
        for skill_dir in self._skill_dirs(root):
            try:
                meta = self._read_skill(skill_dir)
            except DomainError:
                continue
            skills.append(
                {
                    "name": meta["name"],
                    "version": meta["version"],
                    "digest": meta["digest"],
                    "description": meta.get("description", ""),
                    "source_path": root.as_posix(),
                }
            )
        return skills

    def _skill_roots(
        self,
        binding: UserBinding | None,
        skill_paths: tuple[str, ...] | None,
    ) -> list[tuple[str, Path]]:
        if binding is None:
            return [("LEGACY", self.skills_root)]
        if skill_paths is None:
            return [("USER_DEFAULT", Path(binding.default_skill_path))]
        roots: list[tuple[str, Path]] = []
        for path in skill_paths:
            root = Path(path)
            source_kind = "DIRECT_PACKAGE" if (root / "SKILL.md").is_file() else "EXPLICIT"
            roots.append((source_kind, root))
        return roots

    @staticmethod
    def _skill_dirs(root: Path) -> list[Path]:
        if (root / "SKILL.md").is_file():
            return [root]
        if not root.is_dir():
            return []
        return sorted(
            child for child in root.iterdir() if child.is_dir() and (child / "SKILL.md").is_file()
        )

    @staticmethod
    def _effective_paths(
        binding: UserBinding | None,
        skill_paths: tuple[str, ...] | None,
    ) -> tuple[tuple[str, ...], str]:
        if binding is None:
            return (), "LEGACY"
        if skill_paths is None:
            return (binding.default_skill_path,), "USER_DEFAULT"
        return skill_paths, "EXPLICIT"

    def _read_skill(self, skill_dir: Path) -> dict[str, Any]:
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            raise DomainError(ErrorCode.SKILL_NOT_FOUND, f"Missing SKILL.md for {skill_dir.name}")
        text = skill_md.read_text(encoding="utf-8")
        meta = _parse_frontmatter(text)
        name = meta.get("name", skill_dir.name)
        version = meta.get("version", "")
        description = meta.get("description", "")
        if not name or not version:
            raise DomainError(
                ErrorCode.SKILL_INCOMPATIBLE,
                f"Skill {skill_dir.name} requires name/version frontmatter",
            )
        return {
            "name": name,
            "version": version,
            "description": description,
            "requires": meta.get("requires", []),
            "digest": _bundle_digest(skill_dir),
            "dir": skill_dir,
        }

    async def resolve_and_store(self, request: RunRequest) -> RunSnapshot:
        resolved: dict[str, SkillRef] = {}
        effective_paths, path_source = self._effective_paths(
            request.context.user_binding,
            request.skill_paths,
        )
        for binding in request.skills:
            self._resolve_skill_tree(
                binding,
                resolved,
                set(),
                request.context.user_binding,
                request.skill_paths,
            )
        if (
            request.context.user_binding is not None
            and not request.skills
            and request.skill_paths != ()
        ):
            for source_kind, root in self._skill_roots(
                request.context.user_binding,
                request.skill_paths,
            ):
                for skill_dir in self._skill_dirs(root):
                    meta = self._read_skill(skill_dir)
                    self._resolve_skill_tree(
                        SkillBinding(name=meta["name"], version=meta["version"]),
                        resolved,
                        set(),
                        request.context.user_binding,
                        request.skill_paths,
                    )
                    if meta["name"] in resolved:
                        resolved[meta["name"]] = replace(
                            resolved[meta["name"]],
                            source_kind=source_kind,
                            source_path=root.as_posix(),
                        )
        path_digest = self._path_digest(request.context.user_binding, effective_paths)
        return RunSnapshot(
            agent_digest=hashlib.sha256(request.agent_ref.encode("utf-8")).hexdigest(),
            runtime_ref="runtime@local",
            model_profile_ref="deterministic@local",
            execution_profile_ref="local@1",
            skills=tuple(resolved.values()),
            tool_refs=("python", "command", "file_read", "file_write"),
            user_binding=request.context.user_binding,
            effective_skill_paths=effective_paths,
            skill_path_source=path_source,
            path_digest=path_digest,
        )

    def _resolve_skill_tree(
        self,
        binding: SkillBinding,
        resolved: dict[str, SkillRef],
        visiting: set[str],
        user_binding: UserBinding | None,
        skill_paths: tuple[str, ...] | None,
    ) -> None:
        meta = self._read_binding_meta(binding, user_binding, skill_paths)
        name = meta["name"]
        if name in resolved:
            if resolved[name].version != meta["version"]:
                raise DomainError(
                    ErrorCode.SKILL_INCOMPATIBLE,
                    f"Skill {name} is requested with conflicting versions",
                )
            return
        if name in visiting:
            raise DomainError(
                ErrorCode.SKILL_INCOMPATIBLE,
                f"Skill dependency cycle detected at {name}",
            )
        visiting.add(name)
        for dependency_name in meta["requires"]:
            self._resolve_skill_tree(
                SkillBinding(name=dependency_name, version=None),
                resolved,
                visiting,
                user_binding,
                skill_paths,
            )
        visiting.remove(name)
        resolved[name] = self._store_bundle(meta, user_binding)

    def _read_binding_meta(
        self,
        binding: SkillBinding,
        user_binding: UserBinding | None,
        skill_paths: tuple[str, ...] | None,
    ) -> dict[str, Any]:
        found: dict[str, Any] | None = None
        for source_kind, root in self._skill_roots(user_binding, skill_paths):
            if (root / "SKILL.md").is_file():
                meta = self._read_skill(root)
                if meta["name"] != binding.name:
                    continue
                found = {
                    **meta,
                    "source_kind": source_kind,
                    "source_path": root.as_posix(),
                }
                break
            skill_dir = root / binding.name
            if not skill_dir.is_dir():
                continue
            found = {
                **self._read_skill(skill_dir),
                "source_kind": source_kind,
                "source_path": root.as_posix(),
            }
            break
        if found is None:
            raise DomainError(ErrorCode.SKILL_NOT_FOUND, f"Skill not found: {binding.name}")
        meta = found
        if binding.version and binding.version != meta["version"]:
            raise DomainError(
                ErrorCode.SKILL_INCOMPATIBLE,
                f"Skill {binding.name} requested {binding.version}, found {meta['version']}",
            )
        return meta

    @staticmethod
    def _path_digest(binding: UserBinding | None, skill_paths: tuple[str, ...]) -> str:
        if binding is None:
            return ""
        encoded = "\0".join((binding.user_path, binding.project_path or "", *skill_paths)).encode(
            "utf-8"
        )
        return hashlib.sha256(encoded).hexdigest()

    def _store_bundle(self, meta: dict[str, Any], user_binding: UserBinding | None) -> SkillRef:
        snapshot_root = self.snapshot_root
        if user_binding is not None:
            snapshot_root = (
                snapshot_root / hashlib.sha256(user_binding.scope_id.encode("utf-8")).hexdigest()
            )
        destination = snapshot_root / meta["digest"]
        if destination.exists():
            if not any(destination.iterdir()):
                shutil.rmtree(destination)
            else:
                # Same digest means the same bytes. Leave the immutable bundle alone.
                pass
        if not destination.exists():
            destination.mkdir(parents=True, exist_ok=True)
            for source in meta["dir"].rglob("*"):
                if not source.is_file():
                    continue
                relative = source.relative_to(meta["dir"])
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        return SkillRef(
            name=meta["name"],
            version=meta["version"],
            digest=meta["digest"],
            bundle_ref=destination.as_posix(),
            source_kind=meta.get("source_kind", "LEGACY"),
            source_path=meta.get("source_path", ""),
            user_ref=user_binding.user_ref if user_binding else "",
            project_ref=user_binding.project_ref if user_binding else "",
        )

    def load_skill_body(self, skill_ref: SkillRef) -> str:
        """Return the SKILL.md body without frontmatter for harness injection."""

        skill_md = Path(skill_ref.bundle_ref) / "SKILL.md"
        if not skill_md.is_file():
            raise DomainError(ErrorCode.SKILL_NOT_FOUND, f"Missing snapshot for {skill_ref.name}")
        text = skill_md.read_text(encoding="utf-8")
        match = _FRONTMATTER.match(text)
        if match:
            return text[match.end() :].strip()
        return text.strip()
