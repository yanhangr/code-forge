"""Server-owned department bindings and bounded query policy.

The file contains secret *references*, never passwords. Reload on each resolution
so disabling a department invalidates subsequent dispatches. Identity is supplied
by the Runtime, not by model tool arguments.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from code_forge.contracts import DomainError, ErrorCode


@dataclass(frozen=True)
class QueryPolicy:
    default_rows: int = 100
    max_rows: int = 1000
    max_bytes: int = 32000
    cell_bytes: int = 2048
    max_columns: int = 100
    timeout_seconds: int = 60
    rpc_timeout_seconds: int = 5
    mem_limit_mb: int = 1024
    scan_bytes_limit_mb: int = 10240
    max_user_concurrency: int = 1
    max_department_concurrency: int = 4
    max_queries_per_message: int = 12
    max_rows_per_message: int = 5000
    max_bytes_per_message: int = 256000
    max_queries_per_user_hour: int = 120
    max_rows_per_user_hour: int = 20000
    max_bytes_per_user_hour: int = 1024000
    max_queries_per_department_hour: int = 600
    max_rows_per_department_hour: int = 100000
    max_bytes_per_department_hour: int = 5120000
    # Raw table names. For these tables require a predicate on the configured column.
    required_filters: tuple[tuple[str, str], ...] = ()
    allowed_udfs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for key, value in asdict(self).items():
            if key in {"required_filters", "allowed_udfs"}:
                continue
            if type(value) is not int or value <= 0:
                raise ValueError(f"Invalid Impala policy: {key}")
        if not self.default_rows <= self.max_rows <= 10000:
            raise ValueError("Impala row bounds must satisfy default <= max <= 10000")
        if not 4096 <= self.max_bytes <= 256000 or self.cell_bytes > self.max_bytes:
            raise ValueError("Invalid Impala byte bounds")
        if self.max_columns > 100 or self.timeout_seconds > 3600:
            raise ValueError("Impala column/time limits are too large")
        if self.rpc_timeout_seconds > self.timeout_seconds:
            raise ValueError("RPC timeout must not exceed query deadline")


@dataclass(frozen=True)
class DepartmentProfile:
    department: str
    host: str
    user: str
    port: int = 21050
    database: str = "default"
    auth_mechanism: str = "LDAP"
    password_env: str = ""
    ca_cert: str = ""
    use_http_transport: bool = False
    http_path: str = ""
    request_pool: str = ""
    policy: QueryPolicy = QueryPolicy()

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


class DepartmentBindings:
    def __init__(self, path: Path):
        self.path = path

    def resolve(self, scope_id: str) -> DepartmentProfile | None:
        try:
            if self.path.stat().st_size > 128000:
                raise ValueError("Impala configuration too large")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            mode = data["mode"]
            departments = data["departments"]
            if mode == "trusted_single_department":
                if len(departments) != 1:
                    raise ValueError("Single-department mode requires exactly one department")
                department = data["default_department"]
            elif mode == "mapped_departments":
                department = data.get("scope_departments", {}).get(scope_id)
                if department is None:
                    return None
            else:
                raise ValueError("Unknown Impala binding mode")
            raw = dict(departments[department])
            if raw.pop("enabled", True) is not True:
                return None
            policy = raw.pop("policy", {})
            policy["required_filters"] = tuple(sorted(policy.get("required_filters", {}).items()))
            policy["allowed_udfs"] = tuple(policy.get("allowed_udfs", []))
            profile = DepartmentProfile(department=department, policy=QueryPolicy(**policy), **raw)
            if (
                not isinstance(profile.host, str)
                or not 1 <= len(profile.host) <= 255
                or not isinstance(profile.user, str)
                or not 1 <= len(profile.user) <= 200
                or type(profile.port) is not int
                or not 1 <= profile.port <= 65535
                or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", department)
                or profile.auth_mechanism != "LDAP"
                or type(profile.use_http_transport) is not bool
            ):
                raise ValueError("Invalid Impala connection profile")
            if profile.auth_mechanism == "LDAP" and not profile.password_env:
                raise ValueError("LDAP requires a password environment reference")
            if profile.use_http_transport:
                # Impyla 0.23.0 ignores its timeout argument for HTTP transport.
                raise ValueError("HTTP transport requires a verified RPC-timeout adapter")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", profile.database):
                raise ValueError("Invalid default database")
            return profile
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise DomainError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "Invalid or unavailable server Impala configuration; check operator settings",
            ) from exc

    def require(self, scope_id: str, expected_digest: str | None = None) -> DepartmentProfile:
        profile = self.resolve(scope_id)
        if profile is None:
            raise DomainError(ErrorCode.CAPABILITY_DENIED, "Impala binding is disabled or missing")
        if expected_digest is not None and profile.digest != expected_digest:
            raise DomainError(
                ErrorCode.CAPABILITY_DENIED,
                "Impala binding changed since message acceptance; submit a new message",
            )
        return profile
