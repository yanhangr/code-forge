"""Pure audit rules; adapters supply the authenticated actor and DB transaction time."""

from datetime import datetime

from .contracts import AuditFields, DomainError, ErrorCode


def _validate(actor: str, timestamp: datetime) -> None:
    if not actor.strip() or len(actor) > 200:
        raise DomainError(
            ErrorCode.INVALID_REQUEST,
            "Audit actor must be a nonempty reference up to 200 characters",
        )
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise DomainError(ErrorCode.INVALID_REQUEST, "Audit timestamp must be timezone-aware")


def on_insert(actor: str, db_time: datetime) -> AuditFields:
    _validate(actor, db_time)
    return AuditFields(db_time, actor, db_time, actor)


def on_update(before: AuditFields, actor: str, db_time: datetime, *, changed: bool) -> AuditFields:
    if not changed:
        return before
    _validate(actor, db_time)
    if db_time < before.date_updated:
        raise DomainError(
            ErrorCode.STATE_CONFLICT, "Audit update precedes the last committed update"
        )
    return AuditFields(before.date_created, before.created_by, db_time, actor)
