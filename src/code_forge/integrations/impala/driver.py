"""Pinned Impyla integration. Each query owns its connection and cursor.

No cross-thread connection sharing or transparent retries. The first adapter
supports LDAP over TLS using binary HS2; HTTP timeout support is not assumed.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from code_forge.contracts import DomainError, ErrorCode
from code_forge.integrations.impala.config import DepartmentProfile


def connect(profile: DepartmentProfile, secrets: Mapping[str, str]) -> Any:
    from impala.dbapi import connect as impyla_connect

    # These loggers print complete RPC payloads/handles and raw exceptions. Keep
    # operator diagnostics in our sanitized ledger even when root logging is DEBUG.
    for name in ("impala.hiveserver2", "impala._thrift_api"):
        logging.getLogger(name).disabled = True
    kwargs: dict[str, Any] = {
        "host": profile.host,
        "port": profile.port,
        "database": profile.database,
        "auth_mechanism": profile.auth_mechanism,
        "timeout": profile.policy.rpc_timeout_seconds,
        "use_ssl": True,
        "verify_cert": True,
        # In Impyla 0.23.0 'retries' counts all attempts, including the first.
        "retries": 1,
        "use_http_transport": profile.use_http_transport,
        "http_path": profile.http_path,
    }
    if profile.ca_cert:
        kwargs["ca_cert"] = profile.ca_cert
    password = secrets.get(profile.password_env)
    if not password:
        raise DomainError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Impala credential is unavailable")
    kwargs.update(user=profile.user, password=password)
    return impyla_connect(**kwargs)


def query_id(cursor: Any) -> str | None:
    # Impyla 0.23.0 has no public query-id getter. Its HS2 operation GUID is the
    # Impala query ID; keep this version-specific access confined to this adapter.
    operation = getattr(cursor, "_last_operation", None)
    handle = getattr(operation, "handle", None)
    identifier = getattr(handle, "operationId", None)
    guid = getattr(identifier, "guid", None)
    if isinstance(guid, bytes) and len(guid) == 16:
        return (
            f"{int.from_bytes(guid[:8], 'little'):016x}:{int.from_bytes(guid[8:], 'little'):016x}"
        )
    return None
