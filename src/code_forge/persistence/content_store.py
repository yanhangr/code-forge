"""File-backed content store for PostgreSQL + JSONL storage.

PostgreSQL keeps relational facts, status, sequence numbers, idempotency,
audit fields and content references. This adapter keeps the actual bodies
(user input, assistant output, event payloads, configuration snapshots and
pending responses) in append-only Session JSONL logs and immutable object
files under the trusted User Root.

The database stores only ``ref``, ``offset``, ``bytes`` and ``digest`` so no
single column needs to hold a large text or JSON body. Bodies are written
before the database transaction that references them; a crash between the two
leaves an unreferenced record that garbage collection may reclaim later.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    """Deterministic JSON used for digests and on-disk records."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def digest_json(value: Any) -> str:
    return digest_bytes(canonical_json(value).encode("utf-8"))


def digest_text(value: str) -> str:
    return digest_bytes(value.encode("utf-8"))


@dataclass(frozen=True)
class ContentRef:
    """Location of one JSONL record: relative path plus byte span."""

    ref: str
    offset: int
    bytes: int
    digest: str


@dataclass(frozen=True)
class ObjectRef:
    """Location of one immutable object file."""

    ref: str
    bytes: int
    digest: str


class FileContentStore:
    """Stable development adapter; the production target is the same contract."""

    def __init__(self, max_file_bytes: int = 8 * 1024 * 1024):
        self.max_file_bytes = max_file_bytes
        self._lock = threading.RLock()

    def append_record(
        self,
        base: Path,
        relative_dir: str,
        record: dict[str, Any],
        *,
        prefix: str = "content",
    ) -> ContentRef:
        """Append one JSONL record and return its location.

        Record bodies are written and flushed before the caller commits the
        referencing database transaction; an uncommitted orphan is reclaimed by
        garbage collection rather than repaired in place.
        """

        encoded = (canonical_json(record) + "\n").encode("utf-8")
        relative_dir_path = Path(relative_dir)
        with self._lock:
            directory = base / relative_dir_path
            directory.mkdir(parents=True, exist_ok=True)
            target = self._next_file(directory, prefix, len(encoded))
            offset = target.stat().st_size if target.exists() else 0
            with target.open("ab") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            return ContentRef(
                ref=(relative_dir_path / target.name).as_posix(),
                offset=offset,
                bytes=len(encoded),
                digest=digest_json(record),
            )

    def _next_file(self, directory: Path, prefix: str, incoming: int) -> Path:
        files = sorted(directory.glob(f"{prefix}-*.jsonl"))
        if not files:
            return directory / f"{prefix}-000001.jsonl"
        candidate = files[-1]
        if (
            candidate.exists()
            and candidate.stat().st_size
            and candidate.stat().st_size + incoming > self.max_file_bytes
        ):
            return directory / f"{prefix}-{len(files) + 1:06d}.jsonl"
        return candidate

    def read_record(self, base: Path, ref: str, offset: int, length: int) -> dict[str, Any]:
        with (base / ref).open("rb") as handle:
            handle.seek(offset)
            raw = handle.read(length)
        return json.loads(raw.decode("utf-8"))

    def read_verified(
        self,
        base: Path,
        ref: str,
        offset: int,
        length: int,
        expected_digest: str | None,
    ) -> dict[str, Any]:
        record = self.read_record(base, ref, offset, length)
        if expected_digest and digest_json(record) != expected_digest:
            raise ContentUnavailable(f"Content digest mismatch for {ref}")
        return record

    def write_object(self, base: Path, relative_path: str, payload: bytes) -> ObjectRef:
        """Write one immutable object file (idempotent by path)."""

        target = base / relative_path
        with self._lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                temporary = target.with_name(target.name + ".tmp")
                temporary.write_bytes(payload)
                temporary.replace(target)
            return ObjectRef(
                ref=relative_path,
                bytes=len(payload),
                digest=digest_bytes(payload),
            )

    def read_object(self, base: Path, ref: str) -> bytes:
        try:
            return (base / ref).read_bytes()
        except OSError as exc:
            raise ContentUnavailable(f"Content object is unavailable: {ref}") from exc


class ContentUnavailable(Exception):
    """Referenced body is missing or fails digest verification."""
