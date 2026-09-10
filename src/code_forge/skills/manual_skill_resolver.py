"""Manual filesystem Skill resolver and immutable snapshot storage."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from code_forge.contracts import DomainError, ErrorCode, RunRequest, RunSnapshot, SkillBinding, SkillRef
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
    """Reads skills/<name> and stores a full byte-for-byte immutable bundle."""

    def __init__(self, skills_root: str | Path, snapshot_root: str | Path):
        self.skills_root = Path(skills_root)
        self.snapshot_root = Path(snapshot_root)
        self.snapshot_root.mkdir(parents=True, exist_ok=True)

    def list_skills(self) -> list[dict[str, Any]]:
        if not self.skills_root.exists():
            return []
        skills: list[dict[str, Any]] = []
        for skill_dir in sorted(self.skills_root.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue
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
                }
            )
        return skills

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
        for binding in request.skills:
            self._resolve_skill_tree(binding, resolved, set())
        return RunSnapshot(
            agent_digest=hashlib.sha256(request.agent_ref.encode("utf-8")).hexdigest(),
            runtime_ref="runtime@local",
            model_profile_ref="deterministic@local",
            execution_profile_ref="local@1",
            skills=tuple(resolved.values()),
            tool_refs=("python", "command", "file_read", "file_write"),
        )

    def _resolve_skill_tree(
        self,
        binding: SkillBinding,
        resolved: dict[str, SkillRef],
        visiting: set[str],
    ) -> None:
        meta = self._read_binding_meta(binding)
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
            )
        visiting.remove(name)
        resolved[name] = self._store_bundle(meta)

    def _read_binding_meta(self, binding: SkillBinding) -> dict[str, Any]:
        skill_dir = self.skills_root / binding.name
        if not skill_dir.is_dir():
            raise DomainError(ErrorCode.SKILL_NOT_FOUND, f"Skill not found: {binding.name}")
        meta = self._read_skill(skill_dir)
        if binding.version and binding.version != meta["version"]:
            raise DomainError(
                ErrorCode.SKILL_INCOMPATIBLE,
                f"Skill {binding.name} requested {binding.version}, found {meta['version']}",
            )
        return meta

    def _store_bundle(self, meta: dict[str, Any]) -> SkillRef:
        destination = self.snapshot_root / meta["digest"]
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
