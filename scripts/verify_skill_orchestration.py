#!/usr/bin/env python3
"""Verify one Skill resolves and snapshots multiple nested Skills."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))

    from code_forge.contracts import RunRequest, SkillBinding
    from code_forge.skills.manual_skill_resolver import ManualSkillResolver

    async def run() -> None:
        resolver = ManualSkillResolver(
            root / "skills",
            Path(os.environ.get("FORGE_RUNTIME_DIR", ".runtime")) / "skill-snapshots",
        )
        snapshot = await resolver.resolve_and_store(
            RunRequest(
                session_id="verification",
                input="verify nested skills",
                skills=(SkillBinding(name="analysis-report", version="1"),),
            )
        )
        for skill in snapshot.skills:
            print(
                f"{skill.name}@{skill.version} "
                f"digest={skill.digest[:12]} bundle={skill.bundle_ref}"
            )

    asyncio.run(run())


if __name__ == "__main__":
    main()
