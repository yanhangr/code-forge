"""Tiny .env loader with no third-party dependencies.

Existing process environment variables always win over .env values.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(root: str | Path) -> None:
    env_path = Path(root) / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        os.environ.setdefault(key, value)
